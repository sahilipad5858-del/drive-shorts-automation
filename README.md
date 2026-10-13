# Drive to YouTube Shorts Automation

A GitHub Actions workflow that checks one Google Drive folder every four hours, analyses sampled frames with Gemini, generates metadata and a vertical thumbnail, and uploads up to five videos per UTC day.

## Important limitations

- The workflow does **not** transcode, resize, crop, or re-encode the source video. Frame extraction is performed separately for AI analysis and thumbnail creation.
- YouTube reprocesses uploaded videos on its own platform; pixel-identical playback cannot be guaranteed.
- The AI analyses sampled visual frames and the filename; it does not currently transcribe or semantically analyse the full audio track.
- Custom thumbnail assignment is attempted after upload. YouTube may not display a custom thumbnail for Shorts in every surface/account. The workflow logs the result.
- Google may restrict public uploads from unverified API projects. Test with a private video first and confirm API eligibility before enabling unattended public publishing.
- GitHub Actions schedules can be delayed. The five-per-day cap is enforced in repository state using UTC dates.
- The repository's `state/processed.json` contains Drive file IDs, hashes, titles and published URLs. It contains no credentials or video bytes. Because the repository is public, these metadata records are public too.
- Free Gemini API availability and quotas can change. If the free tier is unavailable or rate-limited, the workflow leaves that video unprocessed for a later retry.

## Required GitHub Actions secrets

Set these in **Settings → Secrets and variables → Actions**:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `DRIVE_FOLDER_ID` — ID of the folder containing videos
- `GEMINI_API_KEY` — from Google AI Studio

The OAuth refresh token must be authorized for both scopes:
- `https://www.googleapis.com/auth/drive.readonly`
- `https://www.googleapis.com/auth/youtube.upload`

Use the same Google account for the Drive folder and YouTube channel where possible.

## Setup

1. Upload these files to your GitHub repository, keeping the folder structure.
2. Add all five repository secrets listed above.
3. Ensure `state/processed.json` is committed.
4. In **Settings → Actions → General**, allow GitHub Actions to read and write repository contents.
5. Run **Actions → Drive to YouTube Shorts → Run workflow** manually for the first test.
6. Review the run logs. For a first test, change `privacyStatus` in `main.py` from `public` to `private`. Confirm video, metadata, file integrity and thumbnail behaviour. Then change it back to `public` only after confirming the API project can publish publicly.
7. Use **Settings → Secrets and variables → Actions** to keep all credentials secret.

## Folder ID

Open your Google Drive folder in a browser. Its URL resembles:
`https://drive.google.com/drive/folders/FOLDER_ID_HERE`

The value after `/folders/` is the folder ID. Do not make the folder public.

## Daily cap and duplicates

The workflow uploads at most 5 videos per run and at most 5 per UTC day (both configurable via `MAX_UPLOADS_PER_RUN` / `MAX_UPLOADS_PER_DAY`). This fits the YouTube Data API default quota (10,000 units/day; ~1,600 per `videos.insert` + ~50 per thumbnail ≈ 8,250 for 5 uploads). Keep caps at 5 or lower unless your API quota was increased, otherwise you will see `quotaExceeded`. It records each Drive file ID and SHA-256 hash in `state/processed.json`, and skips an identical file hash even if it has a different Drive ID. Failed metadata requests are retried on later scheduled runs. Upload interruptions use the YouTube API's resumable upload mechanism.

## Security

Never commit OAuth client secrets, refresh tokens, API keys, downloaded videos, or private files. If a credential is exposed, revoke or rotate it immediately.
