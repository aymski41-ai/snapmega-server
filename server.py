import os
import re
import tempfile
import subprocess
import threading
import urllib.request
import urllib.parse
import json
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

semaphore = threading.BoundedSemaphore(3)

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

def get_tiktok_direct(raw_url: str, mode: str, temp_dir: str):
    api_url = "https://www.tikwm.com/api/"
    data = urllib.parse.urlencode({"url": raw_url, "hd": 1}).encode("utf-8")
    req = urllib.request.Request(api_url, data=data, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    with urllib.request.urlopen(req, timeout=20) as response:
        res = json.loads(response.read().decode())
    
    if res.get("code") != 0 or "data" not in res:
        raise Exception("TikWM API returned error")

    vdata = res["data"]
    
    if mode == "audio":
        target_url = vdata.get("music")
        ext = ".mp3"
    else:
        # ضمان اختيار رابط الفيديو (play أو hdplay أو wmplay) وليس رابط الصوت (music)
        target_url = vdata.get("play") or vdata.get("hdplay") or vdata.get("wmplay")
        ext = ".mp4"

    if not target_url:
        raise Exception("No video download URL found")

    out_file = os.path.join(temp_dir, f"tiktok_{vdata.get('id', 'video')}{ext}")
    
    file_req = urllib.request.Request(target_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.tiktok.com/"
    })
    with urllib.request.urlopen(file_req, timeout=60) as dl, open(out_file, "wb") as f_out:
        f_out.write(dl.read())
        
    return out_file

@app.get("/health")
def health():
    return {"ok": True, "service": "snapmega-api"}

@app.post("/api/download")
def download_media(request: DownloadRequest):
    raw_url = validate_url(str(request.url))
    mode = request.mode.lower().strip()

    if not semaphore.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="السيرفر مشغول، أعد المحاولة بعد لحظات")

    temp_dir = tempfile.mkdtemp(prefix="snapmega_")
    try:
        target_file = None

        if "tiktok.com" in raw_url:
            try:
                target_file = get_tiktok_direct(raw_url, mode, temp_dir)
            except Exception as e:
                print("TikTok direct failed, fallback to yt-dlp:", e)

        if not target_file or not os.path.isfile(target_file):
            out_template = os.path.join(temp_dir, "%(id)s.%(ext)s")
            cmd = [
                "yt-dlp",
                "--no-playlist",
                "--no-warnings",
                "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "-o", out_template
            ]

            if mode == "audio":
                cmd += ["-x", "--audio-format", "mp3"]
            else:
                cmd += ["-f", "best[ext=mp4]/bestvideo+bestaudio/best"]

            cmd.append(raw_url)
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)

            files = [
                os.path.join(temp_dir, f)
                for f in os.listdir(temp_dir)
                if os.path.isfile(os.path.join(temp_dir, f)) and not f.endswith(".part")
            ]
            if files:
                target_file = files[0]

        if not target_file or not os.path.isfile(target_file):
            raise HTTPException(status_code=400, detail="تعذر استخراج الفيديو، تأكد من أن الرابط عام")

        if os.path.getsize(target_file) < 5 * 1024:
            raise HTTPException(status_code=400, detail="الملف المستخرج تالف أو محمي من المصدر")

        final_filename = safe_filename(os.path.basename(target_file), mode)
        return FileResponse(
            path=target_file,
            filename=final_filename,
            media_type="audio/mpeg" if mode == "audio" else "video/mp4"
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="حدث خطأ أثناء معالجة الطلب")
    finally:
        semaphore.release()
