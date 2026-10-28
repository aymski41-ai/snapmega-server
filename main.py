import os
import re
import tempfile
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="SnapMega Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class DownloadRequest(BaseModel):
    url: str
    mode: str = "video"  # "video", "audio", "clip"
    clip_start: str = None
    clip_end: str = None

def time_to_seconds(t_str: str) -> float:
    if not t_str:
        return 0.0
    parts = list(map(float, t_str.split(":")))
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    elif len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0]

def apply_youtube_options(options: dict) -> dict:
    options["format"] = "18/best[height<=360]/worst[ext=mp4]/worst"
    options["merge_output_format"] = "mp4"
    options["noplaylist"] = True
    options["socket_timeout"] = 30

    # ربط ملف الكوكيز مباشرة
    cookie_path = "cookies.txt"
    if os.path.exists(cookie_path):
        options["cookiefile"] = cookie_path

    # تفعيل مشغل الويب المتوافق مع الكوكيز ومولد الـ PO Token المحلي
    options["extractor_args"] = {
        "youtube": {
            "player_client": ["web", "mweb"]
        }
    }

    return options

@app.get("/")
@app.get("/health")
def health_check():
    return {"status": "ok", "message": "SnapMega server is alive"}

@app.post("/api/download")
def download_media(req: DownloadRequest):
    url = req.url.strip()
    mode = req.mode
    clip_start = req.clip_start
    clip_end = req.clip_end

    output_dir = tempfile.gettempdir()
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")

    options = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 15,
    }

    # 1. إعدادات YouTube
    if "youtube.com" in url.lower() or "youtu.be" in url.lower():
        options = apply_youtube_options(options)

    # 2. إعدادات Kick السريعة وحماية البث المباشر
    elif "kick.com" in url.lower():
        url = url.split("?")[0].rstrip("/")
        options["format"] = "worst[ext=mp4]/best[height<=360]/worst"
        options["merge_output_format"] = "mp4"
        options["concurrent_fragment_downloads"] = 10
        options["hls_prefer_native"] = True
        options["playlist_items"] = "1"
        options["live_from_start"] = False
        options["http_headers"] = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://kick.com/",
            "Origin": "https://kick.com",
        }

    # 3. إعدادات استخراج الصوت فقط
    if mode == "audio":
        options["format"] = "bestaudio/best"
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }]

    # 4. القص الفوري والسريع دون تحميل الفيديو كاملاً
    if (mode == "clip" or clip_start or clip_end) and clip_start and clip_end:
        start_sec = time_to_seconds(clip_start)
        end_sec = time_to_seconds(clip_end)
        
        # تحميل المجال الزمني المطلوب فقط
        options["download_ranges"] = lambda info_dict, ytdl: [
            {"start_time": start_sec, "end_time": end_sec}
        ]
        options["force_keyframes_at_cuts"] = True
        options["postprocessor_args"] = {
            "ffmpeg": ["-c", "copy"]
        }

    # تنفيذ التنزيل
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if mode == "audio":
                filename = os.path.splitext(filename)[0] + ".mp3"
            
            return {
                "status": "success",
                "title": info.get("title", "video"),
                "filename": os.path.basename(filename),
                "download_url": f"https://snapmega-server.onrender.com/files/{os.path.basename(filename)}"
            }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
        
