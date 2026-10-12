# Web Novel YouTube Automation

Local-first app for turning one web-novel episode into a 16:9 MP4 with script-scaled key images, narration audio, burned subtitles, transitions, and optional BGM.

This implements the PRD's first hard milestone honestly: no paid image API is required. You can paste a manuscript, analyze it into script-scaled scenes, upload/manual-generate images, synthesize or attach narration audio, and render a real MP4 through FFmpeg. Production providers for OpenAI Images and Google Cloud Text-to-Speech are available when credentials are configured.

## Quick Start

```bash
cd webnovel-youtube-automation
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python scripts/create_sample.py
uvicorn app.main:app --host 0.0.0.0 --port 8019
```

If `python3 -m venv` is unavailable on the host, install into the user Python environment instead:

```bash
python3 -m pip install --user --break-system-packages -r requirements.txt
```

Open `http://100.107.174.72:8019` on Tailscale, or `http://localhost:8019` locally.

## Two Modes

The home page (`/`) offers two modes that share one TTS module and one audio cache.

**📖 책 읽어주기 (`/reader`)** — upload an EPUB and have it read aloud. No image API is ever called in this mode.
- Whole book or selected chapters, voice selection (with preview), speed control (client-side `playbackRate`, so changing speed never re-synthesizes), play/pause, previous/next paragraph and chapter, media-key support.
- The EPUB is never sent to TTS as a whole: EPUB → chapters → paragraphs → chunks of at most 400 chars → TTS queue → cached audio → sequential playback. Long chapters are split automatically and played back-to-back (about 25 ms gap), so they sound like one chapter.
- Last position (chapter, paragraph, seconds), speed, voice and chapter selection are saved and restored.
- Audio is generated on play (plus 2 chunks of prefetch) or via "선택 챕터 음성 미리 생성", which shows an estimate (chunks / characters to generate) and asks for confirmation.
- Replacing the EPUB with an edited one (`교체` on the book page) keeps progress and selection; only chunks whose text changed are re-synthesized.

**🎬 YouTube 만들기 (`/youtube`)** — the existing script → TTS → image → subtitle → BGM → MP4 pipeline. Each project has three switches on the episode page:
- AI 이미지 (default **OFF**): when off, the OpenAI image routes return HTTP 400 and the UI hides them; use uploads or "로컬 기본 이미지로 채우기".
- 자막 (default ON): off renders without burned-in subtitles.
- BGM (default ON): off ignores BGM uploads.

## TTS Cache

Audio is stored under `data/assets/tts_cache`, keyed by `sha256(provider|voice|speed|text)`. Identical text with the same voice reuses the file without an API call. Video narration uses `.wav`; reader audio is `.mp3` (transcoded from the same synthesis, or from an existing `.wav` without a new API call). Concurrent requests for the same text share one API call.

## Smoke Test

```bash
. .venv/bin/activate
python scripts/smoke_render.py
```

The smoke test creates a sample project, generates four placeholder images and a test narration track, then renders a 1920x1080 H.264/AAC MP4.

## Access Control

Set `APP_PASSWORD` (and optionally `APP_USER`, default `admin`) in `.env` to require HTTP Basic auth on every page, asset, and render download. Without it the app is open to anyone who can reach the port, including buttons that spend OpenAI/Google credits, so set it whenever the server listens on `0.0.0.0`. Use it over a trusted network (Tailscale) or behind HTTPS, since Basic auth is not encrypted.

## Background Jobs

Narration, image generation, and rendering run as background jobs (one active job per episode). The episode page shows a live progress bar and any failure message. TTS segments are cached by content in `data/assets/tts_cache`, and image generation skips scenes that already have an OpenAI image, so re-running after a failure only redoes the missing parts.

## Providers

Provider code lives behind adapters:

- `LLMProvider`: deterministic local analyzer now, API analyzer later.
- `ImageProvider`: manual upload, local placeholder generator, and OpenAI Images.
- `TTSProvider`: Google Cloud TTS, command-line local TTS when `say`, `espeak`, `espeak-ng`, or `pico2wave` exists, and test-tone provider for smoke tests.

API keys belong in `.env` or shell environment variables. They are not stored in SQLite or project JSON.

## Production Provider Setup

Copy `.env.example` to `.env` and fill only the providers you want to use:

```bash
cp .env.example .env
```

OpenAI Images defaults:

- Model: `gpt-image-1-mini`
- Quality: `medium`
- Size: `1536x1024`
- Episode action: generate one image per analyzed scene, skipping cached OpenAI images
- Scene action: regenerate only that one scene

Google Cloud TTS defaults:

- Language: `ko-KR`
- Audio: LINEAR16 WAV segments
- Speaking rate: `1.0`
- Voice list: loaded from Google when credentials are available

If credentials are missing, the app still starts and marks only that provider unavailable.
