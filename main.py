import os
import json
import time
import hashlib
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state" / "processed.json"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
MAX_UPLOADS = int(os.getenv("MAX_UPLOADS_PER_DAY", "5"))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
VIDEO_MIME_FALLBACK = "application/octet-stream"
ALLOWED_VIDEO_MIMES = {"video/mp4", "video/quicktime", "video/webm", "video/x-matroska",
                       "video/x-msvideo", "video/mpeg", "video/3gpp"}

def required_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required GitHub Actions secret/environment variable: {name}")
    return value

def load_state():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.exists():
        return {"processed": {}, "daily_uploads": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        data.setdefault("processed", {})
        data.setdefault("daily_uploads", {})
        return data
    except Exception:
        raise RuntimeError("state/processed.json is unreadable; refusing to upload to avoid duplicates.")

def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def google_credentials():
    client_id = required_env("GOOGLE_CLIENT_ID")
    client_secret = required_env("GOOGLE_CLIENT_SECRET")
    refresh_token = required_env("GOOGLE_REFRESH_TOKEN")
    # Refresh token was authorized for both scopes in OAuth Playground.
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=DRIVE_SCOPES + YOUTUBE_SCOPES,
    )
    creds.refresh(Request())
    return creds

def list_drive_videos(drive, folder_id):
    results = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed = false"
    while True:
        response = drive.files().list(
            q=query,
            pageSize=100,
            pageToken=page_token,
            orderBy="createdTime",
            fields="nextPageToken,files(id,name,mimeType,size,createdTime,md5Checksum,webViewLink)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        for f in response.get("files", []):
            if f.get("mimeType", "").startswith("video/") or Path(f.get("name", "")).suffix.lower() in {
                ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpeg", ".mpg", ".3gp"
            }:
                results.append(f)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return results

def download_original(drive, file_id, destination):
    request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    # Download in chunks to disk; no transcoding or media conversion.
    from googleapiclient.http import MediaIoBaseDownload
    import io
    with open(destination, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return sha256_file(destination)

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def extract_frames(video_path, out_dir, max_frames=6):
    # Frame extraction is only for analysis/thumbnail generation. The source file is never written to.
    out_dir.mkdir(parents=True, exist_ok=True)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, text=True, timeout=30
    )
    try:
        duration = max(0.1, float(probe.stdout.strip()))
    except Exception:
        duration = 10.0
    times = sorted(set(min(max(0.2, duration * p), max(0.2, duration - 0.2)) for p in [0.12, 0.28, 0.44, 0.60, 0.76, 0.90]))[:max_frames]
    frames = []
    for i, sec in enumerate(times):
        target = out_dir / f"frame_{i}.jpg"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(sec), "-i", str(video_path),
             "-frames:v", "1", "-vf", "scale='min(768,iw)':-2", "-q:v", "3", "-y", str(target)],
            capture_output=True, timeout=45
        )
        if target.exists() and target.stat().st_size:
            frames.append(target)
    return frames

def call_gemini(prompt, frame_paths):
    api_key = required_env("GEMINI_API_KEY")
    # Gemini REST generateContent with sampled frames as inline JPEGs.
    parts = [{"text": prompt}]
    for p in frame_paths[:6]:
        import base64
        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                     "data": base64.b64encode(p.read_bytes()).decode("ascii")}})
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.6}
    }
    for attempt in range(4):
        r = requests.post(url, json=payload, timeout=90)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    raise RuntimeError("Gemini API kept returning a retryable error after 4 attempts.")

def metadata_for_video(name, frame_paths):
    prompt = f"""
You are a careful YouTube Shorts metadata editor. Inspect the supplied sampled video frames and filename.
Return ONLY valid JSON with keys: title, description, tags, hashtags, thumbnail_text, categoryId.
Rules:
- Be accurate; do not claim events or facts not visible or inferable.
- Title should be engaging, specific, natural, and ideally under 70 characters. No fake claims.
- Description: 1-3 useful sentences and a gentle call to action; do not invent links.
- tags: array of up to 12 relevant search phrases, no # symbols.
- hashtags: array of 3-5 relevant hashtags including Shorts when suitable; include #Shorts.
- thumbnail_text: 2-5 short words, suitable for a high-contrast thumbnail overlay.
- categoryId: use "22" unless another category clearly fits.
- This is intended as a YouTube Short. Do not suggest changing or re-encoding the source.
Filename: {name}
"""
    obj = call_gemini(prompt, frame_paths)
    title = str(obj.get("title", Path(name).stem)).strip()[:100]
    description = str(obj.get("description", "")).strip()
    tags = [str(x).strip().lstrip("#")[:100] for x in obj.get("tags", []) if str(x).strip()][:15]
    hashtags = [str(x).strip().replace(" ", "") for x in obj.get("hashtags", []) if str(x).strip()][:5]
    if "#Shorts" not in hashtags:
        hashtags.append("#Shorts")
    description = (description + "\n\n" + " ".join(hashtags)).strip()
    return {
        "title": title or Path(name).stem[:100],
        "description": description[:5000],
        "tags": tags,
        "categoryId": str(obj.get("categoryId", "22")),
        "thumbnail_text": str(obj.get("thumbnail_text", "WATCH THIS"))[:45],
    }

def create_thumbnail(frame_paths, text, destination):
    if not frame_paths:
        return None
    img = Image.open(frame_paths[len(frame_paths)//2]).convert("RGB")
    # Make a vertical 9:16 crop for a Shorts-style thumbnail.
    w, h = img.size
    target_ratio = 9 / 16
    if w / h > target_ratio:
        new_w = int(h * target_ratio)
        left = max(0, (w - new_w) // 2)
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_ratio)
        top = max(0, (h - new_h) // 2)
        img = img.crop((0, top, w, top + new_h))
    img = img.resize((720, 1280), Image.Resampling.LANCZOS)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    from PIL import ImageFilter
    band = Image.new("RGBA", (720, 360), (0, 0, 0, 0))
    d = ImageDraw.Draw(band)
    d.rectangle((0, 0, 720, 360), fill=(0, 0, 0, 145))
    band = band.filter(ImageFilter.GaussianBlur(2))
    overlay.alpha_composite(band, (0, 800))
    img = Image.alpha_composite(img.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(img)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font = ImageFont.truetype(font_path, 54)
    words = text.upper().split()
    lines, current = [], ""
    for word in words:
        candidate = (current + " " + word).strip()
        if draw.textbbox((0,0), candidate, font=font, stroke_width=2)[2] > 650 and current:
            lines.append(current); current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    y = 850
    for line in lines[:3]:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=3)
        x = max(25, (720 - (bbox[2]-bbox[0])) // 2)
        draw.text((x, y), line, font=font, fill=(255,255,255,255),
                  stroke_width=3, stroke_fill=(0,0,0,255))
        y += 70
    img.convert("RGB").save(destination, "JPEG", quality=92, optimize=True)
    return destination

def upload_video(youtube, video_path, meta):
    body = {
        "snippet": {
            "title": meta["title"],
            "description": meta["description"],
            "tags": meta["tags"],
            "categoryId": meta["categoryId"],
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(video_path), mimetype="application/octet-stream",
                            chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    retry = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"Upload progress: {int(status.progress() * 100)}%")
        except HttpError as e:
            if e.resp.status in (429, 500, 502, 503, 504) and retry < 5:
                time.sleep(2 ** retry)
                retry += 1
                continue
            raise
    return response["id"]

def try_set_thumbnail(youtube, video_id, thumbnail_path):
    if not thumbnail_path or not Path(thumbnail_path).exists():
        return "not-created"
    try:
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/jpeg")
        ).execute()
        return "set"
    except Exception as exc:
        print(f"Thumbnail assignment unavailable for {video_id}: {type(exc).__name__}: {exc}")
        return "failed-or-unsupported"

def main():
    folder_id = required_env("DRIVE_FOLDER_ID")
    state = load_state()
    today = datetime.now(timezone.utc).date().isoformat()
    state.setdefault("daily_uploads", {})
    daily_count = int(state["daily_uploads"].get(today, 0))
    if daily_count >= MAX_UPLOADS:
        print(f"Daily limit already reached: {daily_count}/{MAX_UPLOADS} UTC.")
        return

    creds = google_credentials()
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    files = list_drive_videos(drive, folder_id)
    print(f"Found {len(files)} candidate video(s).")
    # Old daily counters are pruned to keep the state file small.
    state["daily_uploads"] = {k:v for k,v in state["daily_uploads"].items() if k >= today}

    with tempfile.TemporaryDirectory(prefix="shorts-") as tmp:
        tmpdir = Path(tmp)
        for f in files:
            if daily_count >= MAX_UPLOADS:
                print("Daily upload limit reached; remaining files wait for a later run.")
                break
            fid = f["id"]
            if fid in state["processed"]:
                continue
            name = f.get("name", fid)
            if f.get("mimeType") not in ALLOWED_VIDEO_MIMES and not Path(name).suffix.lower() in {
                ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpeg", ".mpg", ".3gp"
            }:
                continue
            print(f"Processing: {name}")
            video_path = tmpdir / ("original_" + Path(name).name)
            sha = download_original(drive, fid, video_path)
            # If different Drive IDs contain the same exact bytes, skip the duplicate.
            duplicate_hash = next((k for k,v in state["processed"].items() if v.get("sha256") == sha), None)
            if duplicate_hash:
                state["processed"][fid] = {"sha256": sha, "status": "duplicate-skipped",
                                           "duplicate_of": duplicate_hash, "name": name}
                save_state(state)
                continue

            frame_dir = tmpdir / ("frames_" + fid[:12])
            frames = extract_frames(video_path, frame_dir)
            try:
                meta = metadata_for_video(name, frames)
            except Exception as e:
                # Do not upload without metadata generation; next scheduled run can retry.
                print(f"AI metadata generation failed for {name}: {e}")
                continue

            # Verify downloaded file still matches Drive's checksum when available.
            drive_md5 = f.get("md5Checksum")
            if drive_md5:
                md5 = hashlib.md5(video_path.read_bytes()).hexdigest()
                if md5.lower() != drive_md5.lower():
                    raise RuntimeError(f"Drive checksum mismatch for {name}; refusing upload.")
            sha_before_upload = sha256_file(video_path)
            video_id = upload_video(youtube, video_path, meta)
            sha_after_upload = sha256_file(video_path)
            if sha_before_upload != sha_after_upload:
                raise RuntimeError("Source file changed during upload; check workflow environment.")
            thumb_path = tmpdir / ("thumbnail_" + fid[:12] + ".jpg")
            create_thumbnail(frames, meta["thumbnail_text"], thumb_path)
            thumb_status = try_set_thumbnail(youtube, video_id, thumb_path)
            state["processed"][fid] = {
                "name": name, "sha256": sha, "status": "uploaded",
                "youtube_video_id": video_id,
                "youtube_url": f"https://www.youtube.com/shorts/{video_id}",
                "thumbnail_status": thumb_status,
                "uploaded_at_utc": datetime.now(timezone.utc).isoformat(),
                "title": meta["title"],
            }
            daily_count += 1
            state["daily_uploads"][today] = daily_count
            save_state(state)
            print(f"Published: https://www.youtube.com/shorts/{video_id} | daily {daily_count}/{MAX_UPLOADS}")

    save_state(state)

if __name__ == "__main__":
    main()
