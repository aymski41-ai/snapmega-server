import os
import re
import time
import shutil
import tempfile
import asyncio
import subprocess

from pathlib import Path
from urllib.parse import urlparse
from collections import defaultdict, deque

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    import requests as curl_requests

import yt_dlp

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask


APP_VERSION = "5.7.0"

app = FastAPI(
    title="SnapMega API",
    version=APP_VERSION,
    description="Global video downloader API",
)

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Type", "Content-Length"],
    max_age=86400,
)

rate_limit_store = defaultdict(deque)
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

def get_client_ip(request: Request) -> str:
    if request.client:
        return request.client.host
    return "unknown"

def check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    reqs = rate_limit_store[ip]
    while reqs and (now - reqs[0] > RATE_LIMIT_WINDOW):
        reqs.popleft()
    if len(reqs) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(RATE_LIMIT_WINDOW)},
        )
    requests_list = rate_limit_store[ip]
    requests_list.append(now)

class DownloadRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=MAX_URL_LENGTH)
    mode: str = Field(default="video", pattern=r"^(video|audio|clip)$")
    clip_start: str | None = Field(default=None, max_length=12)
    clip_end: str | None = Field(default=None, max_length=12)

def clean_target_url(raw_url: str) -> str:
    url = str(raw_url or "").strip().replace("[", "").replace("]", "")
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
    if not hostname or not is_allowed_host(hostname):
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
    if not is_fb_watch_url(url):
        return None
    try:
        session = curl_requests.Session(impersonate="chrome120")
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

def parse_time(value: str | None) -> float | None:
    if not value or not value.strip():
        return None
    val = value.strip()
    if re.fullmatch(r"\d+(\.\d+)?", val):
        return float(val)
    m = re.fullmatch(r"(\d{1,3}):(\d{1,2})", val)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None

def cleanup_dir(directory: str | None) -> None:
    if directory:
        try:
            shutil.rmtree(directory, ignore_errors=True)
        except Exception:
            pass

def find_downloaded_file(directory: str, extensions: tuple[str, ...]) -> Path | None:
    root = Path(directory)
    candidates = []
    for ext in extensions:
        candidates.extend(root.glob(f"*{ext}"))
    candidates = [f for f in candidates if f.is_file()]
    if not candidates:
        return None
    candidates.sort(key=lambda f: f.stat().st_size, reverse=True)
    return candidates[0]

def validate_output_file(file_path: Path) -> None:
    if not file_path.exists() or not file_path.is_file():
        raise RuntimeError("Download output was not created.")
    size = file_path.stat().st_size
    if size < 5 * 1024:
        raise RuntimeError("Downloaded file is too small.")
    if size > MAX_OUTPUT_SIZE_BYTES:
        raise RuntimeError("Output file exceeds the configured size limit.")

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
        "nocheckcertificate": False,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36 [FB_IAB/FB4A;FBAV/480.0.0.45.109;]",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "*/*",
        },
    }

def download_video(url: str, directory: str) -> Path:
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
        fallback = [
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
        subprocess.run(fallback, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300, check=True)

    if not clip_file.exists() or clip_file.stat().st_size < 1024:
        raise RuntimeError("Failed to create clip.")
    return clip_file

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
        start = parse_time(payload.clip_start) or 0.0
        end = parse_time(payload.clip_end) or 120.0
        if end <= start:
            raise HTTPException(status_code=400, detail="Invalid clip duration.")

    acquired = False
    temp_dir = None

    try:
        await asyncio.wait_for(download_semaphore.acquire(), timeout=10)
        acquired = True
        temp_dir = tempfile.mkdtemp(prefix="snapmega_")

        if payload.mode == "video":
            file_path = await asyncio.wait_for(asyncio.to_thread(download_video, url, temp_dir), timeout=DOWNLOAD_TIMEOUT)
        elif payload.mode == "audio":
            file_path = await asyncio.wait_for(asyncio.to_thread(download_audio, url, temp_dir), timeout=DOWNLOAD_TIMEOUT)
        else:
            file_path = await asyncio.wait_for(asyncio.to_thread(create_clip, url, temp_dir, start, end), timeout=DOWNLOAD_TIMEOUT)

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
        raise HTTPException(status_code=504, detail="The download took too long.")
    except yt_dlp.utils.DownloadError as err:
        cleanup_dir(temp_dir)
        err_lower = str(err).lower()
        if "private" in err_lower or "login" in err_lower:
            detail = "This video is private or requires login."
        elif "redirect loop" in err_lower or "302" in err_lower:
            detail = "Unable to follow video redirect. Please try another video."
        else:
            detail = re.sub(r"ERROR:\s*", "", str(err).splitlines()[0] if str(err) else "Download failed.")
        raise HTTPException(status_code=422, detail=detail)
    except Exception as exc:
        cleanup_dir(temp_dir)
        raise HTTPException(status_code=500, detail=str(exc) or "Download failed.")
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
