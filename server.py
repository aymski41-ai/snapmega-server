import os
import re
import tempfile
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl

app = FastAPI(title="SnapMega Download API", version="1.0.0")

# Set FRONTEND_ORIGIN to your published site, e.g. https://snapmega.pages.dev
allowed_origins = [x.strip() for x in os.getenv("FRONTEND_ORIGIN", "https://snapmega.pages.dev").split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type", "Accept"],
    expose_headers=["Content-Disposition"],
)

ALLOWED_HOSTS = (
    "tiktok.com", "instagram.com", "facebook.com", "fb.watch",
    "youtube.com", "youtu.be", "youtube-nocookie.com"
)
MAX_BYTES = int(os.getenv("MAX_DOWNLOAD_BYTES", str(500 * 1024 * 1024)))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "180"))
semaphore = threading.BoundedSemaphore(int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")))

class DownloadRequest(BaseModel):
    url: HttpUrl
    mode: str = "video"  # video | audio | clip
    clip_start: str = "00:10"
    clip_end: str = "01:00"


def validate_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in ("http", "https") or not host:
        raise HTTPException(status_code=400, detail="رابط غير صالح")
    if not any(host == domain or host.endswith("." + domain) for domain in ALLOWED_HOSTS):
        raise HTTPException(status_code=400, detail="هذه المنصة غير مدعومة")
    return raw_url


def safe_filename(name: str, mode: str) -> str:
    name = Path(name).name
    name = re.sub(r"[^\w .()\-\[\]]+", "_", name, flags=re.UNICODE).strip(" .")
    if not name:
        name = "snapmega_download"
    suffix = ".mp3" if mode == "audio" else (Path(name).suffix or ".mp4")
    stem = Path(name).stem if Path(name).suffix else name
    return (stem[:120] + suffix)[:150]

@app.get("/health")
def health():
    return {"ok": True, "service": "snapmega-api"}

@app.post("/api/download")
def download_media(request: DownloadRequest):
    raw_url = validate_url(str(request.url))
    mode = request.mode.lower().strip()
    if mode not in ("video", "audio", "clip"):
        raise HTTPException(status_code=400, detail="نوع التحميل غير مدعوم")

    def parse_time(value: str) -> int:
        parts = value.strip().split(":")
        if not 1 <= len(parts) <= 3 or any(not part.isdigit() for part in parts):
            raise HTTPException(status_code=400, detail="صيغة وقت القص غير صحيحة؛ استعمل MM:SS أو HH:MM:SS")
        nums = [int(part) for part in parts]
        if any(n < 0 or n > 59 for n in nums[1:]):
            raise HTTPException(status_code=400, detail="وقت القص غير صالح")
        total = 0
        for n in nums:
            total = total * 60 + n
        return total

    clip_start_seconds = clip_end_seconds = None
    if mode == "clip":
        clip_start_seconds = parse_time(request.clip_start)
        clip_end_seconds = parse_time(request.clip_end)
        if clip_end_seconds <= clip_start_seconds:
            raise HTTPException(status_code=400, detail="وقت النهاية لازم يكون بعد وقت البداية")

    if not semaphore.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="الخادم مشغول، عاود بعد قليل")

    temp_dir = tempfile.mkdtemp(prefix="snapmega-")
    try:
        output_template = os.path.join(temp_dir, "media.%(ext)s")
        command = [
            "yt-dlp", "--no-playlist", "--no-warnings", "--restrict-filenames",
            "--max-filesize", f"{MAX_BYTES // (1024 * 1024)}M",
            "--socket-timeout", "20", "--retries", "1", "--fragment-retries", "1",
            "--output", output_template,
        ]
        if mode == "audio":
            command += ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "5"]
        else:
            command += ["--format", "best[ext=mp4]/best"]
        command.append(raw_url)

        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="انتهت مهلة التحميل؛ جرّب رابطًا آخر")
        if result.returncode != 0:
            # Do not expose raw command output or server paths to visitors.
            raise HTTPException(status_code=422, detail="تعذر جلب هذا الفيديو. قد يكون خاصًا أو محميًا أو غير متاح حاليًا.")

        files = [p for p in Path(temp_dir).glob("*") if p.is_file()]
        if not files:
            raise HTTPException(status_code=422, detail="خدمة المنصة لم تُرجع ملفًا قابلًا للتنزيل")
        media_file = max(files, key=lambda p: p.stat().st_size)
        if media_file.stat().st_size > MAX_BYTES:
            raise HTTPException(status_code=413, detail="حجم الملف أكبر من الحد المسموح")
        if mode == "clip":
            clipped_file = Path(temp_dir) / "snapmega_clip.mp4"
            trim = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", str(clip_start_seconds), "-to", str(clip_end_seconds),
                "-i", str(media_file), "-c:v", "libx264", "-c:a", "aac",
                "-movflags", "+faststart", str(clipped_file)
            ], capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
            if trim.returncode != 0 or not clipped_file.exists() or clipped_file.stat().st_size == 0:
                raise HTTPException(status_code=422, detail="تعذر قص هذا الفيديو؛ جرّب أوقاتًا أقصر")
            media_file = clipped_file
        download_name = safe_filename(media_file.name.replace("media", "snapmega"), "audio" if mode == "audio" else "video")

        # FileResponse streams the result. A background cleanup removes it after response completion.
        from starlette.background import BackgroundTask
        import shutil
        return FileResponse(
            path=str(media_file),
            filename=download_name,
            media_type="audio/mpeg" if mode == "audio" else "video/mp4" if mode == "clip" else "application/octet-stream",
            background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
        )
    except HTTPException:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except Exception:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="حدث خطأ داخلي أثناء تجهيز الملف")
    finally:
        semaphore.release()
