from __future__ import annotations

import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable
from pathlib import Path

from .media import korean_font_family, run_ffmpeg

FPS = 30


def render_episode(
    scenes: list[dict],
    narration_audio: Path,
    srt_path: Path,
    output_path: Path,
    bgm_path: Path | None = None,
    bgm_volume: float = 0.12,
    on_progress: Callable[[int, int, str], None] | None = None,
    burn_subtitles: bool = True,
) -> Path:
    if not scenes:
        raise ValueError("Render requires at least one scene.")
    for scene in scenes:
        image = scene.get("image_path")
        if not image or not Path(image).exists():
            raise ValueError(f"Scene {scene['scene_order']} is missing an image.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    work = output_path.parent / f"{output_path.stem}_work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    try:
        jobs: list[tuple[dict, Path, int]] = []
        elapsed_frames = 0
        elapsed_ms = 0
        for scene in scenes:
            elapsed_ms += max(1000, scene["duration_ms"])
            frames = max(FPS, round(elapsed_ms * FPS / 1000) - elapsed_frames)
            elapsed_frames += frames
            jobs.append((scene, work / f"scene_{scene['scene_order']}.mp4", frames))
        total_steps = len(jobs) + 1
        report = on_progress or (lambda *_: None)
        report(0, total_steps, "장면 클립 만드는 중")
        with ThreadPoolExecutor(max_workers=max(1, min(os.cpu_count() or 1, 4))) as pool:
            futures = [pool.submit(_render_clip, *job) for job in jobs]
            for done, future in enumerate(as_completed(futures), start=1):
                future.result()
                report(done, total_steps, f"장면 클립 {done}/{len(jobs)}")
        report(len(jobs), total_steps, "자막 입히고 최종 인코딩 중" if burn_subtitles else "최종 인코딩 중")
        clips = [clip for _, clip, _ in jobs]

        list_file = work / "clips.txt"
        list_file.write_text("".join(f"file '{clip.resolve()}'\n" for clip in clips), encoding="utf-8")

        escaped_srt = str(srt_path.resolve()).replace("\\", "\\\\").replace("'", r"\'").replace(":", r"\:")
        subtitle_filter = (
            f"subtitles='{escaped_srt}':original_size=1920x1080:force_style='FontName={korean_font_family()},"
            "FontSize=40,Bold=1,Outline=3,Shadow=1,MarginV=60,Alignment=2'"
        )
        video_filter = subtitle_filter if burn_subtitles else "null"
        inputs = ["-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-i", str(narration_audio)]
        if bgm_path and bgm_path.exists():
            inputs += ["-i", str(bgm_path)]
            audio_filter = f"[1:a]volume=1.0[a0];[2:a]aloop=loop=-1:size=2e+09,volume={bgm_volume}[a1];[a0][a1]amix=inputs=2:duration=first:dropout_transition=2[a]"
        else:
            audio_filter = "[1:a]anull[a]"
        run_ffmpeg(
            [
                *inputs,
                "-filter_complex",
                f"[0:v]{video_filter}[v];{audio_filter}",
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(FPS),
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                "-shortest",
                str(output_path),
            ],
            timeout=1800,
        )
        return output_path
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _render_clip(scene: dict, clip: Path, frames: int) -> None:
    duration = frames / FPS
    fade = min(0.25, duration / 4)
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(scene["image_path"]),
            "-vf",
            (
                "scale=3840:2160:force_original_aspect_ratio=increase,crop=3840:2160,"
                f"{ken_burns_filter(scene['scene_order'], frames)},"
                f"fade=t=in:st=0:d={fade:.3f},fade=t=out:st={duration - fade:.3f}:d={fade:.3f},"
                "format=yuv420p"
            ),
            "-frames:v",
            str(frames),
            "-an",
            "-c:v",
            "libx264",
            "-r",
            str(FPS),
            "-preset",
            "ultrafast",
            "-crf",
            "14",
            str(clip),
        ],
        timeout=1800,
    )


def settings_json(**values: object) -> str:
    return json.dumps(values, ensure_ascii=False)


def ken_burns_filter(scene_order: int, frames: int) -> str:
    progress = f"on/{frames}"
    zoom = f"z='min(1.0+0.10*{progress},1.10)'"
    patterns = [
        (f"x='(iw-iw/zoom)*{progress}'", "y='(ih-ih/zoom)/2'"),
        (f"x='(iw-iw/zoom)*(1-{progress})'", "y='(ih-ih/zoom)/2'"),
        ("x='(iw-iw/zoom)/2'", f"y='(ih-ih/zoom)*{progress}'"),
        ("x='(iw-iw/zoom)/2'", f"y='(ih-ih/zoom)*(1-{progress})'"),
        (f"x='(iw-iw/zoom)*{progress}'", f"y='(ih-ih/zoom)*{progress}'"),
        (f"x='(iw-iw/zoom)*(1-{progress})'", f"y='(ih-ih/zoom)*(1-{progress})'"),
    ]
    x_expr, y_expr = patterns[(scene_order - 1) % len(patterns)]
    return f"zoompan={zoom}:{x_expr}:{y_expr}:d={frames}:s=1920x1080:fps={FPS}"
