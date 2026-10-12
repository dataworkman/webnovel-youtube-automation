from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg


@dataclass
class ToolStatus:
    ffmpeg_path: str | None
    ffprobe_path: str | None
    ffmpeg_ok: bool
    ffprobe_ok: bool
    note: str


def get_ffmpeg_path() -> str:
    return shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()


def get_ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def ffmpeg_status() -> ToolStatus:
    ffmpeg = get_ffmpeg_path()
    ffprobe = get_ffprobe_path()
    ffmpeg_ok = _runs([ffmpeg, "-version"]) if ffmpeg else False
    ffprobe_ok = _runs([ffprobe, "-version"]) if ffprobe else False
    note = "System ffprobe is missing; render can continue, but duration probing is limited." if not ffprobe_ok else "Media tools available."
    return ToolStatus(ffmpeg, ffprobe, ffmpeg_ok, ffprobe_ok, note)


def _runs(cmd: list[str | None]) -> bool:
    if not cmd[0]:
        return False
    try:
        subprocess.run([str(part) for part in cmd], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


_KOREAN_FONT_PREFERENCES = ("Noto Sans CJK KR", "Noto Sans KR", "NanumGothic", "Nanum Gothic", "Malgun Gothic", "Apple SD Gothic Neo")


def korean_font_family() -> str:
    override = os.getenv("SUBTITLE_FONT")
    if override:
        return override
    try:
        out = subprocess.run(["fc-list", ":lang=ko", "family"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return _KOREAN_FONT_PREFERENCES[0]
    installed = {name.strip() for line in out.splitlines() for name in line.split(",")}
    return next((name for name in _KOREAN_FONT_PREFERENCES if name in installed), next(iter(sorted(installed)), _KOREAN_FONT_PREFERENCES[0]))


def korean_font_file(bold: bool = False) -> tuple[str, int] | None:
    try:
        out = subprocess.run(
            ["fc-match", "-f", "%{file}\n%{index}", f"{korean_font_family()}:lang=ko{':bold' if bold else ''}"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.splitlines()
        return (out[0], int(out[1])) if len(out) == 2 and out[0] else None
    except Exception:
        return None


def run_ffmpeg(args: list[str], timeout: int = 180) -> None:
    cmd = [get_ffmpeg_path(), *args]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)


def concat_audio(inputs: list[Path], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    list_file = output.with_suffix(".txt")
    list_file.write_text("".join(f"file '{p.resolve()}'\n" for p in inputs), encoding="utf-8")
    try:
        run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(output)])
    finally:
        list_file.unlink(missing_ok=True)
    return output
