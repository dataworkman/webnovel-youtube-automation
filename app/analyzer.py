from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class SceneDraft:
    order: int
    source_start: int
    source_end: int
    summary: str
    characters_present: list[str]
    location_time: str
    visual_action: str
    mood: str
    prompt: str
    negative_constraints: str
    duration_ms: int
    metadata: dict = field(default_factory=lambda: {"analyzer": "local-heuristic-v1"})


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def recommended_scene_count(manuscript: str) -> int:
    text = normalize_text(manuscript)
    # Google ko-KR narration in testing lands around 250-400 Korean chars/min.
    # One key image every 45-75 seconds keeps long episodes from feeling frozen.
    estimated_minutes = max(1.0, len(text) / 320)
    return max(4, min(12, round(estimated_minutes * 1.2)))


def analyze_scenes(manuscript: str, characters: list[dict], style_bible: str, scene_count: int | None = None) -> list[SceneDraft]:
    text = normalize_text(manuscript)
    if not text:
        raise ValueError("Manuscript is empty.")
    scene_count = scene_count or recommended_scene_count(text)
    scene_count = max(1, min(12, scene_count))
    paragraphs = split_units(text, scene_count)
    ranges = _semantic_ranges(paragraphs, scene_count)
    scenes: list[SceneDraft] = []
    for order, (start_i, end_i) in enumerate(ranges, start=1):
        start, end, excerpt = _range_from_indices(paragraphs, start_i, end_i)
        names = [c["name"] for c in characters if c["name"] and c["name"] in excerpt]
        summary = _summarize(excerpt)
        visual_action = _visual_action(excerpt, order, len(ranges))
        mood = _mood(excerpt, order, len(ranges))
        prompt = build_scene_prompt(
            summary=summary,
            visual_action=visual_action,
            mood=mood,
            characters_present=names,
            characters=characters,
            style_bible=style_bible,
            role=scene_role(order, len(ranges)),
        )
        scenes.append(
            SceneDraft(
                order=order,
                source_start=start,
                source_end=end,
                summary=summary,
                characters_present=names,
                location_time="Detected from manuscript context",
                visual_action=visual_action,
                mood=mood,
                prompt=prompt,
                negative_constraints="identity drift, extra fingers, text artifacts, logo, watermark, wrong age, wrong clothing unless specified",
                duration_ms=max(6000, min(45000, int(len(excerpt) * 95))),
            )
        )
    return scenes


def split_units(text: str, scene_count: int) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) < scene_count:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?。？！다])\s+", text) if s.strip()]
        paragraphs = sentences if len(sentences) >= scene_count else _chunk_by_chars(text, scene_count)
    return paragraphs


def _semantic_ranges(paragraphs: list[str], scene_count: int) -> list[tuple[int, int]]:
    n = len(paragraphs)
    if scene_count >= n:
        return [(i, i + 1) for i in range(n)]
    ranges: list[tuple[int, int]] = []
    for i in range(scene_count):
        start = round(i * n / scene_count)
        end = round((i + 1) * n / scene_count)
        if ranges and start < ranges[-1][1]:
            start = ranges[-1][1]
        end = max(start + 1, min(n, end))
        ranges.append((start, end))
    ranges[-1] = (ranges[-1][0], n)
    return ranges


def _range_from_indices(paragraphs: list[str], start_i: int, end_i: int) -> tuple[int, int, str]:
    starts: list[int] = []
    cursor = 0
    joined = "\n\n".join(paragraphs)
    for paragraph in paragraphs:
        starts.append(cursor)
        cursor += len(paragraph) + 2
    excerpt = "\n\n".join(paragraphs[start_i:end_i])
    return starts[start_i], min(len(joined), starts[start_i] + len(excerpt)), excerpt


def _summarize(text: str) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean[:220] + ("..." if len(clean) > 220 else "")


def _visual_action(text: str, order: int, total: int = 4) -> str:
    verbs = ("바라", "달리", "붙잡", "열", "서", "웃", "울", "검", "말했", "돌아")
    hit = next((v for v in verbs if v in text), None)
    if hit:
        return f"Depict the moment around the action '{hit}' with clear body language."
    if order == 1:
        return "Opening establishing moment"
    if order == total:
        return "Closing hook"
    if order >= max(2, round(total * 0.65)):
        return "Conflict, reveal, or escalation"
    return "Character-driven development"


def _mood(text: str, order: int, total: int = 4) -> str:
    if any(w in text for w in ("공포", "피", "죽", "비명", "어둠")):
        return "tense, ominous"
    if any(w in text for w in ("웃", "따뜻", "햇살", "평온")):
        return "warm, hopeful"
    if order == 1:
        return "intriguing"
    if order == total:
        return "suspenseful"
    if order >= max(2, round(total * 0.65)):
        return "dramatic"
    return "emotional"


def scene_role(order: int, total: int = 4) -> str:
    if order == 1:
        return "opening hook"
    if order == total:
        return "closing hook or cliffhanger"
    if order >= max(2, round(total * 0.65)):
        return "conflict, reveal, or escalation"
    return "story development"


def _character_line(character: dict) -> str:
    identity_lock = character.get("identity_lock") or ""
    lock_note = f" Identity lock: {identity_lock}" if identity_lock else ""
    return f"- {character['name']}: {character.get('description_json', '')}{lock_note}"


def build_scene_prompt(
    summary: str,
    visual_action: str,
    mood: str,
    characters_present: list[str],
    characters: list[dict],
    style_bible: str,
    role: str = "story beat",
    location_time: str = "",
    scene_description: str = "",
) -> str:
    names = list(dict.fromkeys(characters_present))
    character_bibles = "\n".join(_character_line(c) for c in characters if c["name"] in names)
    identity_guard = ""
    if names:
        identity_guard = "\n".join(
            [
                "IDENTITY LOCK:",
                "Use the provided character reference images as the source of truth for face, age, hairstyle, body proportions, and recurring outfit details.",
                "Do not redesign, beautify into a different person, change age, change facial structure, swap hair color, or invent a new costume unless the scene explicitly requires it.",
                "Allowed variations: pose, facial expression, camera angle, lighting, weather, and background.",
            ]
        )
    return "\n".join(
        part
        for part in [
            "16:9 Korean webtoon illustration, cinematic still frame.",
            f"Scene role: {role}.",
            f"Scene description: {scene_description}" if scene_description else f"Summary: {summary}",
            f"Location/time: {location_time}" if location_time else "",
            f"Visual action: {visual_action}",
            f"Mood: {mood}",
            f"Characters present: {', '.join(names) if names else 'no recurring character explicitly detected'}",
            f"Character bible:\n{character_bibles}" if character_bibles else "",
            identity_guard,
            f"Style bible: {style_bible}" if style_bible else "Style bible: polished webtoon, expressive faces, clean line art, dramatic lighting.",
            "Preserve character identity exactly across every episode and every scene.",
        ]
        if part
    )


def _chunk_by_chars(text: str, count: int) -> list[str]:
    size = max(1, len(text) // count)
    return [text[i : i + size] for i in range(0, len(text), size)][:count]
