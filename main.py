import os
import re
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="SnapMega Ultra Engine", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

ALLOWED_DOMAINS = {
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "kick.com",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

class DownloadRequest(BaseModel):
    url: str
    mode: str = "video"

def get_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")

def is_allowed_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        return (
            parsed.scheme.lower() in {"http", "https"}
            and any(host == domain or host.endswith("." + domain) for domain in ALLOWED_DOMAINS)
        )
    except Exception:
        return False

@app.get("/")
@app.get("/health")
def health_check():
    return {"status": "ultra-ready", "engine": "SnapMega v3.0"}

@app.post("/api/download")
def get_media_url(req: DownloadRequest):
    url = req.url.strip()
    mode = req.mode.lower().strip()

    if not is_allowed_url(url):
        raise HTTPException(status_code=400, detail="رابط غير مدعوم.")

    host = get_host(url)

    # إعدادات فائقة السرعة: استخراج البيانات والروابط فقط دون كتابة على القرص
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
        "socket_timeout": 8,
    }

    # تخصيص الإعدادات لكل منصة
    if "youtube.com" in host or "youtu.be" in host:
        ydl_opts["format"] = "best[ext=mp4]/best" if mode == "video" else "bestaudio/best"
        ydl_opts["extractor_args"] = {
            "youtube": {
                "player_client": ["ios", "android", "mweb"],
                "player_skip": ["configs", "webpage"]
            }
        }
        cookie_path = Path(__file__).resolve().parent / "cookies.txt"
        if cookie_path.is_file():
            ydl_opts["cookiefile"] = str(cookie_path)

    elif "kick.com" in host:
        ydl_opts["format"] = "best"
        ydl_opts["http_headers"] = {
            "User-Agent": USER_AGENT,
            "Referer": "https://kick.com/",
        }

    elif "instagram.com" in host:
        ydl_opts["format"] = "best"
        ydl_opts["http_headers"] = {
            "User-Agent": "Instagram 278.0.0.19.115 Android (30/11; 480dpi; 1080x2280; samsung; SM-G998B; p3s; exynos2100; en_US; 460010976)",
        }
        ig_cookies = Path(__file__).resolve().parent / "instagram_cookies.txt"
        if ig_cookies.is_file():
            ydl_opts["cookiefile"] = str(ig_cookies)

    else:
        ydl_opts["format"] = "best"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # download=False تضمن استخراج الرابط المباشر في ثانية واحدة دون استهلاك موارد
            info = ydl.extract_info(url, download=False)

            download_url = info.get("url")
            # في حال كانت هناك صيغ متعددة
            if not download_url and "formats" in info:
                download_url = info["formats"][-1].get("url")

            if not download_url:
                raise HTTPException(status_code=400, detail="تعذر جلب رابط التحميل المباشر.")

            return {
                "status": "success",
                "title": info.get("title", "video"),
                "thumbnail": info.get("thumbnail"),
                "download_url": download_url,
                "is_live": info.get("is_live", False),
            }

    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"خطأ في المعالجة: {str(exc)[:200]}")
