import os
import base64
import json
import mimetypes
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

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state" / "processed.json"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
# YouTube Data API default quota is 10,000 units/day.
# videos.insert costs ~1600 units, thumbnails.set ~50 units.
# 5 uploads/run ≈ 8250 units, safely under quota. 6+ risks quotaExceeded.
# Per-run cap protects a single workflow run; per-day cap protects the UTC-day total
# across the every-4-hours schedule (6 runs/day).
MAX_UPLOADS_PER_DAY = int(os.getenv("MAX_UPLOADS_PER_DAY", "5") or "5")
MAX_UPLOADS_PER_RUN = int(os.getenv("MAX_UPLOADS_PER_RUN", "5") or "5")
MAX_UPLOADS_PER_DAY = max(1, min(MAX_UPLOADS_PER_DAY, 10))
MAX_UPLOADS_PER_RUN = max(1, min(MAX_UPLOADS_PER_RUN, 10))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash") or "gemini-3.8-flash"
# Shorts eligibility (YouTube Shorts: <= 3 min since Oct 2024). Original file is
# NEVER re-encoded; longer videos still upload but YouTube treats them as regular videos.
SHORTS_MAX_SECONDS = float(os.getenv("SHORTS_MAX_SECONDS", "180") or "180")
STRICT_SHORTS_ONLY = (os.getenv("STRICT_SHORTS_ONLY", "false") or "false").strip().lower() in ("1", "true", "yes")
# YouTube custom thumbnails: 1280x720 recommended, JPG/PNG, must be < 2MB for safety.
THUMBNAIL_MAX_BYTES = int(os.getenv("THUMBNAIL_MAX_BYTES", "1900000") or "1900000")
VIDEO_MIME_FALLBACK = "application/octet-stream"
ALLOWED_VIDEO_MIMES = {"video/mp4", "video/quicktime", "video/webm", "video/x-matroska",
                       "video/x-msvideo", "video/mpeg", "video/3gpp"}

class UploadLimitReached(RuntimeError):
    """YouTube channel daily limit or API quota hit — stop run cleanly (exit 0)."""

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
    try:
        creds.refresh(Request())
    except Exception as e:
        raise RuntimeError(
            "Google OAuth refresh failed. Verify GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN "
            f"and that the refresh token was granted scopes {DRIVE_SCOPES + YOUTUBE_SCOPES}: {e}"
        ) from e
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

def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_video(video_path):
    """Read-only media inspection (never modifies the file). Returns dict."""
    info = {"duration": None, "width": None, "height": None, "codec": None,
            "size_bytes": Path(video_path).stat().st_size}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,codec_name,duration",
             "-show_entries", "format=duration",
             "-of", "json", str(video_path)],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            streams = data.get("streams") or [{}]
            s = streams[0]
            info["width"] = s.get("width")
            info["height"] = s.get("height")
            info["codec"] = s.get("codec_name")
            dur = s.get("duration") or (data.get("format") or {}).get("duration")
            try:
                info["duration"] = float(dur) if dur is not None else None
            except Exception:
                pass
    except FileNotFoundError:
        raise RuntimeError("ffprobe not found. Ensure FFmpeg is installed (sudo apt-get install -y ffmpeg).")
    except Exception as e:
        print(f"Warning: ffprobe inspection failed: {e}")
    return info


def extract_frames(video_path, out_dir, max_frames=6):    # Frame extraction is only for analysis/thumbnail generation. The source file is never written to.
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError:
        raise RuntimeError("ffprobe not found. Ensure FFmpeg is installed (sudo apt-get install -y ffmpeg).")
    except subprocess.TimeoutExpired:
        probe = None
    try:
        duration = max(0.1, float(probe.stdout.strip())) if probe and probe.stdout.strip() else 10.0
    except Exception:
        duration = 10.0
    times = sorted(set(min(max(0.2, duration * p), max(0.2, duration - 0.2)) for p in [0.12, 0.28, 0.44, 0.60, 0.76, 0.90]))[:max_frames]
    frames = []
    for i, sec in enumerate(times):
        target = out_dir / f"frame_{i}.jpg"
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(sec), "-i", str(video_path),
                 "-frames:v", "1", "-vf", "scale='min(768,iw)':-2", "-q:v", "3", "-y", str(target)],
                capture_output=True, timeout=60
            )
        except FileNotFoundError:
            raise RuntimeError("ffmpeg not found. Ensure FFmpeg is installed (sudo apt-get install -y ffmpeg).")
        except subprocess.TimeoutExpired:
            print(f"Warning: ffmpeg timed out extracting frame at {sec}s; skipping.")
            continue
        if result.returncode != 0:
            print(f"Warning: ffmpeg failed for frame at {sec}s: {(result.stderr or b'')[:500]!r}")
            continue
        if target.exists() and target.stat().st_size:
            frames.append(target)
    if not frames:
        print("Warning: no frames extracted; Gemini will use filename only.")
    return frames

def _strip_json_fences(text):
    text = text.strip()
    if text.startswith("```"):
        # Remove markdown fences like ```json ... ```
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text

def call_gemini(prompt, frame_paths):
    api_key = required_env("GEMINI_API_KEY")
    # Gemini REST generateContent with sampled frames as inline JPEGs.
    # NOTE: REST field names are camelCase (inlineData/mimeType). snake_case
    # names are rejected with 400 "Unknown name" errors.
    parts = [{"text": prompt}]
    for p in frame_paths[:6]:
        parts.append({"inlineData": {"mimeType": "image/jpeg",
                                     "data": base64.b64encode(Path(p).read_bytes()).decode("ascii")}})
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.6}
    }
    last_error = None
    for attempt in range(4):
        try:
            r = requests.post(url, json=payload, timeout=90)
        except requests.RequestException as e:
            last_error = e
            print(f"Gemini request failed (attempt {attempt + 1}/4): {e}")
            time.sleep(2 ** attempt)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last_error = RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:500]}")
            print(f"Gemini retryable HTTP {r.status_code} (attempt {attempt + 1}/4).")
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 404:
            raise RuntimeError(
                f"Gemini model '{GEMINI_MODEL}' not found (HTTP 404: {r.text[:500]}). "
                "Set a valid GEMINI_MODEL env var, e.g. gemini-2.5-flash or gemini-3.8-flash."
            )
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            # Include response body so Actions logs show the real cause (bad key, bad payload, quota).
            raise RuntimeError(f"Gemini API error HTTP {r.status_code}: {r.text[:1000]}") from e
        try:
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"Gemini returned non-JSON response: {r.text[:1000]}") from e
        candidates = data.get("candidates") or []
        if not candidates:
            # Often a safety block or quota issue; surface full payload for debugging.
            raise RuntimeError(f"Gemini returned no candidates (blocked? quota?): {json.dumps(data)[:1000]}")
        try:
            text = candidates[0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"Unexpected Gemini response shape: {json.dumps(data)[:1000]}") from e
        try:
            return json.loads(_strip_json_fences(text))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Gemini did not return valid JSON: {text[:1000]}") from e
    raise RuntimeError(f"Gemini API kept returning a retryable error after 4 attempts: {last_error}")

def metadata_for_video(name, frame_paths, probe=None):
    probe_note = ""
    if probe:
        probe_note = (f"\nVideo facts: duration={probe.get('duration')}s, "
                      f"resolution={probe.get('width')}x{probe.get('height')}, codec={probe.get('codec')}.")
    prompt = f"""
You are an expert YouTube Shorts strategist + metadata editor. Inspect the sampled frames and filename.
Return ONLY valid JSON with keys: title, description, tags, hashtags, thumbnail_text, categoryId.
Rules (virality + accuracy):
- Be 100% accurate; never invent events, people, places, or claims not visible/inferable.
- Title: hook-first, specific, natural, <=70 chars ideal (hard max 100). No ALL-CAPS, no clickbait lies.
- Description: first 1-2 lines must hook + summarize (visible above the fold); then 1 useful sentence;
  then soft CTA (e.g. follow for more). No invented links. Hashtags go at the very end.
- tags: up to 12 specific search phrases (2-4 words each), no # symbols, include 2-3 long-tail variants.
- hashtags: 3-5 total, lowercase-friendly except proper nouns; always end with #Shorts.
- thumbnail_text: 2-4 BIG emotion/curiosity words (e.g. MUST SEE, SO CLOSE), no small words.
- categoryId: "22" (People & Blogs) unless clearly music/gaming/etc.
- Audience: global Shorts feed, any device. Keep language simple and universal.
Filename: {name}{probe_note}
"""
    obj = call_gemini(prompt, frame_paths)
    if not isinstance(obj, dict):
        raise RuntimeError(f"Gemini returned unexpected JSON (expected object): {str(obj)[:500]}")
    title = str(obj.get("title", Path(name).stem)).strip()[:100]
    description = str(obj.get("description", "")).strip()
    tags = [str(x).strip().lstrip("#")[:100] for x in obj.get("tags", []) if str(x).strip()][:15]
    hashtags = [h if h.startswith("#") else f"#{h}" for h in
                (str(x).strip().replace(" ", "") for x in obj.get("hashtags", []) if str(x).strip())][:5]
    if "#Shorts" not in hashtags:
        hashtags.append("#Shorts")
    description = (description + "\n\n" + " ".join(hashtags)).strip()
    category = str(obj.get("categoryId", "22")).strip() or "22"
    if not category.isdigit():
        category = "22"
    return {
        "title": title or Path(name).stem[:100],
        "description": description[:5000],
        "tags": tags,
        "categoryId": category,
        "thumbnail_text": str(obj.get("thumbnail_text", "WATCH THIS"))[:45],
    }

def _load_font(size):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]
    for path in candidates:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def create_thumbnail(frame_paths, text, destination):
    """Build a YouTube-spec thumbnail (1280x720 JPG <2MB) WITHOUT touching the video.

    Uses the middle sampled frame: blurred 16:9 background + centered 9:16 crop
    + high-contrast text band. Returns path or None. Never raises.
    """
    if not frame_paths:
        return None
    try:
        from PIL import ImageFilter, ImageOps
        src = Image.open(frame_paths[len(frame_paths) // 2])
        try:
            src = ImageOps.exif_transpose(src)
        except Exception:
            pass
        src = src.convert("RGB")
    except Exception as e:
        print(f"Warning: could not open frame for thumbnail: {e}")
        return None
    try:
        # 9:16 vertical crop from source for the Shorts look.
        w, h = src.size
        target_ratio = 9 / 16
        if w / h > target_ratio:
            new_w = int(h * target_ratio)
            left = max(0, (w - new_w) // 2)
            crop = src.crop((left, 0, left + new_w, h))
        else:
            new_h = int(w / target_ratio)
            top = max(0, (h - new_h) // 2)
            crop = src.crop((0, top, w, top + new_h))
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:
            resample = Image.LANCZOS
        # Canvas: YouTube-spec 1280x720. Background = blurred fill, center = sharp vertical.
        canvas = Image.new("RGB", (1280, 720), (0, 0, 0))
        bg = crop.resize((1280, 720), resample).filter(ImageFilter.GaussianBlur(25))
        # Darken background slightly for text contrast.
        bg = Image.blend(bg, Image.new("RGB", bg.size, (0, 0, 0)), 0.35)
        canvas.paste(bg, (0, 0))
        fg_h, fg_w = 720, 405  # 9:16 panel centered
        fg = crop.resize((fg_w, fg_h), resample)
        canvas.paste(fg, ((1280 - fg_w) // 2, 0))
        # Bottom text band with contrast.
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        band = Image.new("RGBA", (1280, 200), (0, 0, 0, 0))
        d = ImageDraw.Draw(band)
        d.rectangle((0, 0, 1280, 200), fill=(0, 0, 0, 150))
        band = band.filter(ImageFilter.GaussianBlur(3))
        overlay.alpha_composite(band, (0, 520))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay)
        draw = ImageDraw.Draw(canvas)
        font = _load_font(64)
        words = (text or "WATCH THIS").upper().split()
        lines, current = [], ""
        for word in words:
            candidate = (current + " " + word).strip()
            try:
                width = draw.textbbox((0, 0), candidate, font=font, stroke_width=2)[2]
            except Exception:
                width = len(candidate) * 34
            if width > 1150 and current:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        y = 555
        for line in lines[:2]:
            try:
                bbox = draw.textbbox((0, 0), line, font=font, stroke_width=3)
                x = max(30, (1280 - (bbox[2] - bbox[0])) // 2)
            except Exception:
                x = 30
            draw.text((x, y), line, font=font, fill=(255, 255, 255, 255),
                      stroke_width=3, stroke_fill=(0, 0, 0, 255))
            y += 78
        # Save under YouTube's limit (<2MB): step quality down until it fits.
        destination = Path(destination)
        for quality in (92, 85, 78, 70, 60):
            canvas.convert("RGB").save(destination, "JPEG", quality=quality, optimize=True)
            if destination.stat().st_size <= THUMBNAIL_MAX_BYTES:
                break
        size = destination.stat().st_size
        if size > THUMBNAIL_MAX_BYTES:
            print(f"Warning: thumbnail {size} bytes exceeds {THUMBNAIL_MAX_BYTES}; YouTube may reject it.")
        else:
            print(f"Thumbnail: 1280x720 JPG q-fit, {size // 1024}KB")
        return destination
    except Exception as e:
        print(f"Warning: thumbnail creation failed: {e}")
        return None

def _guess_upload_mimetype(video_path, drive_mime=None):
    if drive_mime and drive_mime.startswith("video/"):
        return drive_mime
    guessed, _ = mimetypes.guess_type(str(video_path))
    if guessed and guessed.startswith("video/"):
        return guessed
    suffix = Path(video_path).suffix.lower()
    mapping = {
        ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
        ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
        ".mpeg": "video/mpeg", ".mpg": "video/mpeg", ".3gp": "video/3gpp",
    }
    return mapping.get(suffix, VIDEO_MIME_FALLBACK)

def upload_video(youtube, video_path, meta, drive_mime=None):
    body = {
        "snippet": {
            "title": meta["title"],
            "description": meta["description"],
            "tags": meta["tags"],
            "categoryId": meta["categoryId"],
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(video_path),
                            mimetype=_guess_upload_mimetype(video_path, drive_mime),
                            chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    retry = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"Upload progress: {int(status.progress() * 100)}%")
        except Exception as e:
            # googleapiclient raises HttpError/ResumableUploadError on upload failures.
            status_code = getattr(getattr(e, "resp", None), "status", None)
            try:
                raw = getattr(e, "content", b"")
                content = raw.decode("utf-8", "replace")[:2000] if isinstance(raw, (bytes, bytearray)) else str(raw)[:2000]
            except Exception:
                content = str(e)[:2000]
            haystack = f"{content} {e}"
            limit_hit = (
                status_code in (400, 403, 429)
                and any(s in haystack for s in (
                    "uploadLimitExceeded", "quotaExceeded", "quota exceeded",
                    "rateLimitExceeded", "uploadRateLimitExceeded",
                    "dailyLimitExceeded", "exceeded the number of videos",
                    "Video Uploads per day",
                ))
            )
            if limit_hit:
                # Channel daily upload limit or Cloud project quota: stop run cleanly,
                # do not crash with exit 1. Quota resets ~midnight Pacific.
                raise UploadLimitReached(
                    f"YouTube limit reached (HTTP {status_code}): {content[:1000]}"
                ) from e
            if status_code in (429, 500, 502, 503, 504) and retry < 5:
                time.sleep(2 ** retry)
                retry += 1
                continue
            # Surface YouTube error body in Actions logs (auth, invalid metadata).
            raise RuntimeError(f"YouTube upload failed HTTP {status_code}: {content[:1000]}") from e
    if not response or "id" not in response:
        raise RuntimeError(f"YouTube upload returned unexpected response: {str(response)[:500]}")
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
    folder_id = required_env("DRIVE_FOLDER_ID").strip()
    if not folder_id or any(c in folder_id for c in ("'", '"', " ", "/")):
        raise RuntimeError("DRIVE_FOLDER_ID looks invalid; expected the ID from the Drive folder URL (/folders/ID).")
    state = load_state()
    today = datetime.now(timezone.utc).date().isoformat()
    state.setdefault("daily_uploads", {})
    daily_count = int(state["daily_uploads"].get(today, 0))
    run_count = 0
    print(f"Caps: {MAX_UPLOADS_PER_RUN} per run, {MAX_UPLOADS_PER_DAY} per UTC day "
          f"(daily used: {daily_count}, model: {GEMINI_MODEL}).")
    if MAX_UPLOADS_PER_DAY > 5 or MAX_UPLOADS_PER_RUN > 5:
        print("Warning: >5 uploads risks YouTube Data API quotaExceeded "
              "(default 10k units/day; ~1600 per upload). Lower caps if you see quota errors.")
    if daily_count >= MAX_UPLOADS_PER_DAY:
        print(f"Daily limit already reached: {daily_count}/{MAX_UPLOADS_PER_DAY} UTC.")
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
            if run_count >= MAX_UPLOADS_PER_RUN:
                print(f"Per-run upload limit reached ({run_count}/{MAX_UPLOADS_PER_RUN}); "
                      "remaining files wait for a later run.")
                break
            if daily_count >= MAX_UPLOADS_PER_DAY:
                print(f"Daily upload limit reached ({daily_count}/{MAX_UPLOADS_PER_DAY} UTC); "
                      "remaining files wait for a later run.")
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
            # ZERO-QUALITY-LOSS GUARANTEE: the upload path is this exact byte-for-byte
            # file. ffmpeg/ffprobe only READ it (frames for AI + thumbnail). No
            # transcode/resize/crop/re-encode is ever applied to the upload.
            probe = probe_video(video_path)
            dur = probe.get("duration")
            if dur is not None:
                is_short = dur <= SHORTS_MAX_SECONDS
                print(f"Media: {probe.get('width')}x{probe.get('height')} {probe.get('codec')}, "
                      f"{dur:.1f}s, {probe['size_bytes'] // (1024*1024)}MB, sha256={sha[:12]}… "
                      f"(original bytes, 0% change on our side)")
                if not is_short:
                    msg = (f"{name} is {dur:.0f}s > {SHORTS_MAX_SECONDS:.0f}s: YouTube will treat it "
                           "as a regular video, not a Short.")
                    if STRICT_SHORTS_ONLY:
                        print(f"Skipping (STRICT_SHORTS_ONLY=1): {msg}")
                        continue
                    print(f"Warning: {msg} Uploading original anyway.")
            else:
                print(f"Media: {probe['size_bytes'] // (1024*1024)}MB, sha256={sha[:12]}… "
                      "(duration unknown; uploading original anyway)")
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
                meta = metadata_for_video(name, frames, probe)
            except Exception as e:
                # Do not upload without metadata generation; next scheduled run can retry.
                print(f"AI metadata generation failed for {name}: {e}")
                continue

            # Verify downloaded file still matches Drive's checksum when available.
            # Google Drive only provides md5 for non-Google-Docs binaries; skip otherwise.
            drive_md5 = f.get("md5Checksum")
            if drive_md5:
                md5 = md5_file(video_path)
                if md5.lower() != drive_md5.lower():
                    raise RuntimeError(f"Drive checksum mismatch for {name}; refusing upload.")
            sha_before_upload = sha256_file(video_path)
            try:
                video_id = upload_video(youtube, video_path, meta, f.get("mimeType"))
            except UploadLimitReached as e:
                # YouTube channel daily limit or project quota — not a code bug.
                # Stop cleanly with exit 0 so the run shows green; retry after reset
                # (~midnight Pacific), after phone-verifying the channel, or with lower caps.
                print(f"Stopping run: {e}")
                print("This is a YouTube limit (channel daily upload limit or API quota), "
                      "not a code bug. Check YouTube Studio uploads, verify channel by phone, "
                      "and retry after quota reset.")
                save_state(state)
                break
            sha_after_upload = sha256_file(video_path)
            if sha_before_upload != sha_after_upload:
                raise RuntimeError("Source file changed during upload; check workflow environment.")
            # Thumbnail must never block state saving: a video uploaded without a
            # thumbnail must still be recorded, otherwise the next run re-uploads it.
            thumb_path = tmpdir / ("thumbnail_" + fid[:12] + ".jpg")
            try:
                create_thumbnail(frames, meta["thumbnail_text"], thumb_path)
            except Exception as e:
                print(f"Warning: thumbnail creation failed for {name}: {e}")
            try:
                thumb_status = try_set_thumbnail(youtube, video_id, thumb_path)
            except Exception as e:
                print(f"Warning: thumbnail step crashed for {name}: {e}")
                thumb_status = "failed-or-unsupported"
            state["processed"][fid] = {
                "name": name, "sha256": sha, "status": "uploaded",
                "youtube_video_id": video_id,
                "youtube_url": f"https://www.youtube.com/shorts/{video_id}",
                "thumbnail_status": thumb_status,
                "uploaded_at_utc": datetime.now(timezone.utc).isoformat(),
                "title": meta["title"],
            }
            daily_count += 1
            run_count += 1
            state["daily_uploads"][today] = daily_count
            save_state(state)
            print(f"Published: https://www.youtube.com/shorts/{video_id} | "
                  f"run {run_count}/{MAX_UPLOADS_PER_RUN}, daily {daily_count}/{MAX_UPLOADS_PER_DAY}")

    save_state(state)

if __name__ == "__main__":
    main()
