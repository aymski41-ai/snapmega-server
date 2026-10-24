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

import yt_dlp

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask


# =========================================================
# SNAPMEGA GLOBAL PRODUCTION API
# =========================================================

APP_VERSION = "4.2.0"

app = FastAPI(
    title="SnapMega API",
    version=APP_VERSION,
    description="Global video downloader API",
)


# =========================================================
# CONFIGURATION
# =========================================================

FRONTEND_ORIGINS_RAW = os.getenv(
    "FRONTEND_ORIGINS",
    "[https://snapmega.pages.dev](https://snapmega.pages.dev)",
)

ALLOWED_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in FRONTEND_ORIGINS_RAW.split(",")
    if origin.strip()
]

# Always allow the main Cloudflare Pages domain.
if "[https://snapmega.pages.dev](https://snapmega.pages.dev)" not in ALLOWED_ORIGINS:
    ALLOWED_ORIGINS.append("[https://snapmega.pages.dev](https://snapmega.pages.dev)")


def env_int(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.getenv(name, str(default)).strip()

    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default

    return max(minimum, min(value, maximum))


MAX_CLIP_SECONDS = 15 * 60
MAX_URL_LENGTH = 4096

MAX_CONCURRENT_DOWNLOADS = env_int(
    "MAX_CONCURRENT_DOWNLOADS",
    2,
    1,
    10,
)

DOWNLOAD_TIMEOUT = env_int(
    "DOWNLOAD_TIMEOUT",
    300,
    30,
    1800,
)

RATE_LIMIT_REQUESTS = env_int(
    "RATE_LIMIT_REQUESTS",
    10,
    1,
    1000,
)

RATE_LIMIT_WINDOW = env_int(
    "RATE_LIMIT_WINDOW",
    60,
    10,
    3600,
)

MAX_OUTPUT_SIZE_MB = env_int(
    "MAX_OUTPUT_SIZE_MB",
    500,
    10,
    2048,
)

MAX_OUTPUT_SIZE_BYTES = (
    MAX_OUTPUT_SIZE_MB * 1024 * 1024
)


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
}


def is_allowed_host(hostname: str) -> bool:
    hostname = hostname.lower().rstrip(".")

    for domain in ALLOWED_ROOT_DOMAINS:
        if hostname == domain:
            return True
        if hostname.endswith("." + domain):
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
    expose_headers=["Content-Disposition"],
    max_age=86400,
)


# =========================================================
# RATE LIMITING
# =========================================================

rate_limit_store = defaultdict(deque)

download_semaphore = asyncio.Semaphore(
    MAX_CONCURRENT_DOWNLOADS
)


def get_client_ip(request: Request) -> str:
    if request.client:
        return request.client.host

    return "unknown"


def cleanup_rate_limit_store(now: float) -> None:
    expired_ips = []

    for ip, requests in rate_limit_store.items():
        while requests and (
            now - requests[0] > RATE_LIMIT_WINDOW
        ):
            requests.popleft()

        if not requests:
            expired_ips.append(ip)

    for ip in expired_ips:
        rate_limit_store.pop(ip, None)


def check_rate_limit(ip: str) -> None:
    now = time.monotonic()

    if len(rate_limit_store) > 1000:
        cleanup_rate_limit_store(now)

    requests = rate_limit_store[ip]

    while requests and (
        now - requests[0] > RATE_LIMIT_WINDOW
    ):
        requests.popleft()

    if len(requests) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail=(
                "Too many requests. "
                "Please try again later."
            ),
            headers={
                "Retry-After": str(
                    RATE_LIMIT_WINDOW
                )
            },
        )

    requests.append(now)


# =========================================================
# REQUEST MODEL
# =========================================================

class DownloadRequest(BaseModel):
    url: str = Field(
        ...,
        min_length=4,
        max_length=MAX_URL_LENGTH,
    )

    mode: str = Field(
        default="video",
        pattern=r"^(video|audio|clip)$",
    )

    clip_start: str | None = Field(
        default=None,
        max_length=12,
    )

    clip_end: str | None = Field(
        default=None,
        max_length=12,
    )


# =========================================================
# URL HELPERS
# =========================================================

def clean_target_url(url: str) -> str:
    url = url.strip()

    if url.startswith("tps://"):
        url = "h" + url

    elif url.startswith("ps://"):
        url = "htt" + url

    elif url.startswith("//"):
        url = "https:" + url

    elif not re.match(
        r"^https?://",
        url,
        re.I,
    ):
        url = "https://" + url

    return url


def validate_url(url: str) -> str:
    url = clean_target_url(url)

    parsed = urlparse(url)

    if parsed.scheme not in {
        "http",
        "https",
    }:
        raise HTTPException(
            status_code=400,
            detail="Invalid URL scheme.",
        )

    if not parsed.hostname:
        raise HTTPException(
            status_code=400,
            detail="Invalid URL.",
        )

    hostname = (
        parsed.hostname
        .lower()
        .rstrip(".")
    )

    if parsed.username or parsed.password:
        raise HTTPException(
            status_code=400,
            detail="Invalid URL.",
        )

    if not is_allowed_host(hostname):
        raise HTTPException(
            status_code=400,
            detail="This platform is not supported.",
        )

    return url


# =========================================================
# TIME PARSER
# =========================================================

def parse_time(
    value: str | None,
) -> float | None:
    if value is None:
        return None

    value = value.strip()

    if not value:
        return None

    if re.fullmatch(
        r"\d+(\.\d+)?",
        value,
    ):
        return float(value)

    match = re.fullmatch(
        r"(\d{1,3}):(\d{1,2})",
        value,
    )

    if match:
        minutes = int(match.group(1))
        seconds = int(match.group(2))

        if seconds >= 60:
            raise HTTPException(
                status_code=400,
                detail="Invalid time format.",
            )

        return (
            minutes * 60
            + seconds
        )

    match = re.fullmatch(
        r"(\d{1,2}):(\d{1,2}):(\d{1,2})",
        value,
    )

    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = int(match.group(3))

        if (
            minutes >= 60
            or seconds >= 60
        ):
            raise HTTPException(
                status_code=400,
                detail="Invalid time format.",
            )

        return (
            hours * 3600
            + minutes * 60
            + seconds
        )

    raise HTTPException(
        status_code=400,
        detail="Invalid time format.",
    )


# =========================================================
# FILE HELPERS
# =========================================================

def cleanup_dir(directory: str) -> None:
    try:
        shutil.rmtree(
            directory,
            ignore_errors=True,
        )
    except Exception:
        pass


def find_downloaded_file(
    directory: str,
    extensions: tuple[str, ...],
) -> Path | None:
    root = Path(directory)
    candidates = []

    for extension in extensions:
        candidates.extend(
            root.glob(f"*{extension}")
        )

    candidates = [
        file
        for file in candidates
        if file.is_file()
    ]

    if not candidates:
        return None

    candidates.sort(
        key=lambda file: file.stat().st_size,
        reverse=True,
    )

    return candidates[0]


def validate_output_file(
    file_path: Path,
) -> None:
    if not file_path.exists():
        raise RuntimeError(
            "Download output was not created."
        )

    if not file_path.is_file():
        raise RuntimeError(
            "Invalid download output."
        )

    size = file_path.stat().st_size

    if size < 5 * 1024:
        raise RuntimeError(
            "Downloaded file is too small."
        )

    if size > MAX_OUTPUT_SIZE_BYTES:
        raise RuntimeError(
            "Output file exceeds the configured size limit."
        )


# =========================================================
# YT-DLP OPTIONS
# =========================================================

def base_ydl_options(
    output_template: str,
) -> dict:
    return {
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreconfig": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 2,
        "nocheckcertificate": False,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 "
                "Safari/537.36"
            ),
            "Accept-Language": (
                "en-US,en;q=0.9"
            ),
        },
    }


# =========================================================
# VIDEO
# =========================================================

def download_video(
    url: str,
    directory: str,
) -> Path:
    output_template = str(
        Path(directory)
        / "snapmega_%(id)s.%(ext)s"
    )

    options = base_ydl_options(
        output_template
    )

    options.update({
        "format": "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
        "merge_output_format": "mp4",
        "extractor_args": {
            "youtube": {
                "player_client": ["tvhtml5", "android", "ios"]
            }
        },
    })

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            url,
            download=True,
        )

        prepared = Path(
            ydl.prepare_filename(info)
        )

    file_path = find_downloaded_file(
        directory,
        (
            ".mp4",
            ".mkv",
            ".webm",
            ".mov",
            ".avi",
        ),
    )

    if file_path:
        return file_path

    if prepared.exists():
        return prepared

    raise RuntimeError(
        "Could not locate downloaded video."
    )

# =========================================================
# AUDIO
# =========================================================

def download_audio(
    url: str,
    directory: str,
) -> Path:
    output_template = str(
        Path(directory)
        / "snapmega_%(id)s.%(ext)s"
    )

    options = base_ydl_options(
        output_template
    )

    options.update({
        "format": "bestaudio/best",
        "extractor_args": {
            "youtube": {
                "player_client": ["tvhtml5", "android", "ios"]
            }
        },
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    })

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.extract_info(
            url,
            download=True,
        )

    file_path = find_downloaded_file(
        directory,
        (
            ".mp3",
            ".m4a",
            ".aac",
            ".opus",
            ".wav",
        ),
    )

    if not file_path:
        raise RuntimeError(
            "Could not locate downloaded audio."
        )

    return file_path


# =========================================================
# CLIP
# =========================================================

def create_clip(
    url: str,
    directory: str,
    start: float,
    end: float,
) -> Path:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "FFmpeg is not installed."
        )

    source_file = download_video(
        url,
        directory,
    )

    clip_file = (
        Path(directory)
        / "snapmega_clip.mp4"
    )

    duration = end - start

    if duration <= 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "Clip end time must be "
                "greater than start time."
            ),
        )

    command = [
        "ffmpeg",
        "-y",
        "-ss",
        str(start),
        "-i",
        str(source_file),
        "-t",
        str(duration),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(clip_file),
    ]

    try:
        subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=DOWNLOAD_TIMEOUT,
            check=True,
        )

    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "Clip processing timed out."
        )

    except subprocess.CalledProcessError:
        raise RuntimeError(
            "Could not create clip."
        )

    return clip_file


# =========================================================
# HEALTH
# =========================================================

@app.get("/")
async def root():
    return {
        "name": "SnapMega API",
        "version": APP_VERSION,
        "status": "online",
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": APP_VERSION,
        "yt_dlp": hasattr(
            yt_dlp,
            "YoutubeDL",
        ),
        "ffmpeg": (
            shutil.which("ffmpeg")
            is not None
        ),
        "ffprobe": (
            shutil.which("ffprobe")
            is not None
        ),
        "max_concurrent_downloads": (
            MAX_CONCURRENT_DOWNLOADS
        ),
        "max_output_mb": (
            MAX_OUTPUT_SIZE_MB
        ),
        "rate_limit_requests": (
            RATE_LIMIT_REQUESTS
        ),
        "rate_limit_window": (
            RATE_LIMIT_WINDOW
        ),
        "allowed_origins": ALLOWED_ORIGINS,
    }


# =========================================================
# DOWNLOAD API
# =========================================================

@app.post("/api/download")
async def api_download(
    payload: DownloadRequest,
    request: Request,
):
    client_ip = get_client_ip(request)

    check_rate_limit(client_ip)

    url = validate_url(
        payload.url
    )

    if shutil.which("ffmpeg") is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Download service is "
                "temporarily unavailable."
            ),
        )

    start = None
    end = None

    if payload.mode == "clip":
        start = parse_time(
            payload.clip_start
        )

        end = parse_time(
            payload.clip_end
        )

        if (
            start is None
            or end is None
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Clip start and end "
                    "times are required."
                ),
            )

        if start < 0 or end < 0:
            raise HTTPException(
                status_code=400,
                detail="Invalid clip time.",
            )

        if end <= start:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Clip end must be "
                    "greater than start."
                ),
            )

        if (
            end - start
            > MAX_CLIP_SECONDS
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Maximum clip duration "
                    "is 15 minutes."
                ),
            )

    acquired = False
    temp_dir = None

    try:
        try:
            await asyncio.wait_for(
                download_semaphore.acquire(),
                timeout=10,
            )

            acquired = True

        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=503,
                detail=(
                    "The download servers "
                    "are busy. Please try "
                    "again in a moment."
                ),
                headers={
                    "Retry-After": "10"
                },
            )

        temp_dir = tempfile.mkdtemp(
            prefix="snapmega_"
        )

        try:
            if payload.mode == "video":
                file_path = (
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            download_video,
                            url,
                            temp_dir,
                        ),
                        timeout=DOWNLOAD_TIMEOUT,
                    )
                )

            elif payload.mode == "audio":
                file_path = (
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            download_audio,
                            url,
                            temp_dir,
                        ),
                        timeout=DOWNLOAD_TIMEOUT,
                    )
                )

            else:
                file_path = (
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            create_clip,
                            url,
                            temp_dir,
                            start,
                            end,
                        ),
                        timeout=DOWNLOAD_TIMEOUT,
                    )
                )

            validate_output_file(
                file_path
            )

            if payload.mode == "audio":
                filename = (
                    "snapmega-audio.mp3"
                )
                media_type = (
                    "audio/mpeg"
                )

            elif payload.mode == "clip":
                filename = (
                    "snapmega-clip.mp4"
                )
                media_type = (
                    "video/mp4"
                )

            else:
                filename = (
                    "snapmega-video.mp4"
                )
                media_type = (
                    "video/mp4"
                )

            return FileResponse(
                path=str(file_path),
                media_type=media_type,
                filename=filename,
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": (
                        "nosniff"
                    ),
                },
                background=BackgroundTask(
                    cleanup_dir,
                    temp_dir,
                ),
            )

        except asyncio.TimeoutError:
            cleanup_dir(temp_dir)

            raise HTTPException(
                status_code=504,
                detail=(
                    "The download took too long. "
                    "Please try another video."
                ),
            )

        except HTTPException:
            cleanup_dir(temp_dir)
            raise

        except yt_dlp.utils.DownloadError:
            cleanup_dir(temp_dir)

            raise HTTPException(
                status_code=422,
                detail=(
                    "This video could not be "
                    "downloaded. It may be private, "
                    "unavailable, restricted, "
                    "or unsupported."
                ),
            )

        except Exception:
            cleanup_dir(temp_dir)

            raise HTTPException(
                status_code=500,
                detail=(
                    "Download failed. "
                    "Please try again later."
                ),
            )

    finally:
        if acquired:
            download_semaphore.release()


# =========================================================
# HTTP ERROR HANDLER
# =========================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(
    request: Request,
    exc: HTTPException,
):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail
        },
        headers=exc.headers or {},
    )


# =========================================================
# GLOBAL UNEXPECTED ERROR HANDLER
# =========================================================

@app.exception_handler(Exception)
async def global_exception_handler(
    request: Request,
    exc: Exception,
):
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error."
        },
    )


# =========================================================
# LOCAL DEVELOPMENT
# =========================================================

if __name__ == "__main__":
    import uvicorn

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
    )
