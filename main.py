import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask


app = FastAPI(title="SnapMega Server", version="2.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


ALLOWED_DOMAINS = {
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "kick.com",
}

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".aac", ".opus",
    ".ogg", ".wav", ".flac",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mkv", ".webm", ".mov",
}

MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

download_semaphore = threading.BoundedSemaphore(2)


class DownloadRequest(BaseModel):
    url: str
    mode: str = "video"
    clip_start: str | None = None
    clip_end: str | None = None


def get_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def is_allowed_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")

        return (
            parsed.scheme.lower() in {"http", "https"}
            and not parsed.username
            and not parsed.password
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

    if not all(
        re.fullmatch(r"\d+(?:\.\d+)?", part)
        for part in parts
    ):
        raise ValueError("صيغة الوقت غير صحيحة.")

    numbers = [float(part) for part in parts]

    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]

    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]

    return numbers[0]


def apply_youtube_options(options: dict) -> dict:
    options.update({
        "format": "18/best[height<=360][ext=mp4]/worst[ext=mp4]/best[height<=360]/worst",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "socket_timeout": 20,
        "extractor_args": {
            "youtube": {
                "player_client": ["web"],
                "po_token": ["web+http://127.0.0.1:4416/"]
            }
        }
    })

    cookie_path = Path(__file__).resolve().parent / "cookies.txt"

    if cookie_path.is_file():
        options["cookiefile"] = str(cookie_path)

    return options


def configure_platform(options: dict, url: str) -> dict:
    host = get_host(url)

    # 1. YouTube
    if (
        host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtu.be"
        or host.endswith(".youtube-nocookie.com")
    ):
        options = apply_youtube_options(options)

    # 2. Kick — تسريع التحميل بالتوازي
    elif host == "kick.com" or host.endswith(".kick.com"):
        options.update({
            "format": "worst[ext=mp4]/best[height<=360]/worst",
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": 8,
            "buffersize": 32768,
            "hls_prefer_native": True,
            "live_from_start": False,
            "http_headers": {
                "User-Agent": USER_AGENT,
                "Referer": "https://kick.com/",
                "Origin": "https://kick.com",
            },
        })

    # 3. Instagram — قبول الصيغة الطبيعية لمنع خطأ No video formats
    elif "instagram.com" in host:
        options.update({
            "format": "best/bestvideo+bestaudio",
            "merge_output_format": "mp4",
            "retries": 2,
            "socket_timeout": 20,
            "http_headers": {
                "User-Agent": USER_AGENT,
                "Referer": "https://www.instagram.com/",
            },
        })

        instagram_cookies = (
            Path(__file__).resolve().parent
            / "instagram_cookies.txt"
        )

        if instagram_cookies.is_file():
            options["cookiefile"] = str(instagram_cookies)

    # 4. TikTok & Facebook — الإعدادات السريعة الأصلية
    else:
        options.update({
            "format": "best/bestvideo+bestaudio",
            "merge_output_format": "mp4",
            "retries": 2,
            "fragment_retries": 2,
            "extractor_retries": 2,
            "http_headers": {
                "User-Agent": USER_AGENT,
            },
        })

    return options


def validate_media_url(url: str) -> None:
    parsed = urlparse(url)
    host = get_host(url)
    path = parsed.path.lower()
    query = parsed.query.lower()

    if "tiktok.com" in host and "/photo/" in path:
        raise HTTPException(
            status_code=400,
            detail=(
                "هذا رابط صور TikTok وليس فيديو. "
                "استعمل رابط فيديو يحتوي على /video/."
            ),
        )

    if "facebook.com" in host and path.rstrip("/") == "/watch":
        has_video_id = bool(
            re.search(r"(?:^|&)v=\d+", query)
        )

        if not has_video_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "رابط Facebook ناقص. "
                    "انسخ رابط الفيديو نفسه أو رابط المشاركة الكامل."
                ),
            )


@app.get("/")
@app.get("/health")
def health_check():
    return {
        "ok": True,
        "service": "snapmega-api",
        "version": "2.2.0",
        "yt_dlp": yt_dlp.version.__version__,
        "status": "ready",
    }


@app.post("/api/download")
def download_media(req: DownloadRequest):
    url = req.url.strip()
    mode = req.mode.lower().strip()

    # تنظيف معلمات التتبع في يوتيوب
    if "youtube.com" in url.lower() or "youtu.be" in url.lower():
        url = url.split("?si=")[0].split("&si=")[0]

    if not is_allowed_url(url):
        raise HTTPException(
            status_code=400,
            detail=(
                "رابط غير مدعوم. استعمل رابطًا من "
                "YouTube أو TikTok أو Instagram أو Facebook أو Kick."
            ),
        )

    validate_media_url(url)

    if mode not in {"video", "audio", "clip"}:
        raise HTTPException(
            status_code=400,
            detail="نوع التحميل غير صالح.",
        )

    start_sec = None
    end_sec = None

    if mode == "clip":
        if not req.clip_start or not req.clip_end:
            raise HTTPException(
                status_code=400,
                detail="حدد وقت البداية والنهاية للمقطع.",
            )

        try:
            start_sec = time_to_seconds(req.clip_start)
            end_sec = time_to_seconds(req.clip_end)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=str(exc),
            )

        if start_sec < 0 or end_sec <= start_sec:
            raise HTTPException(
                status_code=400,
                detail="وقت النهاية يجب أن يكون بعد وقت البداية.",
            )

    output_dir = tempfile.mkdtemp(prefix="snapmega_")
    output_template = os.path.join(
        output_dir,
        "%(id)s.%(ext)s",
    )

    options = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "ignoreerrors": False,
        "overwrites": True,
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

        if mode == "clip":
            options["download_ranges"] = (
                lambda info, ydl: [{
                    "start_time": start_sec,
                    "end_time": end_sec,
                }]
            )
            options["force_keyframes_at_cuts"] = True
            options["postprocessor_args"] = {
                "ffmpeg": ["-c", "copy"]
            }

        with download_semaphore:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.extract_info(url, download=True)

        files = [
            path
            for path in Path(output_dir).iterdir()
            if (
                path.is_file()
                and path.suffix.lower() in MEDIA_EXTENSIONS
                and not path.name.endswith((".part", ".ytdl"))
                and path.stat().st_size > 0
            )
        ]

        if mode == "audio":
            audio_files = [
                path for path in files
                if path.suffix.lower() in AUDIO_EXTENSIONS
            ]

            if audio_files:
                files = audio_files

        if not files:
            raise RuntimeError(
                "لم يتم العثور على ملف صالح بعد التحميل. "
                "قد يكون الفيديو خاصًا أو غير متاح."
            )

        result = max(
            files,
            key=lambda path: path.stat().st_mtime,
        )

        content_types = {
            ".mp4": "video/mp4",
            ".m4v": "video/mp4",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".aac": "audio/aac",
            ".opus": "audio/opus",
            ".ogg": "audio/ogg",
            ".wav": "audio/wav",
            ".webm": "video/webm",
            ".mkv": "video/x-matroska",
            ".mov": "video/quicktime",
            ".flac": "audio/flac",
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
        lower_message = message.lower()

        if any(word in lower_message for word in (
            "login",
            "log in",
            "sign in",
            "private",
            "checkpoint",
            "cookies",
        )):
            message = (
                "المنصة قد تتطلب تسجيل الدخول أو أن الفيديو خاص. "
                "جرّب رابط فيديو عام."
            )

        elif "unsupported url" in lower_message:
            message = (
                "المنصة لم تتعرف على الرابط. "
                "تأكد أنه رابط فيديو كامل وصحيح."
            )

        elif (
            "ffmpeg" in lower_message
            or "ffprobe" in lower_message
        ):
            message = (
                "السيرفر يحتاج FFmpeg لمعالجة هذا الملف. "
                "تحقق من تثبيته في Dockerfile."
            )

        elif (
            "403" in lower_message
            or "forbidden" in lower_message
        ):
            message = (
                "المنصة رفضت طلب التحميل مؤقتًا. "
                "جرّب رابطًا عامًا آخر."
            )

        else:
            message = message[:500]

        raise HTTPException(
            status_code=400,
            detail=f"فشل التحميل: {message}",
        )
