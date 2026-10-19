import os
import re
import json
import shutil
import tempfile
import subprocess
import threading
import urllib.request
import urllib.parse
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl

app = FastAPI(title="SnapMega Download API", version="1.2.0")
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
    "youtube.com", "youtu.be", "youtube-nocookie.com",
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
semaphore = threading.BoundedSemaphore(2)


class DownloadRequest(BaseModel):
    url: HttpUrl
    mode: str = "video"  # video | audio | clip
    clip_start: str = "00:10"
    clip_end: str = "01:00"


def host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def validate_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in ("http", "https") or not host:
        raise HTTPException(status_code=400, detail="رابط غير صالح")
    if not any(host_matches(host, domain) for domain in ALLOWED_HOSTS):
        raise HTTPException(status_code=400, detail="هذه المنصة غير مدعومة")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="الرابط غير صالح")
    return raw_url


def safe_filename(name: str, mode: str) -> str:
    name = Path(name).name
    name = re.sub(r"[^\w .()\-\[\]]+", "_", name, flags=re.UNICODE).strip(" .")
    if not name:
        name = "snapmega_download"
    suffix = ".mp3" if mode == "audio" else ".mp4"
    stem = Path(name).stem if Path(name).suffix else name
    return stem[:80] + suffix


def parse_time_to_seconds(value: str) -> int:
    parts = str(value).strip().split(":")
    if not 1 <= len(parts) <= 3 or any(not part.isdigit() for part in parts):
        raise ValueError("صيغة الوقت غير صحيحة")
    nums = [int(part) for part in parts]
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        if nums[1] >= 60:
            raise ValueError("الثواني غير صحيحة")
        return nums[0] * 60 + nums[1]
    if nums[1] >= 60 or nums[2] >= 60:
        raise ValueError("صيغة الوقت غير صحيحة")
    return nums[0] * 3600 + nums[1] * 60 + nums[2]


def convert_to_mp3(source: str, temp_dir: str) -> str:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed")
    output = os.path.join(temp_dir, "snapmega_audio.mp3")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", source, "-vn", "-codec:a", "libmp3lame", "-q:a", "0", output],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=240,
    )
    if result.returncode != 0 or not os.path.isfile(output) or os.path.getsize(output) < 1024:
        print("FFmpeg MP3 error:", (result.stderr or "")[-3000:])
        raise RuntimeError("MP3 conversion failed")
    return output


# TikTok direct route stays first-choice; MP3 is converted instead of merely renaming music.
def get_tiktok_direct(raw_url: str, mode: str, temp_dir: str) -> str:
    data = urllib.parse.urlencode({"url": raw_url, "hd": 1}).encode("utf-8")
    req = urllib.request.Request(
        "https://www.tikwm.com/api/", data=data,
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise RuntimeError("TikWM API returned an error")
    data_obj = payload["data"]
    media_url = data_obj.get("music") if mode == "audio" else (
        data_obj.get("play") or data_obj.get("hdplay") or data_obj.get("wmplay")
    )
    if not media_url:
        raise RuntimeError("TikTok media URL missing")
    ext = ".source" if mode == "audio" else ".mp4"
    video_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(data_obj.get("id", "video")))
    output_path = os.path.join(temp_dir, f"tiktok_{video_id}{ext}")
    req = urllib.request.Request(
        media_url,
        headers={"User-Agent": USER_AGENT, "Referer": "https://www.tiktok.com/"},
    )
    with urllib.request.urlopen(req, timeout=90) as response, open(output_path, "wb") as output:
        shutil.copyfileobj(response, output)
    if not os.path.isfile(output_path) or os.path.getsize(output_path) < 5 * 1024:
        raise RuntimeError("TikTok download is empty or too small")
    return convert_to_mp3(output_path, temp_dir) if mode == "audio" else output_path


def resolve_facebook_share_url(raw_url: str) -> str:
    """Resolve public Facebook share URLs without forwarding login redirects to yt-dlp."""
    parsed = urlparse(raw_url)
    host = (parsed.hostname or "").lower()
    if not (host_matches(host, "facebook.com") or host == "fb.watch"):
        return raw_url
    try:
        req = urllib.request.Request(raw_url, headers={"User-Agent": USER_AGENT})
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
        with opener.open(req, timeout=15) as response:
            final_url = response.geturl()
        final = urlparse(final_url)
        final_host = (final.hostname or "").lower()
        if (host_matches(final_host, "facebook.com") or final_host == "fb.watch") and not any(
            marker in final.path.lower() for marker in ("/login", "/checkpoint", "/recover")
        ):
            return final_url
        print("Facebook share link redirected to login; trying original URL")
    except Exception as exc:
        print("Facebook share URL resolution note:", repr(exc))
    return raw_url


def probe_media(path: str) -> dict:
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is not installed")
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name:format=duration", "-of", "json", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("Could not inspect media: " + (result.stderr or "")[-1000:])
    data = json.loads(result.stdout or "{}")
    types = {stream.get("codec_type") for stream in data.get("streams", [])}
    try:
        duration = float(data.get("format", {}).get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {"has_video": "video" in types, "has_audio": "audio" in types, "duration": duration}


def validate_video(path: str) -> dict:
    if not os.path.isfile(path) or os.path.getsize(path) < 5 * 1024:
        raise RuntimeError("Media file missing or too small")
    info = probe_media(path)
    if not info["has_video"] or info["duration"] <= 0.2:
        raise RuntimeError("Output is not a valid video")
    return info


def make_compatible_mp4(source: str, temp_dir: str) -> str:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg/ffprobe is not installed")
    info = validate_video(source)
    output = os.path.join(temp_dir, "snapmega_final.mp4")
    cmd = ["ffmpeg", "-y", "-i", source, "-map", "0:v:0"]
    cmd += ["-map", "0:a:0"] if info["has_audio"] else ["-an"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
    if info["has_audio"]:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += ["-movflags", "+faststart", "-sn", "-dn", output]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=300)
    if result.returncode != 0 or not os.path.isfile(output):
        print("FFmpeg MP4 error:", (result.stderr or "")[-4000:])
        raise RuntimeError("MP4 conversion failed")
    validate_video(output)
    return output


def download_with_ytdlp(raw_url: str, mode: str, request: DownloadRequest, temp_dir: str) -> str:
    if not shutil.which("yt-dlp"):
        raise HTTPException(status_code=500, detail="yt-dlp غير مثبت على السيرفر")
    output_template = os.path.join(temp_dir, "media_%(id)s.%(ext)s")
    cmd = [
        "yt-dlp", "--ignore-config", "--no-playlist", "--no-warnings", "--no-progress",
        "--force-overwrites", "--socket-timeout", "30", "--retries", "2", "--fragment-retries", "2",
        "--user-agent", USER_AGENT, "--add-header", "Accept-Language:en-US,en;q=0.9", "-o", output_template,
    ]
    host = (urlparse(raw_url).hostname or "").lower()
    if host_matches(host, "youtube.com") or host == "youtu.be" or host_matches(host, "youtube-nocookie.com"):
        cmd += ["--extractor-args", "youtube:player_client=default"]
    if mode == "audio":
        cmd += ["-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        cmd += ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]
        if mode == "clip":
            try:
                start = parse_time_to_seconds(request.clip_start)
                end = parse_time_to_seconds(request.clip_end)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="صيغة وقت القص غير صحيحة") from exc
            if end <= start:
                raise HTTPException(status_code=400, detail="وقت النهاية يجب أن يكون بعد وقت البداية")
            cmd += ["--download-sections", f"*{start}-{end}", "--force-keyframes-at-cuts"]
    cmd.append(raw_url)
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        print("yt-dlp timeout for host:", host)
        raise HTTPException(status_code=504, detail="انتهت مهلة التنزيل؛ حاول مجدداً") from exc
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "No error output")[-5000:]
        print("yt-dlp failed for host:", host)
        print("yt-dlp exit code:", result.returncode)
        print("yt-dlp stderr:", err)
        low = err.lower()
       if any(word in low for word in (
    "login required",
    "sign in to confirm",
    "cookies are needed",
    "checkpoint",
    "private video",
)):
    raise HTTPException(
        status_code=400,
        detail="الفيديو خاص أو المنصة تطلب تسجيل الدخول. جرّب رابط فيديو عام."
    )

if any(word in low for word in (
    "403 forbidden",
    "http error 403",
    "429 too many requests",
)):
    raise HTTPException(
        status_code=400,
        detail="المنصة رفضت الطلب أو حدّت من عدد المحاولات. حاول لاحقاً."
    )

print("yt-dlp full error:", err)

raise HTTPException(
    status_code=400,
    detail="فشل التحميل. راجع Logs في Render لمعرفة الخطأ الحقيقي."
)
        if "unsupported url" in low:
            raise HTTPException(status_code=400, detail="الرابط غير مدعوم أو رابط مشاركة؛ جرّب رابط الفيديو الأصلي.")
        raise HTTPException(status_code=400, detail="فشل استخراج الفيديو؛ راجع Logs في Render لمعرفة السبب.")
    extensions = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".mp3", ".m4a", ".opus", ".wav", ".source"}
    candidates = []
    for name in os.listdir(temp_dir):
        path = os.path.join(temp_dir, name)
        if not os.path.isfile(path) or name.endswith((".part", ".ytdl", ".temp")):
            continue
        if Path(name).suffix.lower() in extensions and os.path.getsize(path) >= 5 * 1024:
            candidates.append(path)
    if not candidates:
        raise RuntimeError("yt-dlp produced no usable output file")
    candidates.sort(key=lambda path: (os.path.basename(path).startswith("media_"), os.path.getsize(path)), reverse=True)
    selected = candidates[0]
    if mode == "audio":
        if Path(selected).suffix.lower() != ".mp3":
            selected = convert_to_mp3(selected, temp_dir)
    else:
        validate_video(selected)
    return selected


@app.get("/")
def root():
    return {"ok": True, "service": "snapmega-api", "health": "/health", "download": "/api/download"}


@app.get("/health")
def health():
    return {
        "ok": True, "service": "snapmega-api", "version": "1.2.0",
        "yt_dlp": shutil.which("yt-dlp") is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
    }


@app.post("/api/download")
def download_media(request: DownloadRequest):
    raw_url = validate_url(str(request.url))
    mode = (request.mode or "video").lower().strip()
    if mode not in ("video", "audio", "clip"):
        raise HTTPException(status_code=400, detail="نوع التحميل غير مدعوم")
    if not semaphore.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="السيرفر مشغول حالياً، حاول بعد لحظات")
    temp_dir = tempfile.mkdtemp(prefix="snapmega_")
    try:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise HTTPException(status_code=500, detail="FFmpeg/ffprobe غير مثبتين على السيرفر")
        host = (urlparse(raw_url).hostname or "").lower()
        is_tiktok = host_matches(host, "tiktok.com")
        target = None
        used_tiktok_direct = False
        if is_tiktok:
            try:
                target = get_tiktok_direct(raw_url, mode, temp_dir)
                used_tiktok_direct = True
            except Exception as exc:
                print("TikTok direct failed; falling back to yt-dlp:", repr(exc))
        if not target or not os.path.isfile(target):
            resolved_url = resolve_facebook_share_url(raw_url)
            target = download_with_ytdlp(resolved_url, mode, request, temp_dir)
            used_tiktok_direct = False
        if not os.path.isfile(target) or os.path.getsize(target) < 5 * 1024:
            raise RuntimeError("Output file missing or invalid")
        if mode == "audio":
            if Path(target).suffix.lower() != ".mp3":
                target = convert_to_mp3(target, temp_dir)
            media_type = "audio/mpeg"
        else:
            if not (is_tiktok and used_tiktok_direct):
                target = make_compatible_mp4(target, temp_dir)
            media_type = "video/mp4"
        filename = safe_filename(os.path.basename(target), mode)
        # FileResponse streams after return; don't delete temp_dir here.
        return FileResponse(path=target, filename=filename, media_type=media_type)
    except HTTPException:
        raise
    except Exception as exc:
        print("Download endpoint error:", repr(exc))
        raise HTTPException(status_code=400, detail="تعذر تنزيل الوسيط. راجع Logs في Render لمعرفة السبب.") from exc
    finally:
        semaphore.release()
