from __future__ import annotations

from pathlib import Path

from .db import from_json, one, rows


def scene_reference_paths(scene: dict) -> list[Path]:
    names = set(from_json(scene.get("characters_present"), []))
    if not names:
        return []
    episode = one("SELECT * FROM episodes WHERE id = ?", (scene["episode_id"],))
    if not episode:
        return []
    characters = rows("SELECT * FROM characters WHERE project_id = ?", (episode["project_id"],))
    paths: list[Path] = []
    for character in characters:
        if character["name"] in names:
            locked = character.get("locked_reference_path")
            if locked:
                paths.append(Path(locked))
            else:
                paths.extend(Path(ref) for ref in from_json(character["reference_image_paths"], []))
    return [path for path in paths if path.exists()]
