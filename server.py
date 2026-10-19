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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

ALLOWED_HOSTS = (
    "tiktok.com", "instagram.com", "facebook.com", "fb.watch",
    "youtube.com", "youtu.be", "youtube-nocookie.com"
)

semaphore = threading.BoundedSemaphore(2)

class DownloadRequest(BaseModel):
    url: HttpUrl
    mode: str = "video"
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
    suffix = ".mp3" if mode == "audio" else ".mp4"
    stem = Path(name).stem if Path(name).suffix else name
    return (stem[:80] + suffix)

@app.get("/health")
def health():
    return {"ok": True, "service": "snapmega-api"}

@app.post("/api/download")
def download_media(request: DownloadRequest):
    raw_url = validate_url(str(request.url))
    mode = request.mode.lower().strip()

    if not semaphore.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="السيرفر مشغول حالياً، يرجى الانتظار ثوانٍ")

    temp_dir = tempfile.mkdtemp(prefix="snapmega_")
    try:
        out_template = os.path.join(temp_dir, "%(id)s.%(ext)s")
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--no-warnings",
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "--referer", "https://www.tiktok.com/",
            "-o", out_template
        ]

        if mode == "audio":
            cmd += ["-x", "--audio-format", "mp3"]
        else:
            # تحميل أفضل فيديو مباشر مع الصوت مدمج بصيغة MP4 القياسية
            cmd += ["-f", "b[ext=mp4]/best[ext=mp4]/bestvideo+bestaudio/best"]

        cmd.append(raw_url)

        run = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)

        # البحث عن الملف الناتج
        files = [
            os.path.join(temp_dir, f)
            for f in os.listdir(temp_dir)
            if os.path.isfile(os.path.join(temp_dir, f)) and not f.endswith(".part")
        ]
        
        if not files:
            raise HTTPException(status_code=400, detail="تعذر استخراج الفيديو، تأكد أن الفيديو ليس خاصاً")

        target_file = files[0]
        
        # فحص حجم الملف؛ إذا كان أقل من 20 كيلوبايت فهو ملف تالف
        if os.path.getsize(target_file) < 20 * 1024:
            raise HTTPException(status_code=400, detail="الملف المستخرج تالف أو محمي من المصدر")

        final_filename = safe_filename(os.path.basename(target_file), mode)

        return FileResponse(
            path=target_file,
            filename=final_filename,
            media_type="audio/mpeg" if mode == "audio" else "video/mp4"
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="استغرق التحميل وقتاً طويلاً من الخادم")
    finally:
        semaphore.release()
