
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel
import yt_dlp


app = FastAPI(title="SnapMega Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class DownloadRequest(BaseModel):
    url: str
    mode: str = "video"
    clip_start: str | None = None
    clip_end: str | None = None


ALLOWED_DOMAINS = {
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "tiktok.com", "instagram.com", "facebook.com",
    "fb.watch", "kick.com",
}

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac"
}

MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | {
    ".mp4", ".m4v", ".mkv", ".webm", ".mov"
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def get_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_allowed_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        return (
            parsed.scheme.lower() in {"http", "https"}
            and any(
                host == domain or host.endswith("." + domain)
                for domain in ALLOWED_DOMAINS
            )
        )
    except Exception:
        return False


def time_to_seconds(value: str) -> float:
    parts = value.strip().split(":")

    if not 1 <= len(parts) <= 3:
        raise ValueError("صيغة الوقت غير صحيحة.")

    if not all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
        raise ValueError("صيغة الوقت غير صحيحة.")

    numbers = [float(p) for p in parts]

    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return numbers[0]


def apply_youtube_options(options: dict) -> dict:
    options["format"] = (
        "best[height<=360][ext=mp4]/"
        "best[height<=360]/"
        "worst[ext=mp4]/worst"
    )
    options["merge_output_format"] = "mp4"
    options["noplaylist"] = True
    options["socket_timeout"] = 30

    cookie_path = Path(__file__).resolve().parent / "cookies.txt"
    if cookie_path.is_file():
        options["cookiefile"] = str(cookie_path)

    return options


def configure_platform(options: dict, url: str) -> dict:
    host = get_host(url)
    lower_url = url.lower()

    # Default video quality: up to 360p when available
    options["format"] = (
        "best[height<=360][ext=mp4]/"
        "best[height<=360]/"
        "worst[ext=mp4]/worst"
    )
    options["merge_output_format"] = "mp4"
    options["http_headers"] = {
        "User-Agent": USER_AGENT,
    }

    if host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be":
        options = apply_youtube_options(options)

    elif host == "kick.com" or host.endswith(".kick.com"):
        options.update({
            "format": (
                "best[height<=360][ext=mp4]/"
                "best[height<=360]/"
                "worst[ext=mp4]/worst"
            ),
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": 4,
            "buffersize": 16384,
            "hls_prefer_native": True,
            "live_from_start": False,
            "http_headers": {
                "User-Agent": USER_AGENT,
                "Referer": "https://kick.com/",
                "Origin": "https://kick.com",
            },
        })

    elif "facebook.com" in host or host == "fb.watch":
        options.update({
            "format": (
                "best[height<=360][ext=mp4]/"
                "best[height<=360]/"
                "worst[ext=mp4]/worst"
            ),
            "merge_output_format": "mp4",
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 2,
            "http_headers": {
                "User-Agent": USER_AGENT,
                "Referer": "https://www.facebook.com/",
            },
        })

    elif "tiktok.com" in host:
        options.update({
            "format": (
                "best[height<=360][ext=mp4]/"
                "best[height<=360]/"
                "worst[ext=mp4]/worst"
            ),
            "merge_output_format": "mp4",
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 2,
            "http_headers": {
                "User-Agent": USER_AGENT,
                "Referer": "https://www.tiktok.com/",
            },
        })

    elif "instagram.com" in host:
        options.update({
            "format": (
                "best[height<=360][ext=mp4]/"
                "best[height<=360]/"
                "worst[ext=mp4]/worst"
            ),
            "merge_output_format": "mp4",
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 2,
            "http_headers": {
                "User-Agent": USER_AGENT,
                "Referer": "https://www.instagram.com/",
            },
        })

    return options


def validate_media_url(url: str) -> None:
    host = get_host(url)
    path = urlparse(url).path.lower()
    query = urlparse(url).query.lower()

    # A TikTok photo post is not a normal video URL
    if "tiktok.com" in host and "/photo/" in path:
        raise HTTPException(
            status_code=400,
            detail=(
                "هذا رابط صور TikTok وليس فيديو. "
                "استعمل رابط فيديو يحتوي على /video/."
            ),
        )

    # A Facebook watch URL without a video ID is incomplete
    if "facebook.com" in host and path.rstrip("/") == "/watch":
        has_video_id = bool(
            re.search(r"(?:^|&)v=\d+", query)
        )
        if not has_video_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "رابط Facebook ناقص: صفحة watch وحدها لا تكفي. "
                    "انسخ رابط الفيديو نفسه أو رابط المشاركة الكامل."
                ),
            )


@app.get("/")
@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "message": "SnapMega server is alive",
        "yt_dlp": yt_dlp.version.__version__,
    }


@app.post("/api/download")
def download_media(req: DownloadRequest):
    url = req.url.strip()
    mode = req.mode.lower().strip()

    if not is_allowed_url(url):
        raise HTTPException(
            status_code=400,
            detail="رابط غير مدعوم. استعمل رابطًا من منصة مدعومة.",
        )

    validate_media_url(url)

    if mode not in {"video", "audio", "clip"}:
        raise HTTPException(
            status_code=400,
            detail="نوع التحميل غير صالح.",
        )

    if (req.clip_start is None) != (req.clip_end is None):
        raise HTTPException(
            status_code=400,
            detail="حدد وقت البداية والنهاية للقص.",
        )

    start_sec = end_sec = None

    if req.clip_start is not None and req.clip_end is not None:
        try:
            start_sec = time_to_seconds(req.clip_start)
            end_sec = time_to_seconds(req.clip_end)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        if start_sec < 0 or end_sec <= start_sec:
            raise HTTPException(
                status_code=400,
                detail="وقت نهاية المقطع يجب أن يكون بعد وقت البداية.",
            )

    output_dir = tempfile.mkdtemp(prefix="snapmega_")
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")

    options = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "ignoreerrors": False,
    }

    try:
        options = configure_platform(options, url)

        if mode == "audio":
            options["format"] = "bestaudio/best"
            options["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }]

        if start_sec is not None and end_sec is not None:
            options["download_ranges"] = (
                lambda info, ydl: [{
                    "start_time": start_sec,
                    "end_time": end_sec,
                }]
            )
            options["force_keyframes_at_cuts"] = True

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.extract_info(url, download=True)

        files = [
            p for p in Path(output_dir).iterdir()
            if p.is_file()
            and p.suffix.lower() in MEDIA_EXTENSIONS
            and not p.name.endswith((".part", ".ytdl"))
            and p.stat().st_size > 0
        ]

        if mode == "audio":
            audio_files = [
                p for p in files
                if p.suffix.lower() in AUDIO_EXTENSIONS
            ]
            if audio_files:
                files = audio_files

        if not files:
            raise RuntimeError(
                "لم يتم العثور على ملف صالح بعد التحميل."
            )

        result = max(files, key=lambda p: p.stat().st_mtime)

        content_types = {
            ".mp4": "video/mp4",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".webm": "video/webm",
            ".mkv": "video/x-matroska",
            ".mov": "video/quicktime",
        }

        return FileResponse(
            path=str(result),
            media_type=content_types.get(
                result.suffix.lower(),
                "application/octet-stream",
            ),
            filename=result.name,
            background=BackgroundTask(
                shutil.rmtree,
                output_dir,
                ignore_errors=True,
            ),
        )

    except HTTPException:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise

    except Exception as exc:
        shutil.rmtree(output_dir, ignore_errors=True)

        message = str(exc)
        if len(message) > 700:
            message = message[:700]

        lower_message = message.lower()

        if any(word in lower_message for word in (
            "login", "log in", "sign in", "private", "checkpoint"
        )):
            message = (
                "المنصة تطلب تسجيل الدخول أو أن المحتوى خاص. "
                "جرّب رابط فيديو عام آخر."
            )
        elif "unsupported url" in lower_message:
            message = (
                "المنصة لم تتعرف على هذا الرابط. "
                "تأكد أنه رابط فيديو كامل وعام، وليس رابط صفحة أو صور."
            )

        raise HTTPException(
            status_code=400,
            detail=f"فشل التحميل: {message}",
        )
