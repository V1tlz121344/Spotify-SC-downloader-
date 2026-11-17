#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
music_dl.py - скачивает отдельный трек или целый плейлист со SoundCloud и Spotify
в выбранную папку.

    start.bat                                     двойной клик -> меню
    python music_dl.py                            то же из консоли
    python music_dl.py URL -o "D:\\Music" -f flac  без меню
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
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP_NAME = "music_dl"
FORMATS = ("mp3", "m4a", "flac", "opus")
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
            "Поставьте Python 3.12:  winget install -e --id Python.Python.3.12"
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
        sys.exit("yt-dlp не установился. Проверьте интернет и версию Python (нужен 3.9-3.13).")


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


def soundcloud_entries(py: Path, url: str, playlist: bool) -> list[dict]:
    """Список треков плейлиста (или один трек) без скачивания."""
    cmd = [str(py), "-m", "yt_dlp", "--flat-playlist", "--dump-json",
           "--no-warnings", "--ignore-errors"]
    cmd += ["--yes-playlist"] if playlist else ["--no-playlist"]
    cmd.append(url)
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
        entries.append({
            "url": page,
            "title": item.get("title") or "",
            "artist": item.get("uploader") or item.get("channel") or item.get("creator") or "",
        })
    return entries


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


def download_soundcloud(py: Path, url: str, folder: Path, fmt: str, playlist: bool,
                        cookies: str | None, ffmpeg: str) -> tuple[int, list[str]]:
    cmd = [
        str(py), "-m", "yt_dlp",
        "--no-warnings", "--windows-filenames", "--no-mtime",
        "-x", "--audio-format", fmt, "--audio-quality", "0",
        "--embed-metadata",
        "-o", SC_TEMPLATE,
        "-P", str(folder),
        "--ffmpeg-location", ffmpeg,
        "--no-playlist",
    ]
    if cookies:
        cmd += ["--cookies", str(cookies)]

    entries = soundcloud_entries(py, url, playlist)
    if not entries:  # список не получили - качаем как раньше, без разбора по трекам
        return run(cmd + [url]), []

    failed = []
    total = len(entries)
    for index, entry in enumerate(entries, 1):
        label = " - ".join(part for part in (entry["artist"], entry["title"]) if part) \
            or label_from_url(entry["url"])
        print("[%d/%d] %s" % (index, total, label))
        if run(cmd + [entry["url"]], echo=False) != 0:
            failed.append(soundcloud_label(entry["url"]) or label)
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


def download_youtube_track(py: Path, video_id: str, track: dict, folder: Path, fmt: str,
                           cookies: str | None, ffmpeg: str, album: str) -> int:
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
    return run(cmd, echo=False)


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
    parser.add_argument("-V", "--version", action="version", version="music_dl 1.0")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    init_console()
    args = parse_args(argv)

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
