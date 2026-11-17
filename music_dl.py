#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
music_dl.py - скачивает отдельный трек или целый плейлист со SoundCloud и Spotify
в выбранную папку.

    start.bat                                     двойной клик -> интерфейс в браузере
    python music_dl.py --gui                      то же из консоли
    python music_dl.py URL -o "D:\\Music" -f flac  без интерфейса
    python music_dl.py --update                   обновить yt-dlp

SoundCloud качает yt-dlp напрямую. У Spotify нет файлов для скачивания (DRM),
поэтому из Spotify берутся название, исполнитель и длительность, а звук
ищется и качается с YouTube через yt-dlp. Зависимости (yt-dlp и ffmpeg)
ставятся один раз при первом запуске в отдельное окружение.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_NAME = "music_dl"
FORMATS = ("mp3", "m4a", "flac", "opus")
DOWNLOAD_WORKERS = 5
_SC_CLIENT_ID = ""
SC_TEMPLATE = "%(artist,uploader)s - %(title)s.%(ext)s"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")
BAD_WORDS = ("live", "cover", "remix", "sped up", "slowed", "nightcore",
             "karaoke", "instrumental", "reaction", "tutorial")

SPOTIFY_RE = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[a-z-]+/)?(track|album|playlist|artist|show|episode)/([A-Za-z0-9]+)",
    re.I,
)
SOUNDCLOUD_RE = re.compile(r"^https?://(?:www\.|m\.|on\.)?soundcloud\.com/", re.I)
SHORT_RE = re.compile(r"^https?://(?:spotify\.link|snd\.sc)/", re.I)
INVALID_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------------------------------------------------------------- окружение

def init_console() -> None:
    """UTF-8 в консоли Windows, чтобы не падало на русском тексте."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def app_dir() -> Path:
    """Куда класть venv и ffmpeg. MUSIC_DL_HOME переопределяет (портативный режим)."""
    custom = os.environ.get("MUSIC_DL_HOME", "").strip()
    if custom:
        return Path(os.path.expandvars(os.path.expanduser(custom))).resolve()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def has_dist(py: str, dist: str) -> bool:
    """Проверяет пакет по метаданным, не импортируя его."""
    try:
        res = subprocess.run(
            [py, "-c", "import importlib.metadata as m;m.version(%r)" % dist],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return res.returncode == 0


def pip_install(py: str, *specs: str) -> int:
    cmd = [py, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", *specs]
    print("> " + " ".join(shlex.quote(c) for c in cmd))
    try:
        return subprocess.call(cmd)
    except OSError as exc:
        print("  pip не запустился: %s" % exc)
        return 1


def ensure_venv() -> Path:
    venv = app_dir() / "venv"
    py = venv_python(venv)
    if py.exists():
        return py
    print("Первый запуск: создаю окружение в %s" % venv)
    try:
        venv.parent.mkdir(parents=True, exist_ok=True)
        code = subprocess.call([sys.executable, "-m", "venv", str(venv)])
    except OSError as exc:
        code = 1
        print("  venv не создался: %s" % exc)
    if code != 0 or not py.exists():
        sys.exit(
            "Не удалось создать окружение.\n"
            "Поставьте Python 3.12:  https://www.python.org/downloads/"
        )
    return py


def ensure_deps(py: Path, update: bool = False) -> None:
    py_s = str(py)
    if not update and has_dist(py_s, "yt-dlp"):
        return
    print("Ставлю yt-dlp (один раз)...")
    extra = ["--upgrade"] if update else []
    code = pip_install(py_s, *extra, "yt-dlp[default]")
    if code != 0:
        code = pip_install(py_s, *extra, "yt-dlp")
    if code != 0 or not has_dist(py_s, "yt-dlp"):
        sys.exit("yt-dlp не установился. Проверьте интернет и версию Python (нужен 3.9-3.14).")


def install_local_ffmpeg(py: Path) -> Path | None:
    """Берёт ffmpeg из пакета imageio-ffmpeg и кладёт рядом с окружением."""
    if not has_dist(str(py), "imageio-ffmpeg"):
        if pip_install(str(py), "imageio-ffmpeg") != 0:
            return None
    try:
        src = subprocess.check_output(
            [str(py), "-c",
             "import imageio_ffmpeg,sys;sys.stdout.write(imageio_ffmpeg.get_ffmpeg_exe())"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return None
    if not src or not Path(src).is_file():
        return None
    bin_dir = app_dir() / "bin"
    target = bin_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    except OSError:
        return Path(src)
    return target


def ensure_ffmpeg(py: Path) -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    exe = app_dir() / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not exe.exists():
        print("ffmpeg не найден - ставлю локальную копию (один раз)...")
        local = install_local_ffmpeg(py)
        if local is None:
            sys.exit(
                "ffmpeg не установился автоматически.\n"
                "Поставьте вручную:  winget install -e --id Gyan.FFmpeg"
            )
        exe = Path(local)
        print("ffmpeg: %s" % exe)
    os.environ["PATH"] = str(exe.parent) + os.pathsep + os.environ.get("PATH", "")
    return str(exe)


# ------------------------------------------------------------------- ссылки

def resolve_redirect(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.geturl()
    except Exception:
        return url


def normalize_url(raw: str) -> str:
    url = raw.strip().strip('"').strip("'").strip()
    if not url:
        return url
    if url.startswith("spotify:"):
        return url
    if SHORT_RE.match(url):
        url = resolve_redirect(url)
    match = SPOTIFY_RE.match(url)
    if match:
        return "https://open.spotify.com/%s/%s" % (match.group(1).lower(), match.group(2))
    if SOUNDCLOUD_RE.match(url):
        return url.split("?")[0].split("#")[0].rstrip("/")
    return url


def platform_of(url: str) -> str | None:
    if not url:
        return None
    if url.startswith("spotify:") or "open.spotify.com" in url:
        return "spotify"
    if "soundcloud.com" in url:
        return "soundcloud"
    return None


def kind_of(url: str, platform: str) -> str:
    if platform == "spotify":
        match = re.search(r"(?:spotify:|spotify\.com/)(track|album|playlist|artist|show|episode)[:/]",
                          url, re.I)
        return "трек" if match and match.group(1).lower() == "track" else "плейлист"
    if "/sets/" in url or re.search(r"/(tracks|likes|albums|reposts)(/|$)", url):
        return "плейлист"
    return "трек"


def safe_name(text: str, limit: int = 150) -> str:
    text = INVALID_FILE_CHARS.sub("_", text or "").strip(" .")
    return text[:limit].rstrip(" .") or "track"


def count_audio(folder: Path, fmt: str) -> int:
    try:
        return sum(1 for item in folder.iterdir() if item.suffix.lower() == "." + fmt)
    except OSError:
        return 0


def norm_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^0-9a-zа-яё]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------- ввод

def ask(text: str, default: str = "") -> str:
    suffix = " [%s]" % default if default else ""
    try:
        answer = input("%s%s: " % (text, suffix))
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    answer = answer.strip().strip('"').strip("'")
    return answer or default


def ask_platform() -> str:
    print(" 1. SoundCloud")
    print(" 2. Spotify")
    print(" 0. Выход")
    while True:
        choice = ask("Платформа", "1").lower()
        if choice in ("0", "q", "exit", "выход"):
            sys.exit(0)
        if choice in ("1", "soundcloud", "sc"):
            return "soundcloud"
        if choice in ("2", "spotify", "sp"):
            return "spotify"
        print("  Введите 1 или 2.")


def ask_folder(default: Path) -> Path:
    while True:
        raw = ask("Папка для сохранения", str(default))
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path.resolve()
        except OSError as exc:
            print("  Не могу создать папку: %s" % exc)


def ask_format(default: str = "mp3") -> str:
    while True:
        fmt = ask("Формат (%s)" % "/".join(FORMATS), default).lower().lstrip(".")
        if fmt in FORMATS:
            return fmt
        print("  Доступно: %s" % ", ".join(FORMATS))


# ---------------------------------------------------------------- загрузка

def run(cmd: list[str], echo: bool = True) -> int:
    if echo:
        print()
        print("> " + " ".join(shlex.quote(str(c)) for c in cmd))
        print()
    try:
        return subprocess.call([str(c) for c in cmd])
    except OSError as exc:
        print("Не удалось запустить команду: %s" % exc)
        return 1


def capture(cmd: list[str]) -> str:
    """Запускает команду, забирая stdout через файл (без пайпов)."""
    try:
        log = tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace")
    except OSError:
        return ""
    with log:
        try:
            subprocess.run([str(c) for c in cmd], stdout=log, stderr=subprocess.DEVNULL)
        except OSError:
            return ""
        log.seek(0)
        return log.read()


def fetch_text(url: str, timeout: int = 25) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return ""


def soundcloud_client_id(page_url: str) -> str:
    """Публичный client_id из JS SoundCloud - нужен для их API."""
    global _SC_CLIENT_ID
    if _SC_CLIENT_ID:
        return _SC_CLIENT_ID
    html = fetch_text(page_url)
    for src in re.findall(r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', html):
        match = re.search(r'client_id:"([0-9A-Za-z]{20,40})"', fetch_text(src))
        if match:
            _SC_CLIENT_ID = match.group(1)
            return _SC_CLIENT_ID
    return ""


def soundcloud_api_tracks(ids: list[str], page_url: str) -> dict[str, dict]:
    """Названия пачкой через API SoundCloud (до 50 id за запрос)."""
    client_id = soundcloud_client_id(page_url)
    if not client_id or not ids:
        return {}
    found: dict[str, dict] = {}
    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        api = "https://api-v2.soundcloud.com/tracks?ids=%s&client_id=%s" % (",".join(chunk), client_id)
        try:
            data = json.loads(fetch_text(api))
        except ValueError:
            continue
        for track in data if isinstance(data, list) else []:
            found[str(track.get("id"))] = {
                "title": (track.get("title") or "").strip(),
                "artist": ((track.get("user") or {}).get("username") or "").strip(),
                "url": track.get("permalink_url") or "",
            }
    return found


def soundcloud_titles(url: str) -> dict[str, dict]:
    """Названия и исполнители треков со страницы SoundCloud (hydration JSON)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception:
        return {}
    match = re.search(r"window\.__sc_hydration\s*=\s*(\[.*?\]);", html, re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return {}

    found: dict[str, dict] = {}

    def add(track) -> None:
        if not isinstance(track, dict) or track.get("id") is None:
            return
        user = track.get("user") or {}
        found[str(track["id"])] = {
            "title": (track.get("title") or "").strip(),
            "artist": (user.get("username") or "").strip(),
            "url": track.get("permalink_url") or "",
        }

    for item in data:
        kind = item.get("hydratable")
        payload = item.get("data")
        if kind == "playlist" and isinstance(payload, dict):
            for track in payload.get("tracks") or []:
                add(track)
        elif kind in ("track", "sound") and isinstance(payload, dict):
            add(payload)
    return found


def soundcloud_entries(py: Path, url: str, playlist: bool) -> list[dict]:
    """Список треков плейлиста (или один трек) без скачивания."""
    cmd = [str(py), "-m", "yt_dlp", "--flat-playlist", "--dump-json",
           "--no-warnings", "--ignore-errors"]
    cmd += ["--yes-playlist"] if playlist else ["--no-playlist"]
    cmd.append(url)
    known = soundcloud_titles(url)
    entries = []
    for line in capture(cmd).splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        page = item.get("webpage_url") or item.get("url")
        if not isinstance(page, str) or not page.startswith("http"):
            continue
        track_id = str(item.get("id") or "")
        info = known.get(track_id) or {}
        entries.append({
            "url": info.get("url") or page,
            "title": info.get("title") or item.get("title") or "",
            "artist": info.get("artist") or item.get("uploader") or item.get("channel") or "",
            "id": track_id,
        })

    missing = [entry for entry in entries if not entry["title"] and entry["id"]]
    if missing:
        extra = soundcloud_api_tracks([entry["id"] for entry in missing], url)
        for entry in missing:
            info = extra.get(entry["id"]) or {}
            if info.get("title"):
                entry["title"] = info["title"]
                entry["artist"] = info["artist"] or entry["artist"]
                entry["url"] = info["url"] or entry["url"]
    return entries


def track_name(artist: str, title: str) -> str:
    """'Исполнитель - Название', без дубля, если исполнитель уже есть в названии."""
    artist = (artist or "").strip()
    title = (title or "").strip() or "track"
    if artist and not title.lower().startswith(artist.lower()):
        return "%s - %s" % (artist, title)
    return title


def track_output(cmd: list[str], entry: dict) -> list[str]:
    """Команда yt-dlp для одного трека с нормальным именем файла."""
    if not entry.get("title"):
        return cmd + [entry["url"]]  # имя возьмёт сам yt-dlp из метаданных трека
    name = safe_name(track_name(entry.get("artist", ""), entry["title"]))
    return cmd + ["-o", name.replace("%", "%%") + ".%(ext)s", entry["url"]]


def label_from_url(url: str) -> str:
    """Читаемое имя из адреса: /user/track-name -> 'user - track name'."""
    parts = [p.replace("-", " ").replace("_", " ")
             for p in url.split("?")[0].rstrip("/").split("/")[-2:] if p]
    return " - ".join(parts) or url


def soundcloud_label(url: str) -> str:
    """Название трека через oEmbed (для отчёта, если yt-dlp не дал метаданных)."""
    try:
        api = "https://soundcloud.com/oembed?format=json&url=" + urllib.parse.quote(url, safe="")
        req = urllib.request.Request(api, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return (data.get("title") or "").strip()
    except Exception:
        return ""


def entry_label(entry: dict) -> str:
    if entry.get("title"):
        return track_name(entry.get("artist", ""), entry["title"])
    return label_from_url(entry["url"])


def run_track(cmd: list[str]) -> tuple[int, str]:
    """Загрузка одного трека с выводом в файл - чтобы параллельные не мешали друг другу."""
    try:
        log = tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace")
    except OSError:
        try:
            return subprocess.call([str(c) for c in cmd],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), ""
        except OSError:
            return 1, ""
    with log:
        try:
            code = subprocess.call([str(c) for c in cmd], stdout=log, stderr=subprocess.STDOUT)
        except OSError:
            return 1, ""
        log.seek(0)
        return code, log.read()


def last_error(text: str) -> str:
    for line in reversed(text.splitlines()):
        if "ERROR" in line:
            return line.strip()[:300]
    return ""


def log_destination(text: str) -> str:
    hits = re.findall(r"Destination:\s*(.+?\.(?:m4a|webm|mp3|opus|flac|wav|aac|ogg))", text)
    hits += re.findall(r"\[download\]\s*(.+?\.(?:m4a|webm|mp3|opus|flac|wav|aac|ogg))\s*has already been downloaded",
                       text)
    return hits[-1].strip().strip('"') if hits else ""


def dedup_file(path: str) -> str:
    """'Artist - Artist - Title' -> 'Artist - Title'. Возвращает итоговый путь."""
    if not path or not os.path.isfile(path):
        return path
    folder, base = os.path.split(path)
    stem, ext = os.path.splitext(base)
    parts = stem.split(" - ")
    if len(parts) >= 3 and parts[0].strip().lower() == parts[1].strip().lower():
        new_path = os.path.join(folder, parts[0] + " - " + " - ".join(parts[2:]) + ext)
        try:
            if not os.path.exists(new_path):
                os.replace(path, new_path)
                return new_path
        except OSError:
            pass
    return path


def download_tracks(cmd: list[str], entries: list[dict]) -> list[str]:
    """Качает треки в несколько потоков, возвращает список неудачных."""
    total = len(entries)
    failed = []

    def work(entry: dict) -> tuple[bool, str]:
        text = ""
        for attempt in range(3):
            code, text = run_track(track_output(cmd, entry))
            if code == 0:
                dedup_file(log_destination(text))
                return True, ""
            if "429" in text or "Too Many Requests" in text:
                time.sleep(10 * (attempt + 1))  # притормозить, если SoundCloud ругается
            elif attempt < 2:
                time.sleep(3)
        return False, last_error(text)

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        futures = {pool.submit(work, entry): entry for entry in entries}
        for done, future in enumerate(as_completed(futures), 1):
            entry = futures[future]
            label = entry_label(entry)
            try:
                ok, err = future.result()
            except Exception:
                ok, err = False, ""
            print("[%d/%d] %s %s" % (done, total, "OK  " if ok else "FAIL", label))
            if not ok:
                if err:
                    print("   " + err)
                failed.append(soundcloud_label(entry["url"]) or label)
    return failed


def soundcloud_command(py: Path, folder: Path, fmt: str, ffmpeg: str,
                       cookies: str | None = None) -> list[str]:
    cmd = [
        str(py), "-m", "yt_dlp",
        "--no-warnings", "--windows-filenames", "--no-mtime",
        "-x", "--audio-format", fmt, "--audio-quality", "0",
        "--embed-metadata",
        "-o", SC_TEMPLATE,
        "-P", str(folder),
        "--ffmpeg-location", ffmpeg,
        "--no-playlist",
        "--concurrent-fragments", "2",
        "--retries", "3", "--fragment-retries", "3",
    ]
    if cookies:
        cmd += ["--cookies", str(cookies)]
    return cmd


def download_soundcloud(py: Path, url: str, folder: Path, fmt: str, playlist: bool,
                        cookies: str | None, ffmpeg: str) -> tuple[int, list[str]]:
    cmd = soundcloud_command(py, folder, fmt, ffmpeg, cookies)

    entries = soundcloud_entries(py, url, playlist)
    if not entries:  # список не получили - качаем как раньше, без разбора по трекам
        return run(cmd + [url]), []

    if len(entries) == 1:
        entry = entries[0]
        label = entry_label(entry)
        print("[1/1] %s" % label)
        if run(cmd + [entry["url"]], echo=False) != 0:
            return 1, [soundcloud_label(entry["url"]) or label]
        return 0, []

    failed = download_tracks(cmd, entries)
    return (0 if not failed else 1), failed


# ------------------------------------------------------- Spotify -> YouTube

def fetch_spotify_entity(url: str) -> dict:
    match = re.search(r"spotify:(track|album|playlist|artist):([A-Za-z0-9]+)", url) \
        or re.search(r"spotify\.com/(track|album|playlist|artist)/([A-Za-z0-9]+)", url, re.I)
    if not match:
        raise ValueError("не понял ссылку Spotify")
    kind, spotify_id = match.group(1).lower(), match.group(2)
    page = "https://open.spotify.com/embed/%s/%s" % (kind, spotify_id)
    req = urllib.request.Request(page, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Spotify ответил %s. Ссылка открывается в браузере?" % exc.code)
    except Exception as exc:
        raise RuntimeError("нет связи со Spotify: %s" % exc)
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not match:
        raise RuntimeError("Spotify отдал страницу без данных (возможно, плейлист приватный)")
    try:
        data = json.loads(match.group(1))
        return data["props"]["pageProps"]["state"]["data"]["entity"]
    except (ValueError, KeyError):
        raise RuntimeError("не разобрал данные Spotify")


def spotify_track_list(entity: dict) -> list[dict]:
    kind = (entity.get("type") or "").lower()
    if kind == "track":
        artists = ", ".join(a.get("name", "") for a in entity.get("artists") or [])
        return [{
            "title": entity.get("title") or entity.get("name") or "",
            "artist": artists,
            "duration": round((entity.get("duration") or 0) / 1000),
        }]
    tracks = []
    for item in entity.get("trackList") or []:
        if item.get("isPlayable") is False:
            continue
        subtitle = (item.get("subtitle") or "").replace("\u2009", " ").replace("\xa0", " ").strip()
        tracks.append({
            "title": item.get("title") or "",
            "artist": subtitle,
            "duration": round((item.get("duration") or 0) / 1000),
        })
    return [t for t in tracks if t["title"]]


def search_youtube(py: Path, query: str, limit: int = 5) -> list[dict]:
    out = capture([str(py), "-m", "yt_dlp", "ytsearch%d:%s" % (limit, query),
                   "--flat-playlist", "--dump-json", "--no-warnings", "--ignore-errors"])
    found = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("id"):
            found.append({
                "id": item["id"],
                "title": item.get("title") or "",
                "duration": item.get("duration"),
                "channel": item.get("channel") or item.get("uploader") or "",
            })
    return found


def rank_candidates(candidates: list[dict], track: dict) -> list[dict]:
    """Сортирует видео YouTube по похожести: длительность, название, канал."""
    want = norm_text(track["artist"] + " " + track["title"])
    artist_norm = norm_text(track["artist"])
    title_norm = norm_text(track["title"])
    scored = []
    for cand in candidates:
        score = 0.0
        duration = cand.get("duration")
        if track["duration"] and duration:
            diff = abs(duration - track["duration"])
            if diff > 40:
                continue
            score += max(0.0, 4.0 - diff / 4.0)
        name = norm_text(cand["title"])
        score += 4.0 * difflib.SequenceMatcher(None, want, name).ratio()
        if artist_norm and artist_norm in norm_text(cand["channel"]):
            score += 1.5
        for bad in BAD_WORDS:
            if bad in name and bad not in title_norm:
                score -= 2.0
        if score > 3.0:
            scored.append((score, cand))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [cand for _, cand in scored]


def youtube_command(py: Path, video_id: str, track: dict, folder: Path, fmt: str,
                    cookies: str | None, ffmpeg: str, album: str) -> list[str]:
    label = " - ".join(part for part in (track["artist"], track["title"]) if part)
    cmd = [
        str(py), "-m", "yt_dlp", "--no-warnings", "--windows-filenames", "--no-mtime",
        "-x", "--audio-format", fmt, "--audio-quality", "0",
        "--embed-metadata",
        "--no-overwrites",
        "-o", safe_name(label).replace("%", "%%") + ".%(ext)s",
        "-P", str(folder),
        "--ffmpeg-location", ffmpeg,
    ]
    for value, field in ((track["title"], "meta_title"),
                         (track["artist"], "meta_artist"),
                         (album, "meta_album")):
        if value:
            cmd += ["--parse-metadata", "%s:%s" % (value.replace(":", " -"), field)]
    if cookies:
        cmd += ["--cookies", str(cookies)]
    cmd.append("https://www.youtube.com/watch?v=%s" % video_id)
    return cmd


def download_youtube_track(py: Path, video_id: str, track: dict, folder: Path, fmt: str,
                           cookies: str | None, ffmpeg: str, album: str) -> int:
    return run(youtube_command(py, video_id, track, folder, fmt, cookies, ffmpeg, album), echo=False)


def download_spotify(py: Path, url: str, folder: Path, fmt: str,
                     cookies: str | None, ffmpeg: str) -> tuple[int, list[str]]:
    try:
        entity = fetch_spotify_entity(url)
    except (RuntimeError, ValueError) as exc:
        print("Spotify: %s" % exc)
        return 1, []

    tracks = spotify_track_list(entity)
    if not tracks:
        print("В этой ссылке нет треков (или они недоступны).")
        return 1, []

    album = (entity.get("name") or "") if (entity.get("type") or "").lower() == "album" else ""
    total, done, failed = len(tracks), 0, []
    for index, track in enumerate(tracks, 1):
        label = " - ".join(part for part in (track["artist"], track["title"]) if part)
        print("[%d/%d] %s" % (index, total, label))
        query = ("%s %s" % (track["artist"], track["title"])).strip()
        ranked = rank_candidates(search_youtube(py, query), track)
        if not ranked:
            print("   не нашёл подходящего видео на YouTube - пропускаю")
            failed.append(label)
            continue
        for candidate in ranked[:3]:
            if download_youtube_track(py, candidate["id"], track, folder, fmt,
                                      cookies, ffmpeg, album) == 0:
                done += 1
                break
            print("   не вышло с \"%s\", пробую следующий вариант" % candidate["title"])
        else:
            print("   не удалось скачать этот трек")
            failed.append(label)

    print()
    print("Spotify: скачано %d из %d." % (done, total))
    return (0 if done else 1), failed


# --------------------------------------------------------------------- GUI

GUI_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>music.dl</title>
<style>
:root{
  --bg:#050505; --text:#f5f5f5; --muted:#a0a0a0; --faint:#6d6d6d;
  --glass:linear-gradient(180deg, rgba(255,255,255,.09), rgba(255,255,255,.035));
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text)}
body{
  font:14px/1.5 "Inter","Segoe UI",system-ui,-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased;display:flex;justify-content:center;
  padding:40px 24px 80px;min-height:100vh;position:relative;overflow-x:hidden;
}
body::before,body::after{
  content:"";position:fixed;inset:-25%;pointer-events:none;z-index:0;
  background:
    radial-gradient(38% 32% at 22% 12%, rgba(255,255,255,.13), transparent 70%),
    radial-gradient(32% 28% at 82% 26%, rgba(255,255,255,.09), transparent 70%),
    radial-gradient(42% 38% at 55% 96%, rgba(255,255,255,.05), transparent 70%);
  filter:blur(30px);
}
body::after{
  background:
    radial-gradient(28% 24% at 70% 70%, rgba(255,255,255,.055), transparent 70%),
    radial-gradient(30% 26% at 15% 78%, rgba(255,255,255,.04), transparent 70%);
  animation:drift 26s ease-in-out infinite alternate;
}
@keyframes drift{to{transform:translate3d(2.5%,-2%,0) scale(1.06)}}
.app{position:relative;z-index:1;width:100%;max-width:700px}
#topline{position:fixed;top:0;left:0;height:2px;width:0;background:#fff;opacity:0;transition:width .3s,opacity .3s;z-index:9;box-shadow:0 0 18px rgba(255,255,255,.8)}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:60px}
.brand{display:flex;align-items:center;gap:9px;font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:#dcdcdc}
.mark{width:18px;height:18px;border:1px solid rgba(255,255,255,.28);border-radius:5px;position:relative;background:rgba(255,255,255,.05);box-shadow:inset 0 1px 0 rgba(255,255,255,.15)}
.mark:after{content:"";position:absolute;inset:5px;background:#fff;border-radius:1px}
.right{display:flex;align-items:center;gap:8px}
.status{display:inline-flex;align-items:center;gap:8px;padding:8px 13px;border-radius:999px;
  background:rgba(255,255,255,.045);border:1px solid rgba(255,255,255,.09);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.08);
  font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:#8f8f8f;transition:.2s}
.dot{width:5px;height:5px;border-radius:50%;background:#fff;opacity:.25;transition:.2s}
.status.on .dot{opacity:1;box-shadow:0 0 0 3px rgba(255,255,255,.1);animation:pulse 1.8s ease-in-out infinite}
.status.on{color:#ededed;border-color:rgba(255,255,255,.2);background:rgba(255,255,255,.07)}
@keyframes pulse{50%{opacity:.35}}
.link{padding:8px 13px;border-radius:999px;background:rgba(255,255,255,.045);
  border:1px solid rgba(255,255,255,.09);box-shadow:inset 0 1px 0 rgba(255,255,255,.08);
  color:#8f8f8f;font:inherit;font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;
  cursor:pointer;transition:.18s}
.link:hover{color:#fff;background:rgba(255,255,255,.1);border-color:rgba(255,255,255,.24)}
h1{margin:0 0 12px;font-size:42px;line-height:1.04;letter-spacing:-.035em;font-weight:600}
.sub{margin:0;color:var(--muted);font-size:14px;max-width:48ch}
.lbl{display:block;font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--faint);margin:26px 0 9px;padding-left:4px}
.field{
  display:flex;align-items:center;gap:10px;height:54px;padding:0 16px;border-radius:16px;
  background:radial-gradient(120% 160% at 10% -40%,rgba(255,255,255,.12),transparent 55%),var(--glass);
  border:1px solid rgba(255,255,255,.11);border-top-color:rgba(255,255,255,.20);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.14), 0 18px 40px -24px rgba(0,0,0,.9);
  backdrop-filter:blur(24px) saturate(150%);
  -webkit-backdrop-filter:blur(24px) saturate(150%);
  transition:border-color .2s,background .2s;
}
.field:focus-within{border-color:rgba(255,255,255,.32);background:linear-gradient(180deg,rgba(255,255,255,.13),rgba(255,255,255,.05))}
.field input{flex:1;min-width:0;height:100%;border:0;outline:0;background:none;color:var(--text);font:inherit;font-size:14.5px}
.field input::placeholder{color:var(--faint)}
.chip{font-size:10px;letter-spacing:.12em;text-transform:uppercase;padding:5px 9px;border-radius:999px;background:#fff;color:#0a0a0a;opacity:0;transform:scale(.92);transition:.18s;white-space:nowrap;box-shadow:0 6px 16px -6px rgba(255,255,255,.6)}
.chip.on{opacity:1;transform:none}
.pills{display:flex;gap:8px;flex-wrap:wrap}
.pill{height:36px;padding:0 16px;border-radius:12px;background:radial-gradient(120% 150% at 12% -50%,rgba(255,255,255,.14),transparent 55%),var(--glass);
  border:1px solid rgba(255,255,255,.1);box-shadow:inset 0 1px 0 rgba(255,255,255,.1);
  color:#9a9a9a;font:inherit;font-size:12px;letter-spacing:.05em;cursor:pointer;transition:.18s}
.pill:hover{color:#fff;border-color:rgba(255,255,255,.24);transform:translateY(-1px)}
.pill.on{background:#fff;color:#0a0a0a;border-color:#fff;box-shadow:0 10px 24px -10px rgba(255,255,255,.55),inset 0 1px 0 #fff}
.ghost{height:36px;padding:0 14px;border-radius:12px;background:rgba(255,255,255,.06);
  border:1px solid rgba(255,255,255,.12);color:#e8e8e8;font:inherit;font-size:12px;cursor:pointer;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.12);transition:.18s}
.ghost:hover{background:rgba(255,255,255,.11);border-color:rgba(255,255,255,.24)}
.primary{margin-top:26px;width:100%;height:54px;border-radius:16px;border:0;
  background:linear-gradient(180deg,#fff,#e4e4e4);color:#0a0a0a;font:inherit;font-size:14px;font-weight:600;
  letter-spacing:.02em;cursor:pointer;
  box-shadow:inset 0 1px 0 #fff,0 18px 40px -18px rgba(255,255,255,.4);
  transition:transform .16s,box-shadow .16s,filter .16s}
.primary:hover{transform:translateY(-1px);box-shadow:inset 0 1px 0 #fff,0 24px 50px -20px rgba(255,255,255,.5)}
.primary:active{transform:translateY(0);filter:brightness(.95)}
.primary:disabled{background:rgba(255,255,255,.05);color:#5a5a5a;border:1px solid rgba(255,255,255,.08);box-shadow:none;transform:none}
#results{display:none;margin-top:44px}
#results.on{display:block}
.meta{display:flex;justify-content:space-between;font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--faint);padding:0 4px 14px}
.controls{display:none;gap:8px;padding:0 2px 14px}
.controls.on{display:flex}
.controls .ghost:disabled{opacity:.4;cursor:default;transform:none}
#list{display:flex;flex-direction:column;gap:8px}
.track{display:grid;grid-template-columns:34px 1fr 54px;gap:12px;align-items:center;
  padding:12px 14px;border-radius:14px;
  background:radial-gradient(140% 120% at 8% -30%,rgba(255,255,255,.10),transparent 60%),linear-gradient(180deg,rgba(255,255,255,.055),rgba(255,255,255,.02));
  border:1px solid rgba(255,255,255,.075);border-top-color:rgba(255,255,255,.16);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.10),0 14px 30px -24px rgba(0,0,0,.95);
  animation:in .28s ease both;transition:background .18s,border-color .18s}
@keyframes in{from{opacity:0;transform:translateY(3px)}}
.track:hover{background:rgba(255,255,255,.08);border-color:rgba(255,255,255,.16)}
.mid{min-width:0}
.name{font-size:13.5px;color:#eaeaea;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar{height:2px;background:rgba(255,255,255,.14);border-radius:2px;margin-top:7px;opacity:0;transition:opacity .2s}
.track.downloading .bar{opacity:1}
.bar i{display:block;height:100%;width:0;background:#fff;border-radius:2px;box-shadow:0 0 10px rgba(255,255,255,.7);transition:width .4s ease}
.num{font-size:11px;color:#666;font-variant-numeric:tabular-nums}
.state{font-size:12px;color:#a0a0a0;text-align:right;font-variant-numeric:tabular-nums}
.track.done .state{color:#fff}
.track.failed .state{color:#fff;font-size:13px}
.track.failed .name{color:#a8a8a8}
.err{display:none;font-size:11.5px;color:#7d7d7d;margin-top:3px}
.track.failed .err{display:block}
.summary{margin-top:16px;padding:0 4px;font-size:12.5px;color:#9a9a9a}
.summary b{color:#fff;font-weight:500}
.error{display:none;margin-top:16px;padding:12px 15px;border-radius:14px;font-size:13px;
  background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.25);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.12);backdrop-filter:blur(18px)}
.error.on{display:block}
footer{margin-top:56px;font-size:10.5px;color:#3f3f3f;letter-spacing:.14em;text-transform:uppercase}
@media (max-width:560px){h1{font-size:32px}header{margin-bottom:40px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<div id="topline"></div>
<div class="app">
  <header>
    <div class="brand"><span class="mark"></span>music.dl</div>
    <div class="right">
      <span class="status" id="status"><span class="dot"></span><span id="phase">готов</span></span>
      <button class="link" id="quit">Выход</button>
    </div>
  </header>

  <main>
    <h1>Скачать музыку</h1>
    <p class="sub">Ссылка на трек или плейлист — SoundCloud или Spotify. Плейлист скачается целиком.</p>

    <label class="lbl" for="url">Ссылка</label>
    <div class="field">
      <input id="url" autocomplete="off" spellcheck="false" placeholder="https://soundcloud.com/...">
      <span class="chip" id="chip"></span>
    </div>

    <label class="lbl">Формат</label>
    <div class="pills">
      <button class="pill on" data-fmt="mp3">mp3</button>
      <button class="pill" data-fmt="m4a">m4a</button>
      <button class="pill" data-fmt="flac">flac</button>
      <button class="pill" data-fmt="opus">opus</button>
    </div>

    <label class="lbl" for="folder">Папка</label>
    <div class="field">
      <input id="folder" autocomplete="off" spellcheck="false">
      <button class="ghost" id="browse">Обзор</button>
    </div>

    <button class="primary" id="go">Скачать</button>
    <div class="error" id="error"></div>

    <section id="results">
      <div class="meta"><span id="mleft">Загрузка</span><span id="mright"></span></div>
      <div class="controls" id="controls">
        <button class="ghost" id="pause">Пауза</button>
        <button class="ghost" id="cancel">Отмена</button>
      </div>
      <div id="list"></div>
      <div class="summary" id="summary"></div>
    </section>
  </main>

  <footer>локально · yt-dlp · ffmpeg</footer>
</div>
<script>
const $ = (s) => document.querySelector(s);
let fmt = 'mp3';
let busy = false;
let phase = 'idle';

function detect(){
  const v = $('#url').value.trim();
  const chip = $('#chip');
  let name = '';
  if (/open\.spotify\.com|spotify:/.test(v)) name = 'Spotify';
  else if (/soundcloud\.com|snd\.sc/.test(v)) name = 'SoundCloud';
  chip.textContent = name;
  chip.classList.toggle('on', !!name);
}

async function boot(){
  const cfg = await fetch('/api/config').then((r) => r.json()).catch(() => ({}));
  if (cfg.folder) $('#folder').value = cfg.folder;
  if (cfg.initial_url) { $('#url').value = cfg.initial_url; detect(); }
  document.querySelectorAll('.pill').forEach((p) => p.addEventListener('click', () => {
    document.querySelectorAll('.pill').forEach((x) => x.classList.remove('on'));
    p.classList.add('on');
    fmt = p.dataset.fmt;
  }));
  $('#url').addEventListener('input', detect);
  $('#url').addEventListener('keydown', (e) => { if (e.key === 'Enter') start(); });
  $('#go').addEventListener('click', start);
  $('#browse').addEventListener('click', browse);
  $('#quit').addEventListener('click', () => { fetch('/api/quit', { method: 'POST' }); });
  $('#pause').addEventListener('click', () => {
    fetch(phase === 'paused' ? '/api/resume' : '/api/pause', { method: 'POST' });
  });
  $('#cancel').addEventListener('click', () => { fetch('/api/cancel', { method: 'POST' }); });
  poll();
}

async function browse(){
  const r = await fetch('/api/browse', { method: 'POST' }).then((x) => x.json()).catch(() => ({}));
  if (r.folder) $('#folder').value = r.folder;
}

async function start(){
  if (busy) return;
  const url = $('#url').value.trim();
  if (!url) { $('#url').focus(); return; }
  busy = true;
  lock(true);
  hideError();
  const res = await fetch('/api/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url: url, folder: $('#folder').value.trim(), fmt: fmt })
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    busy = false;
    lock(false);
    showError(data.error || 'Не получилось запустить');
    return;
  }
  $('#list').innerHTML = '';
  $('#summary').textContent = '';
  $('#results').classList.add('on');
}

function lock(on){
  $('#go').disabled = on;
  $('#url').disabled = on;
  $('#folder').disabled = on;
  $('#browse').disabled = on;
}

function showError(text){ $('#error').textContent = text; $('#error').classList.add('on'); }
function hideError(){ $('#error').classList.remove('on'); }

function stateText(t){
  if (t.status === 'done') return '✓';
  if (t.status === 'failed') return '×';
  if (t.status === 'paused') return 'пауза';
  if (t.status === 'cancelled') return '—';
  if (t.status === 'searching') return 'поиск';
  if (t.status === 'downloading') return Math.round(t.percent || 0) + '%';
  return '';
}

function syncRows(tracks){
  const list = $('#list');
  if (list.children.length !== tracks.length){
    list.innerHTML = '';
    tracks.forEach((t, i) => {
      const el = document.createElement('div');
      el.className = 'track';
      const num = document.createElement('span');
      num.className = 'num';
      num.textContent = String(i + 1).padStart(2, '0');
      const mid = document.createElement('div');
      mid.className = 'mid';
      const name = document.createElement('div');
      name.className = 'name';
      const bar = document.createElement('div');
      bar.className = 'bar';
      bar.appendChild(document.createElement('i'));
      const err = document.createElement('div');
      err.className = 'err';
      mid.appendChild(name); mid.appendChild(bar); mid.appendChild(err);
      const state = document.createElement('span');
      state.className = 'state';
      el.appendChild(num); el.appendChild(mid); el.appendChild(state);
      list.appendChild(el);
    });
  }
  tracks.forEach((t, i) => {
    const el = list.children[i];
    el.className = 'track ' + (t.status || '');
    el.querySelector('.name').textContent = t.label || '';
    el.querySelector('.state').textContent = stateText(t);
    el.querySelector('.err').textContent = t.error || '';
    if (t.status === 'downloading') el.querySelector('.bar i').style.width = (t.percent || 0) + '%';
  });
}

function render(s){
  if (s.phase === 'idle') return;
  phase = s.phase;
  const running = s.phase === 'running' || s.phase === 'resolving';
  const paused = s.phase === 'paused';
  const alive = running || paused;

  if (s.phase === 'error'){ showError(s.message || 'Ошибка'); busy = false; lock(false); }
  if (s.phase === 'done' || s.phase === 'cancelled'){ busy = false; lock(false); }

  $('#phase').textContent = running ? 'загрузка' : paused ? 'пауза'
    : s.phase === 'done' ? 'готово' : s.phase === 'cancelled' ? 'отменено' : 'ошибка';
  $('#status').classList.toggle('on', alive);
  $('#results').classList.add('on');
  $('#controls').classList.toggle('on', alive);
  $('#pause').textContent = paused ? 'Продолжить' : 'Пауза';
  $('#pause').disabled = s.phase === 'resolving';
  $('#cancel').disabled = !alive;

  const pct = s.total ? Math.round(s.done / s.total * 100) : 0;
  const top = $('#topline');
  top.style.width = pct + '%';
  top.style.opacity = running ? '1' : '0';

  $('#mleft').textContent = s.phase === 'resolving' ? 'Получаю список'
    : paused ? 'Пауза' : s.phase === 'cancelled' ? 'Отменено' : 'Загрузка';
  $('#mright').textContent = s.total ? (s.done + ' из ' + s.total) : '';
  syncRows(s.tracks || []);

  if (s.phase === 'running' || s.phase === 'resolving') $('#summary').textContent = '';
  if (s.phase === 'paused') $('#summary').textContent = 'На паузе · нажмите «Продолжить»';
  if (s.phase === 'cancelled') $('#summary').textContent = 'Загрузка отменена';
  if (s.phase === 'done'){
    const failed = (s.tracks || []).filter((t) => t.status === 'failed').length;
    const ok = (s.tracks || []).length - failed;
    $('#summary').innerHTML = failed
      ? 'Готово · <b>' + ok + '</b> из ' + s.total + ' · не скачалось <b>' + failed + '</b>'
      : 'Готово · <b>' + ok + '</b> из ' + s.total;
    $('#mright').textContent = '';
  }
}

async function poll(){
  try {
    const s = await fetch('/api/state').then((r) => r.json());
    render(s);
  } catch (e) {}
  setTimeout(poll, 600);
}

boot();
</script>
</body>
</html>
"""

GUI_LOCK = threading.Lock()
GUI_CTX = {"py": None, "ffmpeg": None, "logdir": "", "initial_url": "", "items": [],
           "platform": "", "folder": None, "fmt": "mp3", "cmd": [], "album": ""}
GUI_CTL = {"paused": False, "cancelled": False, "procs": set()}
GUI_STATE = {
    "phase": "idle",
    "message": "",
    "url": "",
    "platform": "",
    "fmt": "mp3",
    "folder": "",
    "total": 0,
    "done": 0,
    "tracks": [],
}


def gui_update(**fields) -> None:
    with GUI_LOCK:
        GUI_STATE.update(fields)


def gui_track(index: int, **fields) -> None:
    with GUI_LOCK:
        if 0 <= index < len(GUI_STATE["tracks"]):
            GUI_STATE["tracks"][index].update(fields)


def gui_log_state(path: str) -> tuple[float | None, str]:
    """Процент и имя файла из лога yt-dlp (для живой подписи в списке)."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 4000))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return None, ""
    hits = re.findall(r"(\d{1,3}(?:\.\d+)?)%", text)
    percent = float(hits[-1]) if hits else None
    names = re.findall(r"Destination:\s*(.+?\.(?:m4a|webm|mp3|opus|flac|wav|aac|ogg))", text)
    label = ""
    if names:
        label = os.path.splitext(os.path.basename(names[-1].strip().strip('"')))[0]
    return percent, label


def gui_snapshot() -> dict:
    with GUI_LOCK:
        tracks = [dict(track) for track in GUI_STATE["tracks"]]
        data = {key: GUI_STATE[key] for key in GUI_STATE if key != "tracks"}
        logdir = GUI_CTX["logdir"]
    data["tracks"] = tracks
    data["done"] = sum(1 for track in tracks if track.get("status") in ("done", "failed"))
    if logdir:
        for index, track in enumerate(tracks):
            if track.get("status") == "downloading":
                value, label = gui_log_state(os.path.join(logdir, "%03d.log" % index))
                if value is not None:
                    track["percent"] = value
                if label:
                    track["label"] = label
                    gui_track(index, label=label)
    return data


def gui_run(cmd: list[str], log_path: str) -> tuple[str, str]:
    """Запускает yt-dlp: 'ok' | 'fail' | 'paused' | 'cancelled'."""
    try:
        log = open(log_path, "ab")
    except OSError as exc:
        return "fail", str(exc)
    code = None
    try:
        proc = subprocess.Popen([str(c) for c in cmd], stdout=log, stderr=subprocess.STDOUT)
        with GUI_LOCK:
            GUI_CTL["procs"].add(proc)
        try:
            code = proc.wait()
        finally:
            with GUI_LOCK:
                GUI_CTL["procs"].discard(proc)
    except OSError as exc:
        return "fail", str(exc)
    finally:
        log.close()

    if code == 0:
        return "ok", ""
    if GUI_CTL["cancelled"]:
        return "cancelled", ""
    if GUI_CTL["paused"]:
        return "paused", ""
    try:
        with open(log_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 4000))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        text = ""
    return "fail", last_error(text)


def gui_attempt(cmd: list[str], log_path: str) -> tuple[str, str]:
    """До трёх попыток; пауза и отмена прерывают повторы."""
    err = ""
    for attempt in range(3):
        state, err = gui_run(cmd, log_path)
        if state != "fail":
            return state, err
        with GUI_LOCK:
            if GUI_CTL["paused"]:
                return "paused", ""
            if GUI_CTL["cancelled"]:
                return "cancelled", ""
        if "429" in err or "Too Many Requests" in err:
            time.sleep(10 * (attempt + 1))
        elif attempt < 2:
            time.sleep(2)
    return "fail", err


def gui_pool(indices: list[int], work) -> None:
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        futures = [pool.submit(work, index) for index in indices]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass


def gui_logdir() -> Path:
    path = Path(tempfile.gettempdir()) / ("music_dl_%d_%s" % (os.getpid(), os.urandom(4).hex()))
    path.mkdir(parents=True, exist_ok=True)
    return path


def gui_log_path(index: int) -> str:
    return os.path.join(GUI_CTX["logdir"], "%03d.log" % index)


def gui_worker() -> None:
    return gui_work_soundcloud if GUI_CTX.get("platform") == "soundcloud" else gui_work_spotify


def gui_finish() -> None:
    with GUI_LOCK:
        if GUI_CTL["cancelled"]:
            GUI_STATE["phase"] = "cancelled"
        elif GUI_CTL["paused"]:
            GUI_STATE["phase"] = "paused"
        else:
            GUI_STATE["phase"] = "done"


def gui_job(url: str, folder: Path, fmt: str) -> None:
    py, ffmpeg = GUI_CTX["py"], GUI_CTX["ffmpeg"]
    platform = platform_of(url) or ""
    try:
        gui_update(platform=platform, message="Получаю список треков...")
        if platform == "soundcloud":
            entries = soundcloud_entries(py, url, kind_of(url, platform) == "плейлист")
            items = [{"label": entry_label(entry), "url": entry["url"],
                      "title": entry.get("title", ""), "artist": entry.get("artist", "")}
                     for entry in entries]
        else:
            entity = fetch_spotify_entity(url)
            album = (entity.get("name") or "") if (entity.get("type") or "").lower() == "album" else ""
            items = [{"label": track_name(track["artist"], track["title"]), "track": track}
                     for track in spotify_track_list(entity)]
            GUI_CTX["album"] = album

        with GUI_LOCK:
            cancelled = GUI_CTL["cancelled"]
        if cancelled:
            gui_update(phase="cancelled", message="")
            return
        if not items:
            gui_update(phase="error", message="Не нашёл треков по этой ссылке")
            return

        if not GUI_CTX.get("logdir"):
            GUI_CTX["logdir"] = str(gui_logdir())
        GUI_CTX["items"] = items
        GUI_CTX["platform"] = platform
        GUI_CTX["folder"] = folder
        GUI_CTX["fmt"] = fmt
        if platform == "soundcloud":
            GUI_CTX["cmd"] = soundcloud_command(py, folder, fmt, ffmpeg)

        gui_update(phase="running", total=len(items), done=0, message="",
                   tracks=[{"label": item["label"], "status": "queued", "percent": 0.0, "error": ""}
                           for item in items])

        gui_pool(list(range(len(items))), gui_worker())
        gui_finish()
    except Exception as exc:
        gui_update(phase="error", message="%s" % exc)


def gui_stopped() -> str:
    """'' если работаем, иначе 'paused' или 'cancelled' (без удержания замка)."""
    with GUI_LOCK:
        if GUI_CTL["cancelled"]:
            return "cancelled"
        if GUI_CTL["paused"]:
            return "paused"
    return ""


def gui_work_soundcloud(index: int) -> None:
    item = GUI_CTX["items"][index]
    stopped = gui_stopped()
    if stopped:
        gui_track(index, status=stopped)
        return
    gui_track(index, status="downloading")
    state, err = gui_attempt(track_output(GUI_CTX["cmd"], item), gui_log_path(index))
    if state == "ok":
        try:
            with open(gui_log_path(index), "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            text = ""
        final = dedup_file(log_destination(text))
        label = os.path.splitext(os.path.basename(final))[0] if final else item["label"]
        gui_track(index, status="done", percent=100, label=label)
    elif state in ("paused", "cancelled"):
        gui_track(index, status=state)
    else:
        gui_track(index, status="failed", label=soundcloud_label(item["url"]) or item["label"], error=err)


def gui_work_spotify(index: int) -> None:
    item = GUI_CTX["items"][index]
    track = item["track"]
    stopped = gui_stopped()
    if stopped:
        gui_track(index, status=stopped)
        return
    gui_track(index, status="searching")
    ranked = rank_candidates(
        search_youtube(GUI_CTX["py"], ("%s %s" % (track["artist"], track["title"])).strip()), track)
    stopped = gui_stopped()
    if stopped:
        gui_track(index, status=stopped)
        return
    if not ranked:
        gui_track(index, status="failed", error="не нашёл на YouTube")
        return
    gui_track(index, status="downloading")
    err = ""
    for candidate in ranked[:3]:
        state, err = gui_attempt(
            youtube_command(GUI_CTX["py"], candidate["id"], track, GUI_CTX["folder"],
                            GUI_CTX["fmt"], None, GUI_CTX["ffmpeg"], GUI_CTX.get("album", "")),
            gui_log_path(index))
        if state == "ok":
            gui_track(index, status="done", percent=100)
            return
        if state in ("paused", "cancelled"):
            gui_track(index, status=state)
            return
    gui_track(index, status="failed", error=err)


def gui_pause() -> bool:
    with GUI_LOCK:
        if GUI_STATE["phase"] != "running":
            return False
        GUI_CTL["paused"] = True
        GUI_STATE["phase"] = "paused"
    return True


def gui_cancel() -> bool:
    with GUI_LOCK:
        if GUI_STATE["phase"] not in ("running", "paused", "resolving"):
            return False
        GUI_CTL["cancelled"] = True
        GUI_CTL["paused"] = False
        GUI_STATE["phase"] = "cancelled"
        procs = list(GUI_CTL["procs"])
    for proc in procs:
        try:
            proc.terminate()
        except OSError:
            pass
    return True


def gui_resume() -> bool:
    with GUI_LOCK:
        if GUI_STATE["phase"] != "paused":
            return False
        GUI_CTL["paused"] = False
        GUI_STATE["phase"] = "running"
        indices = [i for i, track in enumerate(GUI_STATE["tracks"]) if track["status"] == "paused"]
    if not indices:
        gui_finish()
        return True
    for index in indices:
        gui_track(index, status="queued", error="")
    threading.Thread(target=gui_resume_worker, args=(indices,), daemon=True).start()
    return True


def gui_resume_worker(indices: list[int]) -> None:
    try:
        gui_pool(indices, gui_worker())
    finally:
        gui_finish()


def gui_browse() -> str:
    py = GUI_CTX["py"]
    handle, out_path = tempfile.mkstemp(suffix=".txt")
    os.close(handle)
    code = (
        "import tkinter as tk, sys\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)\n"
        "path = filedialog.askdirectory(title='Куда сохранять')\n"
        "root.destroy()\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write(path or '')\n"
    )
    try:
        subprocess.run([str(py), "-c", code, out_path], timeout=180)
        with open(out_path, encoding="utf-8") as handle_out:
            return handle_out.read().strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


class GUIHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body, ctype: str = "application/json; charset=utf-8") -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self._send(200, GUI_HTML, "text/html; charset=utf-8")
        elif self.path.startswith("/api/state"):
            self._send(200, json.dumps(gui_snapshot(), ensure_ascii=False))
        elif self.path.startswith("/api/config"):
            self._send(200, json.dumps({
                "folder": str(Path.home() / "Music"),
                "initial_url": GUI_CTX["initial_url"],
                "formats": list(FORMATS),
            }, ensure_ascii=False))
        else:
            self._send(404, "{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            payload = {}

        if self.path == "/api/start":
            url = normalize_url(payload.get("url") or "")
            fmt = (payload.get("fmt") or "mp3").lower()
            folder_raw = (payload.get("folder") or "").strip() or str(Path.home() / "Music")
            if not url or not platform_of(url):
                self._send(400, json.dumps({"error": "Не понял ссылку. Нужна ссылка SoundCloud или Spotify."},
                                           ensure_ascii=False))
                return
            if fmt not in FORMATS:
                self._send(400, json.dumps({"error": "Неизвестный формат"}, ensure_ascii=False))
                return
            with GUI_LOCK:
                if GUI_STATE["phase"] in ("resolving", "running", "paused"):
                    self._send(409, json.dumps({"error": "Уже идёт загрузка"}, ensure_ascii=False))
                    return
            folder = Path(os.path.expandvars(os.path.expanduser(folder_raw)))
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._send(400, json.dumps({"error": "Не могу создать папку: %s" % exc}, ensure_ascii=False))
                return
            with GUI_LOCK:
                GUI_CTL["paused"] = False
                GUI_CTL["cancelled"] = False
                GUI_CTL["procs"] = set()
            gui_update(phase="resolving", message="", url=url, platform=platform_of(url) or "",
                       fmt=fmt, folder=str(folder), total=0, done=0, tracks=[])
            threading.Thread(target=gui_job, args=(url, folder, fmt), daemon=True).start()
            self._send(200, '{"ok": true}')
        elif self.path == "/api/pause":
            ok = gui_pause()
            self._send(200 if ok else 409, json.dumps({"ok": ok}))
        elif self.path == "/api/resume":
            ok = gui_resume()
            self._send(200 if ok else 409, json.dumps({"ok": ok}))
        elif self.path == "/api/cancel":
            ok = gui_cancel()
            self._send(200, json.dumps({"ok": ok}))
        elif self.path == "/api/browse":
            self._send(200, json.dumps({"folder": gui_browse()}, ensure_ascii=False))
        elif self.path == "/api/quit":
            self._send(200, '{"ok": true}')
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self._send(404, "{}")


def gui_main(url: str = "", port: int = 0, open_browser: bool = True) -> int:
    py = ensure_venv()
    ensure_deps(py)
    ffmpeg = ensure_ffmpeg(py)
    GUI_CTX["py"] = py
    GUI_CTX["ffmpeg"] = ffmpeg
    GUI_CTX["logdir"] = str(gui_logdir())
    GUI_CTX["initial_url"] = url

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), GUIHandler)
    except OSError as exc:
        print("Не удалось открыть порт: %s" % exc)
        return 1

    link = "http://127.0.0.1:%d/" % server.server_address[1]
    print("Интерфейс: %s" % link)
    print("Не закрывайте это окно, пока качаете. Выход - кнопка в интерфейсе.")
    if open_browser:
        try:
            webbrowser.open(link)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        shutil.rmtree(GUI_CTX.get("logdir") or "", ignore_errors=True)
    return 0


# --------------------------------------------------------------------- CLI

def print_banner() -> None:
    print("=" * 60)
    print("  SoundCloud / Spotify  ->  папка")
    print("=" * 60)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="music_dl.py",
        description="Скачивание трека или плейлиста со SoundCloud и Spotify в выбранную папку.",
        epilog='Пример: python music_dl.py "https://soundcloud.com/..." -o "D:\\Music" -f mp3 -y',
    )
    parser.add_argument("url", nargs="?", help="ссылка на трек или плейлист")
    parser.add_argument("-o", "--output", help="папка для сохранения")
    parser.add_argument("-f", "--format", choices=FORMATS, help="формат файлов (по умолчанию mp3)")
    parser.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение")
    parser.add_argument("--cookies", help="cookies.txt (если YouTube просит вход)")
    parser.add_argument("--update", action="store_true", help="обновить yt-dlp")
    parser.add_argument("--gui", action="store_true", help="открыть интерфейс в браузере")
    parser.add_argument("--port", type=int, default=0, help="порт для --gui (0 - любой свободный)")
    parser.add_argument("-V", "--version", action="version", version="music_dl 1.0")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    init_console()
    args = parse_args(argv)

    if args.gui:
        return gui_main(args.url or "", args.port)

    py = ensure_venv()
    if args.update:
        ensure_deps(py, update=True)
        ensure_ffmpeg(py)
        print("Готово: yt-dlp обновлён.")
        return 0
    ensure_deps(py)
    ffmpeg = ensure_ffmpeg(py)

    url = normalize_url(args.url) if args.url else ""
    if url:
        platform = platform_of(url)
        if not platform:
            print("Не понял ссылку: %s" % url)
            print("Поддерживаются soundcloud.com и open.spotify.com")
            return 2
    else:
        print_banner()
        platform = ask_platform()
        while True:
            url = normalize_url(ask("Ссылка на трек или плейлист"))
            found = platform_of(url)
            if found == platform:
                break
            if found:
                print("  Это ссылка %s - переключаюсь на неё." % found)
                platform = found
                break
            print("  Не похоже на ссылку SoundCloud или Spotify.")

    kind = kind_of(url, platform)

    if args.output:
        folder = Path(os.path.expandvars(os.path.expanduser(args.output)))
        folder.mkdir(parents=True, exist_ok=True)
        folder = folder.resolve()
    else:
        folder = ask_folder(Path.home() / "Music")

    fmt = args.format or ask_format("mp3")
    cookies = os.path.abspath(os.path.expanduser(args.cookies)) if args.cookies else None

    if not args.yes:
        print()
        print("  Платформа : %s" % platform)
        print("  Тип       : %s" % kind)
        print("  Ссылка    : %s" % url)
        print("  Папка     : %s" % folder)
        print("  Формат    : %s" % fmt)
        if ask("Начать скачивание? (Y/n)", "y").lower() not in ("y", "yes", "д", "да"):
            print("Отменено.")
            return 0

    before = count_audio(folder, fmt)
    if platform == "soundcloud":
        code, failed = download_soundcloud(py, url, folder, fmt, kind == "плейлист", cookies, ffmpeg)
    else:
        code, failed = download_spotify(py, url, folder, fmt, cookies, ffmpeg)
    added = max(0, count_audio(folder, fmt) - before)

    print()
    if failed:
        print("Не удалось скачать (%d):" % len(failed))
        for label in failed:
            print("  - %s" % label)
        print()
    if code == 0:
        print("Готово. Файлы: %s" % folder)
        return 0
    if added:
        print("Скачано файлов: %d, часть треков не удалась (причины - выше)." % added)
        print("Файлы: %s" % folder)
        return 0

    print("Загрузка завершилась с ошибкой (код %s)." % code)
    if platform == "soundcloud":
        print("Если это плейлист и не скачался ни один трек, они обычно недоступны")
        print("в вашем регионе (гео-блокировка) или удалены. Строки с причиной - выше.")
    else:
        print("Если YouTube просит войти (\"Sign in to confirm you're not a bot\"),")
        print("укажите cookies браузера:  --cookies cookies.txt")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
