import os
import re
import time
import asyncio
import shutil
import tempfile
import urllib.request
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
from fastapi.responses import FileResponse, JSONResponse


# ============================================================
# SNAPMEGA API
# ============================================================

APP_VERSION = "6.5.6"

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
        t
        for t in timestamps
        if now - t < RATE_LIMIT_WINDOW
    ]

    if len(timestamps) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
        )

    timestamps.append(now)


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

    return (
        hostname == "fb.watch"
        or hostname.endswith(".fb.watch")
    )


def is_facebook_url(url: str) -> bool:
    hostname = get_hostname(url)

    return any(
        hostname == domain
        or hostname.endswith("." + domain)
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
        return (
            "https://www.youtube.com/watch?v="
            + match.group(1)
        )

    match = re.search(
        r"youtu\.be/([^/?#]+)",
        url,
        re.IGNORECASE,
    )

    if match:
        return (
            "https://www.youtube.com/watch?v="
            + match.group(1)
        )

    return url


def resolve_fb_watch(url: str) -> str:
    try:
        session = curl_requests.Session(
            impersonate="chrome120"
        )

        resp = session.get(
            url,
            allow_redirects=True,
            timeout=12,
        )

        final_url = str(resp.url)

        if (
            "login" in final_url
            or "checkpoint" in final_url
            or "next=" in final_url
        ):
            m = (
                re.search(r"v%3D(\d+)", final_url)
                or re.search(r"videos%2F(\d+)", final_url)
            )

            if m:
                return (
                    "https://www.facebook.com/watch/?v="
                    + m.group(1)
                )

            canon = re.search(
                r'<link\s+rel="canonical"\s+href="([^"]+)"',
                resp.text,
            )

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
# COOKIES HELPER
# ============================================================

def get_valid_cookie_path() -> str | None:
    potential_paths = [
        Path("/app/cookies.txt"),
        Path("cookies.txt"),
        Path(__file__).resolve().parent / "cookies.txt",
    ]

    for p in potential_paths:
        try:
            if p.is_file() and p.stat().st_size > 0:
                return str(p.resolve())

        except OSError:
            continue

    return None


# ============================================================
# BGUTIL PO TOKEN PROVIDER
# ============================================================

def get_bgutil_provider_url() -> str:
    return os.getenv(
        "BGUTIL_POT_PROVIDER_URL",
        "http://127.0.0.1:4416",
    ).rstrip("/")


def check_bgutil_provider() -> bool:
    provider_url = get_bgutil_provider_url()

    try:
        with urllib.request.urlopen(
            f"{provider_url}/ping",
            timeout=3,
        ) as response:
            return response.status == 200

    except Exception:
        return False


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
    # ========================================================
    # YOUTUBE VIDEO FORMAT
    # ========================================================

    options["format"] = (
        "bestvideo*+bestaudio/best"
    )

    options["merge_output_format"] = "mp4"


    # ========================================================
    # YOUTUBE COOKIES
    # ========================================================

    cookie_path = get_valid_cookie_path()

    if cookie_path:
        options["cookiefile"] = cookie_path

        print(
            f"[SnapMega] Active YouTube cookies from: "
            f"{cookie_path}",
            flush=True,
        )

    else:
        print(
            "[SnapMega] WARNING: YouTube cookies.txt not found.",
            flush=True,
        )


    # ========================================================
    # YOUTUBE + BGUTIL PO TOKEN
    #
    # mweb is the primary client.
    # tv and web_safari are fallbacks.
    # ========================================================

    options["extractor_args"] = {
        "youtube": {
            "player_client": [
                "mweb",
                "tv",
                "web_safari",
            ],
        },

        "youtubepot-bgutilhttp": {
            "base_url": get_bgutil_provider_url(),
        },
    }


    # ========================================================
    # YOUTUBE DEBUG LOGGING
    #
    # This is intentionally enabled only for YouTube.
    # It lets Render logs show whether BgUtils actually
    # generates the PO Token.
    # ========================================================

    options["quiet"] = False

    options["no_warnings"] = False

    options["verbose"] = True


    # ========================================================
    # DEBUG INFORMATION
    # ========================================================

    print(
        "[SnapMega] YouTube clients: "
        "mweb,tv,web_safari",
        flush=True,
    )

    print(
        "[SnapMega] BgUtils URL: "
        + get_bgutil_provider_url(),
        flush=True,
    )

    print(
        "[SnapMega] BgUtils status: "
        + (
            "ONLINE"
            if check_bgutil_provider()
            else "OFFLINE"
        ),
        flush=True,
    )

    return options


def apply_audio_options(options: dict) -> dict:
    options["format"] = "bestaudio/best"

    options["postprocessors"] = [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "192",
    }]

    return options


def parse_float_safe(val) -> float | None:
    if val is None:
        return None

    try:
        s = str(val).strip()

        if not s:
            return None

        return float(s)

    except Exception:
        return None


# ============================================================
# FILE HELPERS
# ============================================================

def cleanup_temp_directory(directory: str):
    try:
        if os.path.exists(directory):
            shutil.rmtree(
                directory,
                ignore_errors=True,
            )

    except Exception:
        pass


def find_output_file(output_dir: str) -> Path | None:
    directory = Path(output_dir)

    if not directory.exists():
        return None

    files = [
        p
        for p in directory.rglob("*")
        if (
            p.is_file()
            and p.suffix.lower()
            not in {
                ".part",
                ".ytdl",
                ".temp",
            }
        )
    ]

    if not files:
        return None

    files.sort(
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    return files[0]


def check_output_size(path: Path):
    if not path.exists():
        return

    maximum = (
        MAX_OUTPUT_SIZE_MB
        * 1024
        * 1024
    )

    if path.stat().st_size > maximum:
        try:
            path.unlink()

        except Exception:
            pass

        raise HTTPException(
            status_code=413,
            detail=(
                "Output file is too large. "
                f"Maximum allowed size is "
                f"{MAX_OUTPUT_SIZE_MB} MB."
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

    output_dir = tempfile.mkdtemp(
        prefix="snapmega_"
    )

    options = base_ydl_options(
        output_dir
    )

    youtube = is_youtube_url(url)


    # ========================================================
    # YOUTUBE
    # ========================================================

    if youtube:

        options = apply_youtube_options(
            options
        )

        if mode == "audio":
            options = apply_audio_options(
                options
            )


    # ========================================================
    # FACEBOOK
    # ========================================================

    elif is_facebook_url(url):

        url = (
            url
            .replace(
                "www.facebook.com",
                "m.facebook.com",
            )
            .replace(
                "web.facebook.com",
                "m.facebook.com",
            )
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
            options = apply_audio_options(
                options
            )


    # ========================================================
    # KICK
    # ========================================================

    elif "kick.com" in url.lower():

        options["format"] = (
            "best[height<=480]/"
            "bestvideo[height<=480]+"
            "bestaudio/best"
        )

        options["merge_output_format"] = "mp4"

        if mode == "audio":
            options = apply_audio_options(
                options
            )


    # ========================================================
    # OTHER PLATFORMS
    # ========================================================

    else:

        if mode == "audio":
            options = apply_audio_options(
                options
            )

        else:
            options["format"] = (
                "bestvideo*+bestaudio/best"
            )

            options["merge_output_format"] = "mp4"


    # ========================================================
    # CLIP
    # ========================================================

    if (
        mode == "clip"
        and clip_start is not None
        and clip_end is not None
    ):
        if clip_end > clip_start:

            options["download_ranges"] = (
                lambda info_dict, ydl: [{
                    "start_time": clip_start,
                    "end_time": clip_end,
                }]
            )

            options["force_keyframes_at_cuts"] = True


    # ========================================================
    # DOWNLOAD
    # ========================================================

    try:

        def run_download():

            with yt_dlp.YoutubeDL(
                options
            ) as ydl:

                return ydl.extract_info(
                    url,
                    download=True,
                )


        async with download_semaphore:

            info = await asyncio.wait_for(
                asyncio.to_thread(
                    run_download
                ),
                timeout=DOWNLOAD_TIMEOUT,
            )


    # ========================================================
    # TIMEOUT
    # ========================================================

    except asyncio.TimeoutError:

        cleanup_temp_directory(
            output_dir
        )

        raise HTTPException(
            status_code=504,
            detail=(
                "Download timed out. "
                "Please try again."
            ),
        )


    # ========================================================
    # HTTP EXCEPTION
    # ========================================================

    except HTTPException:

        cleanup_temp_directory(
            output_dir
        )

        raise


    # ========================================================
    # GENERAL ERROR
    # ========================================================

    except Exception as exc:

        cleanup_temp_directory(
            output_dir
        )

        err = str(exc)

        err_lower = err.lower()


        # ====================================================
        # YOUTUBE ERROR HANDLING
        # ====================================================

        if youtube:

            if (
                "sign in to confirm" in err_lower
                or "not a bot" in err_lower
            ):

                if check_bgutil_provider():

                    detail = (
                        "YouTube rejected the Render server "
                        "request. BgUtils is online, but "
                        "YouTube is still requiring "
                        "authentication/bot verification. "
                        "Check the Render logs for "
                        "'Generating a gvs PO Token'. "
                        "If a PO Token was generated and "
                        "this message remains, the Render "
                        "server IP may be restricted by "
                        "YouTube."
                    )

                else:

                    detail = (
                        "YouTube rejected the request and "
                        "the BgUtils PO Token Provider is "
                        "not reachable. Please verify "
                        "that BgUtils is running on "
                        "127.0.0.1:4416."
                    )


            elif (
                "login required" in err_lower
                or "private" in err_lower
            ):

                detail = (
                    "This YouTube video requires login "
                    "or is private."
                )


            elif "age-restricted" in err_lower:

                detail = (
                    "This YouTube video is age restricted."
                )


            else:

                first_line = (
                    err.splitlines()[0]
                    if err
                    else "YouTube download failed."
                )

                detail = re.sub(
                    r"ERROR:\s*",
                    "",
                    first_line,
                )


        # ====================================================
        # FACEBOOK
        # ====================================================

        elif "cannot parse data" in err_lower:

            detail = (
                "Facebook layout changed or access was "
                "restricted. Please try another video."
            )


        # ====================================================
        # GENERAL
        # ====================================================

        else:

            first_line = (
                err.splitlines()[0]
                if err
                else "Download failed."
            )

            detail = re.sub(
                r"ERROR:\s*",
                "",
                first_line,
            )


        raise HTTPException(
            status_code=400,
            detail=detail,
        )


    # ========================================================
    # FIND OUTPUT
    # ========================================================

    output_file = find_output_file(
        output_dir
    )

    if output_file is None:

        cleanup_temp_directory(
            output_dir
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "Download completed but output "
                "file was not found."
            ),
        )


    # ========================================================
    # SIZE CHECK
    # ========================================================

    check_output_size(
        output_file
    )


    # ========================================================
    # RESULT
    # ========================================================

    return {
        "path": str(output_file),

        "filename": output_file.name,

        "title": (
            info.get("title")
            if info
            else None
        ),

        "temp_dir": output_dir,
    }


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
async def root():

    cookie_path = get_valid_cookie_path()

    return {
        "name": "SnapMega",

        "version": APP_VERSION,

        "status": "online",

        "has_cookies": (
            cookie_path is not None
        ),

        "cookie_path": cookie_path,

        "bgutil_pot_provider": {
            "url": get_bgutil_provider_url(),
            "online": check_bgutil_provider(),
        },
    }


@app.get("/health")
async def health():

    cookie_path = get_valid_cookie_path()

    return {
        "status": "ok",

        "app": "SnapMega",

        "version": APP_VERSION,

        "has_cookies": (
            cookie_path is not None
        ),

        "cookie_path": cookie_path,

        "ffmpeg": (
            shutil.which("ffmpeg")
            is not None
        ),

        "yt_dlp": getattr(
            yt_dlp,
            "__version__",
            "unknown",
        ),

        "youtube": {
            "enabled": True,

            "player_clients": [
                "mweb",
                "tv",
                "web_safari",
            ],

            "po_token_provider": (
                check_bgutil_provider()
            ),

            "po_token_provider_url": (
                get_bgutil_provider_url()
            ),
        },

        "limits": {
            "max_clip_seconds": (
                MAX_CLIP_SECONDS
            ),

            "max_concurrent_downloads": (
                MAX_CONCURRENT_DOWNLOADS
            ),

            "download_timeout": (
                DOWNLOAD_TIMEOUT
            ),

            "max_output_size_mb": (
                MAX_OUTPUT_SIZE_MB
            ),
        },
    }


# ============================================================
# DOWNLOAD ROUTE
# ============================================================

@app.post("/api/download")
async def api_download(
    request: Request,
    background_tasks: BackgroundTasks,
):

    try:

        body = await request.json()

    except Exception:

        raise HTTPException(
            status_code=400,
            detail="Invalid JSON format.",
        )


    raw_url = (
        body.get("url")
        or body.get("link")
        or body.get("videoUrl")
        or ""
    )

    url = str(raw_url).strip()


    if not url:

        raise HTTPException(
            status_code=400,
            detail="URL is required.",
        )


    if len(url) > MAX_URL_LENGTH:

        raise HTTPException(
            status_code=400,
            detail="URL is too long.",
        )


    if not is_allowed_url(url):

        raise HTTPException(
            status_code=400,
            detail="This URL is not supported.",
        )


    # ========================================================
    # CLIENT IP
    # ========================================================

    client_ip = (
        request.headers.get(
            "CF-Connecting-IP"
        )
        or request.headers.get(
            "X-Forwarded-For"
        )
        or (
            request.client.host
            if request.client
            else "unknown"
        )
    )


    if "," in client_ip:

        client_ip = (
            client_ip
            .split(",")[0]
            .strip()
        )


    check_rate_limit(
        client_ip
    )


    # ========================================================
    # MODE / CLIP
    # ========================================================

    mode = str(
        body.get("mode")
        or "video"
    ).lower()


    start_val = parse_float_safe(
        body.get("clip_start")
        or body.get("start")
    )


    end_val = parse_float_safe(
        body.get("clip_end")
        or body.get("end")
    )


    # ========================================================
    # DOWNLOAD
    # ========================================================

    result = await download_media(
        url=url,

        mode=mode,

        clip_start=start_val,

        clip_end=end_val,
    )


    file_path = Path(
        result["path"]
    )


    # ========================================================
    # CLEANUP
    # ========================================================

    background_tasks.add_task(
        cleanup_temp_directory,
        result["temp_dir"],
    )


    # ========================================================
    # MEDIA TYPE
    # ========================================================

    media_type = (
        "audio/mpeg"
        if mode == "audio"
        else "video/mp4"
    )


    # ========================================================
    # RESPONSE
    # ========================================================

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
# HTTP EXCEPTION HANDLER
# ============================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(
    request: Request,
    exc: HTTPException,
):

    return JSONResponse(
        status_code=exc.status_code,

        content={
            "detail": str(
                exc.detail
            )
        },
    )


# ============================================================
# START SERVER
# ============================================================

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
