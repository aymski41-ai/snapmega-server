import os
import re
import time
import asyncio
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    import requests as curl_requests

import yt_dlp

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
)

from fastapi.middleware.cors import CORSMiddleware
from fastapi.background import BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


# ============================================================
# SNAPMEGA API - v6.5.0 (FINAL PRODUCTION CODE)
# ============================================================

APP_VERSION = "6.5.0"

MAX_CLIP_SECONDS = int(os.getenv("MAX_CLIP_SECONDS", "7200"))
MAX_URL_LENGTH = int(os.getenv("MAX_URL_LENGTH", "4096"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "900"))
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "60"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))
MAX_OUTPUT_SIZE_MB = int(os.getenv("MAX_OUTPUT_SIZE_MB", "500"))


# ============================================================
# APP & CORS
# ============================================================

app = FastAPI(
    title="SnapMega API",
    version=APP_VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "Content-Disposition",
        "Content-Type",
        "Content-Length",
    ],
)


# ============================================================
# CONCURRENCY & RATE LIMIT
# ============================================================

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
request_log: dict[str, list[float]] = {}


def cleanup_rate_limit():
    now = time.time()
    expired = [
        ip
        for ip, timestamps in request_log.items()
        if not timestamps or now - timestamps[-1] > RATE_LIMIT_WINDOW
    ]
    for ip in expired:
        request_log.pop(ip, None)


def check_rate_limit(ip: str):
    cleanup_rate_limit()
    now = time.time()
    timestamps = request_log.setdefault(ip, [])

    timestamps[:] = [
        t for t in timestamps
        if now - t < RATE_LIMIT_WINDOW
    ]

    if len(timestamps) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
        )

    timestamps.append(now)


# ============================================================
# REQUEST MODEL
# ============================================================

class DownloadRequest(BaseModel):
    url: str = Field(
        ...,
        min_length=1,
        max_length=MAX_URL_LENGTH,
    )
    mode: str = Field(
        default="video",
        pattern="^(video|audio|clip)$",
    )
    clip_start: float | None = None
    clip_end: float | None = None


# ============================================================
# ALLOWED DOMAINS & VALIDATION
# ============================================================

ALLOWED_DOMAINS = {
    "youtube.com",
    "m.youtube.com",
    "youtu.be",
    "tiktok.com",
    "vm.tiktok.com",
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "fb.com",
    "kick.com",
}


def get_hostname(url: str) -> str:
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname
    except Exception:
        return ""


def is_allowed_url(url: str) -> bool:
    hostname = get_hostname(url)
    if not hostname:
        return False
    return any(
        hostname == domain or hostname.endswith("." + domain)
        for domain in ALLOWED_DOMAINS
    )


def is_youtube_url(url: str) -> bool:
    hostname = get_hostname(url)
    return hostname in {
        "youtube.com",
        "m.youtube.com",
        "youtu.be",
    }


def is_fb_watch_url(url: str) -> bool:
    hostname = get_hostname(url)
    return hostname == "fb.watch" or hostname.endswith(".fb.watch")


def is_facebook_url(url: str) -> bool:
    hostname = get_hostname(url)
    return any(
        hostname == domain or hostname.endswith("." + domain)
        for domain in [
            "facebook.com",
            "fb.com",
            "fb.watch",
        ]
    )


# ============================================================
# URL NORMALIZATION
# ============================================================

def normalize_youtube_url(url: str) -> str:
    match = re.search(
        r"youtube\.com/shorts/([^/?#]+)",
        url,
        re.IGNORECASE,
    )
    if match:
        return "https://www.youtube.com/watch?v=" + match.group(1)

    match = re.search(
        r"youtu\.be/([^/?#]+)",
        url,
        re.IGNORECASE,
    )
    if match:
        return "https://www.youtube.com/watch?v=" + match.group(1)

    return url


def resolve_fb_watch(url: str) -> str:
    """فك روابط fb.watch بـ curl-cffi لتجاوز خطأ 302"""
    try:
        session = curl_requests.Session(impersonate="chrome120")
        resp = session.get(url, allow_redirects=True, timeout=12)
        final_url = str(resp.url)

        if (
            "login" in final_url
            or "checkpoint" in final_url
            or "next=" in final_url
        ):
            m = re.search(r"v%3D(\d+)", final_url) or re.search(r"videos%2F(\d+)", final_url)
            if m:
                return "https://www.facebook.com/watch/?v=" + m.group(1)

            canon = re.search(r'<link\s+rel="canonical"\s+href="([^"]+)"', resp.text)
            if canon:
                return canon.group(1)

        return final_url
    except Exception:
        return url


def normalize_url(url: str) -> str:
    url = url.strip()
    if is_youtube_url(url):
        return normalize_youtube_url(url)
    if is_fb_watch_url(url):
        return resolve_fb_watch(url)
    return url


# ============================================================
# YT-DLP OPTIONS CONFIGURATION
# ============================================================

def base_ydl_options(output_dir: str) -> dict:
    return {
        "outtmpl": os.path.join(
            output_dir,
            "%(title).180s [%(id)s].%(ext)s",
        ),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreconfig": True,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 5,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "continuedl": True,
        "overwrites": True,
        "restrictfilenames": False,
        "nocheckcertificate": False,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }


def apply_youtube_options(options: dict) -> dict:
    options["format"] = (
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
        "best[ext=mp4]/"
        "best"
    )
    options["merge_output_format"] = "mp4"

    # قراءة ملف الكوكيز تلقائياً في حال وجوده في المستودع
    cookie_file = Path("cookies.txt")
    if cookie_file.is_file() and cookie_file.stat().st_size > 0:
        options["cookiefile"] = str(cookie_file.resolve())
    else:
        # إعدادات تخطي فحص الروبوت عند عدم توفر ملف الكوكيز
        options["extractor_args"] = {
            "youtube": {
                "player_client": ["tvhtml5", "ios"],
                "player_skip": ["configs"],
            }
        }

    return options


def apply_audio_options(options: dict) -> dict:
    options["format"] = "bestaudio[ext=m4a]/bestaudio/best"
    options["postprocessors"] = [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "192",
    }]
    return options


def validate_clip(clip_start: float | None, clip_end: float | None):
    if clip_start is None:
        raise HTTPException(status_code=400, detail="clip_start is required.")
    if clip_end is None:
        raise HTTPException(status_code=400, detail="clip_end is required.")
    if clip_start < 0:
        raise HTTPException(status_code=400, detail="clip_start cannot be negative.")
    if clip_end <= clip_start:
        raise HTTPException(status_code=400, detail="clip_end must be greater than clip_start.")
    if clip_end - clip_start > MAX_CLIP_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"Clip cannot exceed {MAX_CLIP_SECONDS} seconds.",
        )


# ============================================================
# FILE HELPERS
# ============================================================

def cleanup_temp_directory(directory: str):
    try:
        if os.path.exists(directory):
            shutil.rmtree(directory, ignore_errors=True)
    except Exception:
        pass


def find_output_file(output_dir: str) -> Path | None:
    directory = Path(output_dir)
    if not directory.exists():
        return None

    files = [
        p
        for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() not in {".part", ".ytdl", ".temp"}
    ]
    if not files:
        return None

    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return files[0]


def check_output_size(path: Path):
    if not path.exists():
        return

    maximum = MAX_OUTPUT_SIZE_MB * 1024 * 1024
    if path.stat().st_size > maximum:
        try:
            path.unlink()
        except Exception:
            pass
        raise HTTPException(
            status_code=413,
            detail=(
                "Output file is too large. "
                f"Maximum allowed size is {MAX_OUTPUT_SIZE_MB} MB."
            ),
        )


# ============================================================
# DOWNLOAD ENGINE
# ============================================================

async def download_media(
    url: str,
    mode: str,
    clip_start: float | None = None,
    clip_end: float | None = None,
):
    url = normalize_url(url)
    output_dir = tempfile.mkdtemp(prefix="snapmega_")
    options = base_ydl_options(output_dir)

    youtube = is_youtube_url(url)

    if youtube:
        options = apply_youtube_options(options)
        if mode == "audio":
            options = apply_audio_options(options)

    elif is_facebook_url(url):
        url = (
            url
            .replace("www.facebook.com", "m.facebook.com")
            .replace("web.facebook.com", "m.facebook.com")
        )
        options["format"] = (
            "bestvideo*+bestaudio/best"
            if mode != "audio"
            else "bestaudio/best"
        )
        options["merge_output_format"] = "mp4"
        options["http_headers"].update({
            "Referer": "https://m.facebook.com/",
            "Origin": "https://m.facebook.com",
        })
        if mode == "audio":
            options = apply_audio_options(options)

    elif "kick.com" in url.lower():
        options["format"] = (
            "best[height<=480]/"
            "bestvideo[height<=480]+"
            "bestaudio/best"
        )
        options["merge_output_format"] = "mp4"
        if mode == "audio":
            options = apply_audio_options(options)

    else:
        if mode == "audio":
            options = apply_audio_options(options)
        else:
            options["format"] = (
                "bestvideo[ext=mp4]+"
                "bestaudio[ext=m4a]/"
                "best[ext=mp4]/"
                "best"
            )
            options["merge_output_format"] = "mp4"

    if mode == "clip":
        validate_clip(clip_start, clip_end)
        options["download_ranges"] = lambda info_dict, ydl: [{
            "start_time": clip_start,
            "end_time": clip_end,
        }]
        options["force_keyframes_at_cuts"] = True

    try:
        def run_download():
            with yt_dlp.YoutubeDL(options) as ydl:
                return ydl.extract_info(url, download=True)

        async with download_semaphore:
            info = await asyncio.wait_for(
                asyncio.to_thread(run_download),
                timeout=DOWNLOAD_TIMEOUT,
            )

    except asyncio.TimeoutError:
        cleanup_temp_directory(output_dir)
        raise HTTPException(
            status_code=504,
            detail="Download timed out. Please try again.",
        )

    except HTTPException:
        cleanup_temp_directory(output_dir)
        raise

    except Exception as exc:
        cleanup_temp_directory(output_dir)
        err = str(exc)
        err_lower = err.lower()

        if youtube:
            if "sign in to confirm" in err_lower or "not a bot" in err_lower:
                detail = (
                    "YouTube restricted this download on cloud servers. "
                    "Upload cookies.txt to your repository to unlock full YouTube access."
                )
            elif "login required" in err_lower or "private" in err_lower:
                detail = "This YouTube video requires login or is private."
            elif "age-restricted" in err_lower:
                detail = "This YouTube video is age restricted."
            else:
                detail = re.sub(
                    r"ERROR:\s*",
                    "",
                    err.splitlines()[0] if err else "YouTube download failed.",
                )

        elif "cannot parse data" in err_lower:
            detail = "Facebook layout changed or access was restricted. Please try another video."

        else:
            detail = re.sub(
                r"ERROR:\s*",
                "",
                err.splitlines()[0] if err else "Download failed.",
            )

        raise HTTPException(status_code=422, detail=detail)

    output_file = find_output_file(output_dir)
    if output_file is None:
        cleanup_temp_directory(output_dir)
        raise HTTPException(
            status_code=500,
            detail="Download completed but output file was not found.",
        )

    check_output_size(output_file)

    return {
        "path": str(output_file),
        "filename": output_file.name,
        "title": info.get("title") if info else None,
        "temp_dir": output_dir,
    }


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
async def root():
    return {
        "name": "SnapMega",
        "version": APP_VERSION,
        "status": "online",
        "has_cookies": Path("cookies.txt").exists(),
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "app": "SnapMega",
        "version": APP_VERSION,
        "has_cookies": Path("cookies.txt").exists(),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "yt_dlp": getattr(yt_dlp, "__version__", "unknown"),
        "limits": {
            "max_clip_seconds": MAX_CLIP_SECONDS,
            "max_concurrent_downloads": MAX_CONCURRENT_DOWNLOADS,
            "download_timeout": DOWNLOAD_TIMEOUT,
            "max_output_size_mb": MAX_OUTPUT_SIZE_MB,
        },
    }


@app.post("/api/download")
async def api_download(
    request: Request,
    payload: DownloadRequest,
    background_tasks: BackgroundTasks,
):
    url = payload.url.strip()

    if not url:
        raise HTTPException(status_code=400, detail="URL is required.")

    if len(url) > MAX_URL_LENGTH:
        raise HTTPException(status_code=400, detail="URL is too long.")

    if not is_allowed_url(url):
        raise HTTPException(status_code=400, detail="This URL is not supported.")

    client_ip = (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For")
        or (request.client.host if request.client else "unknown")
    )

    if "," in client_ip:
        client_ip = client_ip.split(",")[0].strip()

    check_rate_limit(client_ip)

    result = await download_media(
        url=url,
        mode=payload.mode,
        clip_start=payload.clip_start,
        clip_end=payload.clip_end,
    )

    file_path = Path(result["path"])

    # حذف المجلد المؤقت تلقائياً بعد اكتمال إرسال الملف
    background_tasks.add_task(
        cleanup_temp_directory,
        result["temp_dir"],
    )

    media_type = (
        "audio/mpeg"
        if payload.mode == "audio"
        else "video/mp4"
    )

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=result["filename"],
        background=background_tasks,
        headers={
            "Cache-Control": "no-store",
            "X-SnapMega-Version": APP_VERSION,
        },
    )


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
    )
