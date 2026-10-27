import os
import re
import time
import shutil
import tempfile
import asyncio
import subprocess
import requests

from pathlib import Path
from urllib.parse import urlparse
from collections import defaultdict, deque

import yt_dlp

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask


# =========================================================
# SNAPMEGA GLOBAL PRODUCTION API
# =========================================================

APP_VERSION = "5.5.0"

app = FastAPI(
    title="SnapMega API",
    version=APP_VERSION,
    description="Global video downloader and media processing API",
)

# =========================================================
# CONFIGURATION
# =========================================================

ALLOWED_ORIGINS = ["*"]

def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))

MAX_CLIP_SECONDS = env_int("MAX_CLIP_SECONDS", 120 * 60, 30, 4 * 60 * 60)
MAX_URL_LENGTH = env_int("MAX_URL_LENGTH", 4096, 100, 20000)
MAX_CONCURRENT_DOWNLOADS = env_int("MAX_CONCURRENT_DOWNLOADS", 3, 1, 10)
DOWNLOAD_TIMEOUT = env_int("DOWNLOAD_TIMEOUT", 900, 30, 3600)
RATE_LIMIT_REQUESTS = env_int("RATE_LIMIT_REQUESTS", 60, 1, 1000)
RATE_LIMIT_WINDOW = env_int("RATE_LIMIT_WINDOW", 60, 10, 3600)
MAX_OUTPUT_SIZE_MB = env_int("MAX_OUTPUT_SIZE_MB", 500, 10, 2048)
MAX_OUTPUT_SIZE_BYTES = MAX_OUTPUT_SIZE_MB * 1024 * 1024

# =========================================================
# SUPPORTED DOMAINS
# =========================================================

ALLOWED_ROOT_DOMAINS = {
    "tiktok.com",
    "instagram.com",
    "instagr.am",
    "facebook.com",
    "fb.com",
    "fb.watch",
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "kick.com",
}

def is_allowed_host(hostname: str) -> bool:
    if not hostname:
        return False
    hostname = hostname.lower().rstrip(".")
    for domain in ALLOWED_ROOT_DOMAINS:
        if hostname == domain or hostname.endswith("." + domain):
            return True
    return False

# =========================================================
# CORS
# =========================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Type", "Content-Length"],
    max_age=86400,
)

# =========================================================
# RATE LIMITING
# =========================================================

rate_limit_store = defaultdict(deque)
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

def get_client_ip(request: Request) -> str:
    if request.client:
        return request.client.host
    return "unknown"

def cleanup_rate_limit_store(now: float) -> None:
    expired_ips = []
    for ip, reqs in rate_limit_store.items():
        while reqs and (now - reqs[0] > RATE_LIMIT_WINDOW):
            reqs.popleft()
        if not reqs:
            expired_ips.append(ip)
    for ip in expired_ips:
        rate_limit_store.pop(ip, None)

def check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    if len(rate_limit_store) > 1000:
        cleanup_rate_limit_store(now)
    reqs = rate_limit_store[ip]
    while reqs and (now - reqs[0] > RATE_LIMIT_WINDOW):
        reqs.popleft()
    if len(reqs) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(RATE_LIMIT_WINDOW)},
        )
    reqs.append(now)

# =========================================================
# REQUEST MODEL
# =========================================================

class DownloadRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=MAX_URL_LENGTH)
    mode: str = Field(default="video", pattern=r"^(video|audio|clip)$")
    clip_start: str | None = Field(default=None, max_length=12)
    clip_end: str | None = Field(default=None, max_length=12)

# =========================================================
# URL NORMALIZATION & HELPERS
# =========================================================

def clean_target_url(raw_url: str) -> str:
    """تنظيف الرابط وإصلاح البادئة دون فك الروابط للحفاظ على الرابط الأصلي"""
    url = str(raw_url or "").strip()
    url = url.replace("[", "").replace("]", "")

    if url.startswith("tps://"):
        url = "h" + url
    elif url.startswith("ps://"):
        url = "htt" + url
    elif url.startswith("//"):
        url = "https:" + url
    elif not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    return url

def validate_url(raw_url: str) -> str:
    url = clean_target_url(raw_url)
    try:
        parsed = urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL structure.")

    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Invalid URL scheme.")

    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        raise HTTPException(status_code=400, detail="Invalid URL host.")

    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="Invalid URL credentials.")

    if not is_allowed_host(hostname):
        raise HTTPException(status_code=400, detail="This platform is not supported.")

    return url

def is_fb_watch_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        return host == "fb.watch" or host.endswith(".fb.watch")
    except Exception:
        return False

def is_facebook_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        return any(host == d or host.endswith("." + d) for d in ["facebook.com", "fb.com", "fb.watch"])
    except Exception:
        return False

def resolve_fb_watch_url(url: str) -> str | None:
    """فك مسار fb.watch مع حفظ الجلسة والكوكيز للحصول على الرابط المباشر"""
    if not is_fb_watch_url(url):
        return None

    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        resp = session.get(url, allow_redirects=True, timeout=12)
        final_url = str(resp.url)

        if "login" in final_url or "checkpoint" in final_url or "next=" in final_url:
            m = re.search(r"v%3D(\d+)", final_url) or re.search(r"videos%2F(\d+)", final_url)
            if m:
                return f"https://www.facebook.com/watch/?v={m.group(1)}"
            canon = re.search(r'<link\s+rel="canonical"\s+href="([^"]+)"', resp.text)
            if canon:
                return canon.group(1)

        return final_url
    except Exception:
        return None

# =========================================================
# TIME PARSER
# =========================================================

def parse_time(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None

    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)

    match = re.fullmatch(r"(\d{1,3}):(\d{1,2})", value)
    if match:
        minutes = int(match.group(1))
        seconds = int(match.group(2))
        if seconds >= 60:
            raise HTTPException(status_code=400, detail="Invalid time format.")
        return minutes * 60 + seconds

    match = re.fullmatch(r"(\d{1,2}):(\d{1,2}):(\d{1,2})", value)
    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        if minutes >= 60 or seconds >= 60:
            raise HTTPException(status_code=400, detail="Invalid time format.")
        return hours * 3600 + minutes * 60 + seconds

    raise HTTPException(status_code=400, detail="Invalid time format.")

# =========================================================
# FILE HELPERS
# =========================================================

def cleanup_dir(directory: str | None) -> None:
    if not directory:
        return
    try:
        shutil.rmtree(directory, ignore_errors=True)
    except Exception:
        pass

def find_downloaded_file(directory: str, extensions: tuple[str, ...]) -> Path | None:
    root = Path(directory)
    candidates = []
    for extension in extensions:
        candidates.extend(root.glob(f"*{extension}"))
    candidates = [f for f in candidates if f.is_file()]
    if not candidates:
        return None
    candidates.sort(key=lambda f: f.stat().st_size, reverse=True)
    return candidates[0]

def validate_output_file(file_path: Path) -> None:
    if not file_path.exists():
        raise RuntimeError("Download output was not created.")
    if not file_path.is_file():
        raise RuntimeError("Invalid download output.")
    size = file_path.stat().st_size
    if size < 5 * 1024:
        raise RuntimeError("Downloaded file is too small or corrupted.")
    if size > MAX_OUTPUT_SIZE_BYTES:
        raise RuntimeError("Output file exceeds the configured size limit.")

# =========================================================
# YT-DLP PRODUCTION CONFIGURATION
# =========================================================

def base_ydl_options(output_template: str) -> dict:
    return {
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreconfig": True,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 2,
        "nocheckcertificate": False,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    }

# =========================================================
# DOWNLOAD ENGINE (MULTI-STRATEGY)
# =========================================================

def download_video(url: str, directory: str) -> Path:
    output_template = str(Path(directory) / "snapmega_%(id)s.%(ext)s")
    candidates = []

    # تجهيز خطط التحميل (الرابط المحلول أولاً ثم الأصلي كـ Fallback)
    if is_fb_watch_url(url):
        resolved = resolve_fb_watch_url(url)
        if resolved and resolved != url:
            candidates.append(resolved)
    candidates.append(url)

    last_error = None
    for target in candidates:
        options = base_ydl_options(output_template)

        if is_facebook_url(target):
            options["format"] = "best/bestvideo+bestaudio"
            options["merge_output_format"] = "mp4"
            options["http_headers"].update({
                "Referer": "https://www.facebook.com/",
                "Origin": "https://www.facebook.com",
            })
        elif "kick.com" in target.lower():
            options["format"] = "best[height<=480]/bestvideo[height<=480]+bestaudio/best"
            options["merge_output_format"] = "mp4"
        else:
            options["format"] = "bestvideo*+bestaudio/best"
            options["merge_output_format"] = "mp4"

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(target, download=True)
                prepared = Path(ydl.prepare_filename(info))

            file_path = find_downloaded_file(directory, (".mp4", ".mkv", ".webm", ".mov", ".avi"))
            if file_path:
                return file_path
            if prepared.exists():
                return prepared
        except yt_dlp.utils.DownloadError as err:
            last_error = err
            continue

    if last_error:
        raise last_error

    raise RuntimeError("Could not locate downloaded video.")

def download_audio(url: str, directory: str) -> Path:
    output_template = str(Path(directory) / "snapmega_%(id)s.%(ext)s")
    candidates = []

    if is_fb_watch_url(url):
        resolved = resolve_fb_watch_url(url)
        if resolved and resolved != url:
            candidates.append(resolved)
    candidates.append(url)

    last_error = None
    for target in candidates:
        options = base_ydl_options(output_template)
        options.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })

        if is_facebook_url(target):
            options["http_headers"].update({
                "Referer": "https://www.facebook.com/",
                "Origin": "https://www.facebook.com",
            })

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.extract_info(target, download=True)

            file_path = find_downloaded_file(directory, (".mp3", ".m4a", ".aac", ".opus", ".wav"))
            if file_path:
                return file_path
        except yt_dlp.utils.DownloadError as err:
            last_error = err
            continue

    if last_error:
        raise last_error

    raise RuntimeError("Could not locate downloaded audio.")

def create_clip(url: str, directory: str, start: float, end: float) -> Path:
    duration = end - start
    if duration <= 0:
        raise HTTPException(status_code=400, detail="Clip end time must be greater than start time.")
    if duration > MAX_CLIP_SECONDS:
        raise HTTPException(status_code=400, detail=f"Maximum clip duration is {MAX_CLIP_SECONDS // 60} minutes.")
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("FFmpeg is not installed.")

    source_file = download_video(url, directory)
    clip_file = Path(directory) / "snapmega_clip.mp4"

    command = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", str(source_file),
        "-t", str(duration),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(clip_file),
    ]

    try:
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=True)
    except Exception:
        fallback_command = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", str(source_file),
            "-t", str(duration),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "28",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(clip_file),
        ]
        subprocess.run(fallback_command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300, check=True)

    if not clip_file.exists() or clip_file.stat().st_size < 1024:
        raise RuntimeError("Failed to create clip.")

    return clip_file

# =========================================================
# ROUTES
# =========================================================

@app.get("/")
async def root():
    return {"name": "SnapMega API", "version": APP_VERSION, "status": "online"}

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": APP_VERSION,
        "yt_dlp": getattr(yt_dlp, "__version__", "unknown"),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "max_concurrent_downloads": MAX_CONCURRENT_DOWNLOADS,
    }

@app.post("/api/download")
async def api_download(payload: DownloadRequest, request: Request):
    client_ip = get_client_ip(request)
    check_rate_limit(client_ip)
    url = validate_url(payload.url)

    if shutil.which("ffmpeg") is None:
        raise HTTPException(status_code=503, detail="FFmpeg is not installed.")

    start, end = None, None
    if payload.mode == "clip":
        start = parse_time(payload.clip_start)
        end = parse_time(payload.clip_end)
        if start is None or end is None:
            raise HTTPException(status_code=400, detail="Clip start and end times are required.")
        if start < 0 or end < 0 or end <= start:
            raise HTTPException(status_code=400, detail="Invalid clip time.")
        if end - start > MAX_CLIP_SECONDS:
            raise HTTPException(status_code=400, detail=f"Maximum clip duration is {MAX_CLIP_SECONDS // 60} minutes.")

    acquired = False
    temp_dir = None

    try:
        try:
            await asyncio.wait_for(download_semaphore.acquire(), timeout=10)
            acquired = True
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=503,
                detail="The download servers are busy. Please try again in a moment.",
                headers={"Retry-After": "10"},
            )

        temp_dir = tempfile.mkdtemp(prefix="snapmega_")

        if payload.mode == "video":
            file_path = await asyncio.wait_for(
                asyncio.to_thread(download_video, url, temp_dir),
                timeout=DOWNLOAD_TIMEOUT,
            )
        elif payload.mode == "audio":
            file_path = await asyncio.wait_for(
                asyncio.to_thread(download_audio, url, temp_dir),
                timeout=DOWNLOAD_TIMEOUT,
            )
        else:
            file_path = await asyncio.wait_for(
                asyncio.to_thread(create_clip, url, temp_dir, start, end),
                timeout=DOWNLOAD_TIMEOUT,
            )

        validate_output_file(file_path)

        filename = f"snapmega-{'audio.mp3' if payload.mode == 'audio' else ('clip.mp4' if payload.mode == 'clip' else 'video.mp4')}"
        media_type = "audio/mpeg" if payload.mode == "audio" else "video/mp4"

        return FileResponse(
            path=str(file_path),
            media_type=media_type,
            filename=filename,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            background=BackgroundTask(cleanup_dir, temp_dir),
        )

    except asyncio.TimeoutError:
        cleanup_dir(temp_dir)
        raise HTTPException(status_code=504, detail="The download took too long. Please try another video.")

    except yt_dlp.utils.DownloadError as err:
        cleanup_dir(temp_dir)
        error_text = str(err).strip()
        error_lower = error_text.lower()

        if "private" in error_lower:
            detail = "This video is private or requires login."
        elif "login" in error_lower:
            detail = "This video requires login to view."
        elif "not found" in error_lower or "unavailable" in error_lower:
            detail = "The video could not be found or is unavailable."
        elif "redirect loop" in error_lower or "302" in error_lower:
            detail = "Unable to follow video redirect. The media link may be expired or restricted."
        else:
            detail = re.sub(r"ERROR:\s*", "", error_text.splitlines()[0] if error_text else "Download failed.")

        raise HTTPException(status_code=422, detail=detail)

    except HTTPException:
        cleanup_dir(temp_dir)
        raise

    except Exception as exc:
        cleanup_dir(temp_dir)
        message = str(exc).strip() or "Download failed. Please try again."
        raise HTTPException(status_code=500, detail=message)

    finally:
        if acquired:
            download_semaphore.release()

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers or {})

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
