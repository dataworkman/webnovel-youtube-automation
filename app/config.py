from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "app.sqlite3"
ASSETS_DIR = DATA_DIR / "assets"
RENDERS_DIR = DATA_DIR / "renders"
TTS_CACHE_DIR = ASSETS_DIR / "tts_cache"

for directory in (DATA_DIR, ASSETS_DIR, RENDERS_DIR, TTS_CACHE_DIR):
    directory.mkdir(parents=True, exist_ok=True)
