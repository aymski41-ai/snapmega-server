
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request as URLRequest, urlopen

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
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
    "tiktok.com", "instagram.com", "facebook.com", "fb.watch",
    "kick.com",
}

MEDIA_EXTENSIONS = {
    ".mp4", ".m4v", ".mkv", ".webm", ".mov",
    ".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac",
}


def is_allowed_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        return (
            parsed.scheme in {"http", "https"}
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
    options["format"] = "best[ext=mp4]/best[height<=720]/best"
    options["merge_output_format"] = "mp4"
    options["noplaylist"] = True
    options["socket_timeout"] = 30

    cookie_path = Path(__file__).resolve().parent / "cookies.txt"
    if cookie_path.is_file():
        options["cookiefile"] = str(cookie_path)

    options["extractor_args"] = {
        "youtube": {
            "player_client": ["web", "mweb"]
        }
    }

    return options


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
            detail="رابط غير مدعوم. استعمل رابطاً مباشراً من منصة مدعومة."
        )

    if mode not in {"video", "audio", "clip"}:
        raise HTTPException(status_code=400, detail="نوع التحميل غير صالح.")

    if (req.clip_start is None) != (req.clip_end is None):
        raise HTTPException(
            status_code=400,
            detail="حدد وقت البداية والنهاية للقص."
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
                detail="وقت نهاية المقطع يجب أن يكون بعد وقت البداية."
            )

    output_dir = tempfile.mkdtemp(prefix="snapmega_")
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
    lower_url = url.lower()

    options = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "ignoreerrors": False,
    }

    if "youtube.com" in lower_url or "youtu.be" in lower_url:
        options = apply_youtube_options(options)

    elif "kick.com" in lower_url:
        options.update({
            "format": "best[ext=mp4]/best[height<=720]/best",
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": 4,
            "buffersize": 16384,
            "hls_prefer_native": True,
            "live_from_start": False,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Referer": "https://kick.com/",
                "Origin": "https://kick.com",
            },
        })

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

    try:
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
                if p.suffix.lower() in {
                    ".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac"
                }
            ]
            if audio_files:
                files = audio_files

        if not files:
            raise RuntimeError(
                "اكتمل طلب التحميل دون العثور على ملف وسائط صالح."
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
                result.suffix.lower(), "application/octet-stream"
            ),
            filename=result.name,
            background=BackgroundTask(
                shutil.rmtree, output_dir, ignore_errors=True
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

        raise HTTPException(
            status_code=400,
            detail=f"فشل التحميل: {message}",
        )
