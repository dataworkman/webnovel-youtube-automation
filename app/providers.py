from __future__ import annotations

import base64
import hashlib
import math
import os
import shutil
import subprocess
import threading
import time
import wave
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

from .config import TTS_CACHE_DIR
from .media import get_ffmpeg_path, korean_font_file, run_ffmpeg

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

GOOGLE_RECOMMENDED_KO_VOICE = os.getenv("GOOGLE_TTS_DEFAULT_VOICE", "ko-KR-Chirp3-HD-Charon")


class ProviderError(RuntimeError):
    pass


class TTSProvider(ABC):
    @abstractmethod
    def list_voices(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> tuple[bool, str]:
        raise NotImplementedError

    @abstractmethod
    def synthesize(self, text: str, voice: str, output_path: Path, settings: dict[str, Any] | None = None) -> Path:
        raise NotImplementedError


class LocalCommandTTS(TTSProvider):
    def __init__(self) -> None:
        self.command = self._detect()

    def _detect(self) -> str | None:
        for command in ("say", "espeak-ng", "espeak", "pico2wave"):
            if shutil.which(command):
                return command
        return None

    def list_voices(self) -> list[str]:
        if self.command == "say":
            return ["Yuna", "Yuri", "Seoyeon", "default"]
        if self.command in {"espeak", "espeak-ng"}:
            return ["ko", "en"]
        if self.command == "pico2wave":
            return ["en-US"]
        return []

    def health_check(self) -> tuple[bool, str]:
        if not self.command:
            return False, "No local TTS command found. Install say, espeak-ng, espeak, or pico2wave."
        return True, f"Using {self.command}"

    def synthesize(self, text: str, voice: str, output_path: Path, settings: dict[str, Any] | None = None) -> Path:
        if not self.command:
            raise ProviderError("No local TTS command is installed.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.command == "say":
            aiff = output_path.with_suffix(".aiff")
            cmd = ["say", "-v", voice or "Yuna", "-o", str(aiff), text]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            subprocess.run([get_ffmpeg_path(), "-y", "-i", str(aiff), str(output_path)], check=True, capture_output=True, text=True)
            aiff.unlink(missing_ok=True)
            return output_path
        if self.command in {"espeak", "espeak-ng"}:
            cmd = [self.command, "-v", voice or "ko", "-w", str(output_path), text]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return output_path
        wav = output_path.with_suffix(".wav")
        subprocess.run(["pico2wave", "-w", str(wav), text], check=True, capture_output=True, text=True)
        return wav


class TestToneTTS(TTSProvider):
    """Deterministic audio provider for smoke tests, not a production narration voice."""

    def list_voices(self) -> list[str]:
        return ["test-tone"]

    def health_check(self) -> tuple[bool, str]:
        return True, "Test tone provider available for render smoke tests."

    def synthesize(self, text: str, voice: str, output_path: Path, settings: dict[str, Any] | None = None) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        duration = max(2.0, min(45.0, len(text) / 8.0))
        sample_rate = 44_100
        frames = int(duration * sample_rate)
        with wave.open(str(output_path), "w") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            for i in range(frames):
                amp = int(9000 * math.sin(2 * math.pi * 220 * (i / sample_rate)))
                wav.writeframesraw(amp.to_bytes(2, "little", signed=True))
        return output_path


class GoogleCloudTTS(TTSProvider):
    def __init__(self) -> None:
        self._client = None

    def _has_credentials_hint(self) -> bool:
        adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        return bool(os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("GOOGLE_CLOUD_PROJECT") or adc.exists())

    def _load_client(self):
        if not self._has_credentials_hint():
            raise ProviderError("Google Cloud credentials are not configured.")
        if self._client is not None:
            return self._client
        try:
            from google.cloud import texttospeech

            self._client = texttospeech.TextToSpeechClient()
            return self._client
        except Exception as exc:
            raise ProviderError(f"Google Cloud TTS unavailable: {exc}") from exc

    def list_voices(self) -> list[str]:
        if not self._has_credentials_hint():
            return []
        try:
            response = self._load_client().list_voices(language_code="ko-KR")
            voices = sorted(
                (voice.name for voice in response.voices if "ko-KR" in voice.language_codes),
                key=_google_voice_sort_key,
            )
            return voices or [GOOGLE_RECOMMENDED_KO_VOICE]
        except Exception:
            return []

    def health_check(self) -> tuple[bool, str]:
        if not self._has_credentials_hint():
            return False, "Google Cloud credentials are not configured."
        try:
            voices = self.list_voices()
            if not voices:
                return False, "Google Cloud TTS credentials are not configured or ko-KR voices could not be listed."
            return True, f"Google Cloud TTS available ({len(voices)} ko-KR voices)."
        except Exception as exc:
            return False, str(exc)

    def synthesize(self, text: str, voice: str, output_path: Path, settings: dict[str, Any] | None = None) -> Path:
        from google.cloud import texttospeech

        settings = settings or {}
        output_path.parent.mkdir(parents=True, exist_ok=True)
        voice_name = voice or GOOGLE_RECOMMENDED_KO_VOICE
        request = texttospeech.SynthesizeSpeechRequest(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(language_code="ko-KR", name=voice_name),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.LINEAR16,
                speaking_rate=float(settings.get("speaking_rate", 1.0)),
            ),
        )
        response = self._load_client().synthesize_speech(request=request)
        output_path.write_bytes(response.audio_content)
        return output_path


class ImageProvider(ABC):
    @abstractmethod
    def create(self, prompt: str, output_path: Path, scene_number: int, reference_paths: list[Path] | None = None) -> Path:
        raise NotImplementedError


class PlaceholderImageProvider(ImageProvider):
    def create(self, prompt: str, output_path: Path, scene_number: int, reference_paths: list[Path] | None = None) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        colors = [("#101820", "#f2aa4c"), ("#203a43", "#e6dada"), ("#2d3436", "#81ecec"), ("#42275a", "#fbc2eb")]
        bg, fg = colors[(scene_number - 1) % len(colors)]
        image = Image.new("RGB", (1920, 1080), bg)
        draw = ImageDraw.Draw(image)
        title_font = _load_font(True, 72)
        body_font = _load_font(False, 36)
        draw.text((90, 90), f"Scene {scene_number}", fill=fg, font=title_font)
        wrapped = _wrap(prompt, 58)[:10]
        y = 220
        for line in wrapped:
            draw.text((90, y), line, fill="white", font=body_font)
            y += 52
        image.save(output_path, quality=94)
        return output_path


class OpenAIImageProvider(ImageProvider):
    def __init__(
        self,
        model: str | None = None,
        quality: str | None = None,
        size: str | None = None,
    ) -> None:
        self.model = model or os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1-mini")
        self.quality = quality or os.getenv("OPENAI_IMAGE_QUALITY", "medium")
        self.size = size or os.getenv("OPENAI_IMAGE_SIZE", "1536x1024")

    def health_check(self) -> tuple[bool, str]:
        if not os.getenv("OPENAI_API_KEY"):
            return False, "OPENAI_API_KEY is not configured."
        try:
            from openai import OpenAI

            OpenAI()
            return True, f"OpenAI Images available ({self.model}, {self.size}, {self.quality})."
        except Exception as exc:
            return False, str(exc)

    def create(self, prompt: str, output_path: Path, scene_number: int, reference_paths: list[Path] | None = None) -> Path:
        from openai import OpenAI

        ok, reason = self.health_check()
        if not ok:
            raise ProviderError(reason)
        client = OpenAI()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        reference_paths = [path for path in (reference_paths or []) if path.exists()]
        if reference_paths:
            handles = [path.open("rb") for path in reference_paths]
            try:
                result = client.images.edit(
                    model=self.model,
                    image=handles,
                    prompt=prompt,
                    size=self.size,
                    quality=self.quality,
                )
            finally:
                for handle in handles:
                    handle.close()
        else:
            result = client.images.generate(
                model=self.model,
                prompt=prompt,
                size=self.size,
                quality=self.quality,
                n=1,
            )
        image_data = result.data[0]
        b64 = getattr(image_data, "b64_json", None)
        if not b64:
            raise ProviderError("OpenAI image response did not include b64 image data.")
        output_path.write_bytes(base64.b64decode(b64))
        return output_path


def get_tts_provider(name: str) -> TTSProvider:
    if name == "google":
        return GoogleCloudTTS()
    if name == "test":
        return TestToneTTS()
    return LocalCommandTTS()


def tts_cache_key(provider_key: str, text: str, voice: str, speaking_rate: float) -> str:
    return hashlib.sha256(f"{provider_key}|{voice}|{speaking_rate}|{text}".encode("utf-8")).hexdigest()[:32]


def tts_cache_path(provider_key: str, text: str, voice: str, speaking_rate: float, audio_format: str = "wav") -> Path:
    return TTS_CACHE_DIR / f"{tts_cache_key(provider_key, text, voice, speaking_rate)}.{audio_format}"


def is_tts_cached(provider_key: str, text: str, voice: str, speaking_rate: float, audio_format: str = "wav") -> bool:
    path = tts_cache_path(provider_key, text, voice, speaking_rate, audio_format)
    return path.exists() and path.stat().st_size > 0


_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()


def _key_lock(key: str) -> threading.Lock:
    with _key_locks_guard:
        return _key_locks.setdefault(key, threading.Lock())


def synthesize_cached(
    tts: TTSProvider,
    provider_key: str,
    text: str,
    voice: str,
    speaking_rate: float,
    attempts: int = 3,
    audio_format: str = "wav",
) -> tuple[Path, bool]:
    """Content-addressed TTS shared by video and reading modes.

    Identical (provider, voice, rate, text) never hits the API twice. Video narration needs WAV
    for exact durations; reading mode stores compact MP3 and reuses an existing WAV without an API call.
    """
    key = tts_cache_key(provider_key, text, voice, speaking_rate)
    path = TTS_CACHE_DIR / f"{key}.{audio_format}"
    wav_path = TTS_CACHE_DIR / f"{key}.wav"
    with _key_lock(key):
        if path.exists() and path.stat().st_size > 0:
            return path, True
        called_api = False
        if not (wav_path.exists() and wav_path.stat().st_size > 0):
            partial = TTS_CACHE_DIR / f"{key}.part.wav"
            for attempt in range(attempts):
                try:
                    tts.synthesize(text, voice, partial, {"speaking_rate": speaking_rate})
                    break
                except ProviderError:
                    raise
                except Exception:
                    if attempt == attempts - 1:
                        raise
                    time.sleep(2**attempt)
            called_api = True
            os.replace(partial, wav_path)
        if audio_format == "wav":
            return wav_path, not called_api
        partial_out = TTS_CACHE_DIR / f"{key}.part.{audio_format}"
        run_ffmpeg(["-y", "-i", str(wav_path), "-ac", "1", "-codec:a", "libmp3lame", "-q:a", "4", "-f", audio_format, str(partial_out)])
        os.replace(partial_out, path)
        wav_path.unlink(missing_ok=True)
        return path, not called_api


def wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as wav:
        frames = wav.getnframes()
        rate = wav.getframerate()
    return int((frames / rate) * 1000)


def _google_voice_sort_key(name: str) -> tuple[int, str]:
    if name == GOOGLE_RECOMMENDED_KO_VOICE:
        return (-1, name)
    tiers = ["Chirp3-HD", "Chirp", "Neural2", "Wavenet", "Standard", "Studio", "News"]
    for index, tier in enumerate(tiers):
        if tier.lower() in name.lower():
            return (index, name)
    return (len(tiers), name)


def _load_font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    found = korean_font_file(bold)
    if found:
        return ImageFont.truetype(found[0], size, index=found[1])
    return ImageFont.truetype("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", size)


def _wrap(text: str, width: int) -> list[str]:
    words = text.replace("\n", " ").split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        if sum(len(w) for w in current) + len(current) + len(word) > width:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines
