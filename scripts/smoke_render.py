from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import analyzer
from app.config import ASSETS_DIR, RENDERS_DIR
from app.db import as_json, connect, execute, init_db, one, rows
from app.media import concat_audio, ffmpeg_status
from app.providers import PlaceholderImageProvider, TestToneTTS
from app.renderer import render_episode
from app.subtitles import write_srt
from scripts.create_sample import main as create_sample


def main() -> None:
    init_db()
    create_sample()
    episode = one("SELECT e.* FROM episodes e JOIN projects p ON p.id = e.project_id WHERE p.title = ? ORDER BY e.id LIMIT 1", ("Sample Web Novel",))
    assert episode
    project = one("SELECT * FROM projects WHERE id = ?", (episode["project_id"],))
    characters = rows("SELECT * FROM characters WHERE project_id = ?", (project["id"],))
    drafts = analyzer.analyze_scenes(episode["manuscript"], characters, project["style_bible"], 4)
    with connect() as conn:
        conn.execute("DELETE FROM scenes WHERE episode_id = ?", (episode["id"],))
        conn.execute("DELETE FROM narration_segments WHERE episode_id = ?", (episode["id"],))
        for draft in drafts:
            conn.execute(
                """
                INSERT INTO scenes (
                    episode_id, scene_order, source_start, source_end, summary, characters_present,
                    location_time, visual_action, mood, prompt, negative_constraints, duration_ms, generation_metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode["id"],
                    draft.order,
                    draft.source_start,
                    draft.source_end,
                    draft.summary,
                    as_json(draft.characters_present),
                    draft.location_time,
                    draft.visual_action,
                    draft.mood,
                    draft.prompt,
                    draft.negative_constraints,
                    draft.duration_ms,
                    as_json({"analyzer": "smoke"}),
                ),
            )
    provider = PlaceholderImageProvider()
    scenes = rows("SELECT * FROM scenes WHERE episode_id = ? ORDER BY scene_order", (episode["id"],))
    for scene in scenes:
        image_path = ASSETS_DIR / f"episode_{episode['id']}" / f"scene_{scene['scene_order']}_placeholder.jpg"
        provider.create(scene["prompt"], image_path, scene["scene_order"])
        execute("UPDATE scenes SET image_path = ?, generation_metadata = ? WHERE id = ?", (str(image_path), as_json({"source": "smoke-placeholder"}), scene["id"]))

    tts = TestToneTTS()
    audio_paths = []
    start = 0
    scenes = rows("SELECT * FROM scenes WHERE episode_id = ? ORDER BY scene_order", (episode["id"],))
    with connect() as conn:
        for scene in scenes:
            audio_path = ASSETS_DIR / f"episode_{episode['id']}" / "audio" / f"segment_{scene['scene_order']}.wav"
            tts.synthesize(scene["summary"], "test-tone", audio_path)
            audio_paths.append(audio_path)
            conn.execute(
                "INSERT INTO narration_segments (episode_id, segment_order, text, audio_path, start_ms, end_ms) VALUES (?, ?, ?, ?, ?, ?)",
                (episode["id"], scene["scene_order"], scene["summary"], str(audio_path), start, start + scene["duration_ms"]),
            )
            start += scene["duration_ms"]
    narration = ASSETS_DIR / f"episode_{episode['id']}" / "audio" / "narration.wav"
    concat_audio(audio_paths, narration)
    segments = rows("SELECT * FROM narration_segments WHERE episode_id = ? ORDER BY segment_order", (episode["id"],))
    srt = write_srt(segments, ASSETS_DIR / f"episode_{episode['id']}" / "subtitles.srt")
    output = RENDERS_DIR / f"smoke_episode_{episode['id']}.mp4"
    status = ffmpeg_status()
    if not status.ffmpeg_ok:
        raise SystemExit(f"FFmpeg unavailable: {status.note}")
    render_episode(rows("SELECT * FROM scenes WHERE episode_id = ? ORDER BY scene_order", (episode["id"],)), narration, srt, output)
    assert output.exists() and output.stat().st_size > 100_000
    print(f"Smoke render complete: {output}")


if __name__ == "__main__":
    main()

