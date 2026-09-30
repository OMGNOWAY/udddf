<div align="center">

# Universal Downloader

</div>


<!-- README polish: repository metadata badges -->
<p>
  <a href="https://github.com/vishnuskandha/Universal-Downloader"><img alt="GitHub stars" src="https://img.shields.io/github/stars/vishnuskandha/Universal-Downloader?style=for-the-badge&logo=github&label=Stars"></a>
  <a href="https://github.com/vishnuskandha/Universal-Downloader/fork"><img alt="GitHub forks" src="https://img.shields.io/github/forks/vishnuskandha/Universal-Downloader?style=for-the-badge&logo=github&label=Forks"></a>
  <a href="https://github.com/vishnuskandha/Universal-Downloader/issues"><img alt="GitHub issues" src="https://img.shields.io/github/issues/vishnuskandha/Universal-Downloader?style=for-the-badge&logo=github&label=Issues"></a>
  <a href="https://github.com/vishnuskandha/Universal-Downloader/commits"><img alt="Last commit" src="https://img.shields.io/github/last-commit/vishnuskandha/Universal-Downloader?style=for-the-badge&logo=git&label=Updated"></a>
</p>
<!-- End README polish -->

[![CI](https://github.com/vishnuskandha/Universal-Downloader/actions/workflows/ci.yml/badge.svg)](https://github.com/vishnuskandha/Universal-Downloader/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A modern video and audio downloader for **YouTube**, **Facebook**, and
**Instagram** - including videos, shorts, reels, and MP3 audio extraction - with
a terminal-themed glassmorphism UI.

- **Frontend**: React, Vite, Tailwind CSS v4, Framer Motion, WebGL matrix background
- **Backend**: Python, FastAPI, yt-dlp, FFmpeg

Built by [Vishnu Skandha](https://github.com/vishnuskandha).

## Features

- Multi-platform support: YouTube (videos, shorts, up to 4K), Facebook
  (videos, reels), Instagram (reels, posts)
- Smart format selection with H.264/AAC compatibility (H.264 video + AAC audio,
  merged to MP4)
- MP3 audio extraction at up to 320kbps
- Single and batch download modes (batch uses a 3-worker concurrency pool with
  auto-retry)
- Auto platform detection and playlist protection (`noplaylist`)
- File size estimation before download
- 3 retry attempts with progressive backoff
- Rate limiting (60 requests/minute/IP) and request timeouts
- Optional auto-send of downloaded videos to a Telegram chat
- Responsive UI usable from mobile devices on the local network

## Quick Start

### Prerequisites

- Python 3.10+
- Node.js 18+
- FFmpeg available on `PATH` (or bundled in `backend/ffmpeg-master-latest-win64-gpl/bin/`)

### One-Click Start (Windows)

```bat
start_dev.bat
```

This installs dependencies on first run and launches the backend (port 8000)
and frontend (port 5173) in separate windows.

### Manual Setup

Backend:

```bash
cd backend
python -m venv venv
venv\Scripts\activate        # Windows: use source venv/bin/activate on macOS/Linux
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. API docs are available at http://localhost:8000/docs.

### Docker

```bash
docker compose up --build
```

Backend runs on port 8000 and frontend on port 5173.

### Telegram Auto-Send (Optional)

Set these environment variables (or in `backend/.env`) to send downloads to
Telegram:

```
TELEGRAM_BOT_TOKEN=<bot token from BotFather>
TELEGRAM_CHAT_ID=<chat, group, or channel ID>
```

### YouTube Sign-In (Optional)

YouTube often blocks datacenter IPs ("confirm you're not a bot"). Give the backend a
**spare** Google account and it signs in by itself with a headless browser:

```
GOOGLE_EMAIL=<spare account email>
GOOGLE_PASSWORD=<its password>
GOOGLE_TOTP_SECRET=<only if it uses an authenticator app>   # optional
ADMIN_TOKEN=<any long random string>                        # for the manual endpoint
```

- Automatic: when YouTube asks for sign-in, the API logs in, saves the cookies and retries
  the request (at most one attempt every 15 minutes).
- Manual: `POST /api/cookies/login` with header `X-Admin-Token: <ADMIN_TOKEN>`.
- Google blocks a lot of automated logins, so this can fail. Set
  `GOOGLE_LOGIN_DEBUG_DIR` to save a screenshot of where it got stuck.
- Uploading a `cookies.txt` with `POST /api/cookies` still works as a fallback.

## CLI Usage

```bash
# Download a single URL
python cli_download.py "https://youtube.com/watch?v=..."

# Batch download from a file (one URL per line, # for comments)
python batch_download.py urls.txt

# Batch download from stdin
echo -e "url1\nurl2" | python batch_download.py -
```

## API Endpoints

### `POST /api/analyze`

Analyze a URL and return available formats.

```json
{
  "url": "https://youtube.com/watch?v=..."
}
```

Response includes platform, title, thumbnail, duration, and a list of formats
with `formatId`, extension, resolution, fps, and estimated file size.

### `POST /api/download`

Download media in the selected format.

```json
{
  "url": "https://youtube.com/watch?v=...",
  "formatId": "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/best",
  "title": "Optional friendly filename"
}
```

Returns the binary file (MP4/MP3) with a `Content-Disposition` header.

### Security

- Only `http`/`https` URLs are accepted, and hosts resolving to private,
  loopback, or link-local addresses are rejected to prevent SSRF abuse.
- No secrets are stored in the repository; the Telegram bot token must be
  provided via environment variables.
- TLS certificate verification is enabled. To restore the legacy bypass
  (needed on networks with custom MITM proxies), set
  `YTDLP_NO_CHECK_CERTIFICATE=1`.

## Project Structure

```
Universal-Downloader/
├── backend/
│   ├── main.py              # FastAPI app, endpoints, rate limiting, SSRF protection
│   ├── downloader.py        # yt-dlp wrapper: analyze, download, retry, FFmpeg control
│   ├── telegram_sender.py   # Optional Telegram video auto-send
│   ├── requirements.txt
│   ├── Dockerfile
│   └── render.yaml          # Render deployment config
├── frontend/
│   ├── src/
│   │   ├── App.jsx          # Main application component
│   │   ├── api.js           # API client (analyze + download + blob error parsing)
│   │   └── components/      # FaultyTerminal, BatchMode, ProfileCard, SpotlightCard
│   ├── package.json
│   ├── vite.config.js
│   └── Dockerfile
├── cli_download.py          # CLI single-URL downloader
├── batch_download.py        # CLI batch downloader
├── start_dev.bat            # Windows one-click startup
├── docker-compose.yml
└── urls.txt                 # Sample URL list for batch mode
```

## License

[MIT](LICENSE) - Copyright (c) 2026 Vishnu Skandha

Download content only where you have the right to do so. Respect the Terms of
Service of every platform.
