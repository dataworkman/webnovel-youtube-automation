from __future__ import annotations

import json
import os

from .analyzer import SceneDraft, build_scene_prompt, normalize_text, recommended_scene_count, scene_role, split_units

DEFAULT_NEGATIVE = "identity drift, extra fingers, text artifacts, logo, watermark, wrong age, wrong clothing unless specified"

SYSTEM_PROMPT = """You are a storyboard director turning a Korean web-novel episode into key images for a narrated YouTube video.
You receive numbered paragraphs. Split them into exactly N consecutive scenes at real scene boundaries
(change of place, time, speaker focus, or emotional beat), not at equal sizes.
Return JSON only: {"scenes":[{"start":int,"end":int,"summary":str,"characters":[str],"location_time":str,
"visual_action":str,"mood":str,"scene_description":str}]}
Rules:
- start/end are inclusive paragraph numbers; scenes must be in order, contiguous, and cover every paragraph.
- summary: one Korean sentence (max 120 chars) of what happens.
- characters: only names from the provided character list that visibly appear in the scene.
- location_time: short English phrase, e.g. "rainy alley, night".
- visual_action: one English sentence describing the single most image-worthy moment, with body language.
- mood: 2-3 English words.
- scene_description: 2-3 English sentences describing the frame (setting, composition, lighting, expressions). No text or captions in the image."""


class AnalyzerError(RuntimeError):
    pass


def llm_available() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def analyze_scenes_llm(
    manuscript: str,
    characters: list[dict],
    style_bible: str,
    scene_count: int | None = None,
) -> list[SceneDraft]:
    if not llm_available():
        raise AnalyzerError("OPENAI_API_KEY is not configured.")
    text = normalize_text(manuscript)
    if not text:
        raise ValueError("Manuscript is empty.")
    scene_count = max(1, min(12, scene_count or recommended_scene_count(text)))
    units = split_units(text, scene_count)
    scene_count = min(scene_count, len(units))

    names = [c["name"] for c in characters if c["name"]]
    numbered = "\n\n".join(f"[{i}] {unit}" for i, unit in enumerate(units, start=1))
    user = f"N = {scene_count}\nCharacters: {', '.join(names) if names else '(none defined)'}\n\nParagraphs:\n{numbered}"

    payload = _call_llm(user)
    return _build_drafts(payload, units, text, characters, style_bible, scene_count)


def _call_llm(user: str) -> dict:
    from openai import OpenAI

    model = os.getenv("OPENAI_ANALYZER_MODEL", "gpt-4.1-mini")
    try:
        response = OpenAI().chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        return json.loads(response.choices[0].message.content or "{}")
    except Exception as exc:
        raise AnalyzerError(f"LLM analysis failed: {exc}") from exc


def _build_drafts(payload: dict, units: list[str], text: str, characters: list[dict], style_bible: str, scene_count: int) -> list[SceneDraft]:
    raw = payload.get("scenes")
    if not isinstance(raw, list) or not raw:
        raise AnalyzerError("LLM returned no scenes.")
    n = len(units)
    known = {c["name"] for c in characters if c["name"]}

    try:
        raw = sorted(raw, key=lambda s: int(s["start"]))
        starts = [int(s["start"]) for s in raw]
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyzerError(f"LLM scenes have invalid ranges: {exc}") from exc
    starts[0] = 1
    starts = [max(prev + 1, s) for prev, s in zip([0] + starts, starts)]
    if starts[-1] > n:
        raise AnalyzerError("LLM scene ranges exceed the manuscript.")
    ends = [s - 1 for s in starts[1:]] + [n]

    offsets: list[int] = []
    cursor = 0
    for unit in units:
        offsets.append(cursor)
        cursor += len(unit) + 2

    drafts: list[SceneDraft] = []
    for order, (scene, start, end) in enumerate(zip(raw, starts, ends), start=1):
        excerpt = "\n\n".join(units[start - 1 : end])
        present = [name for name in dict.fromkeys(scene.get("characters") or []) if name in known]
        summary = str(scene.get("summary") or excerpt[:120]).strip()
        visual_action = str(scene.get("visual_action") or "").strip()
        mood = str(scene.get("mood") or "emotional").strip()
        location_time = str(scene.get("location_time") or "unspecified").strip()
        description = str(scene.get("scene_description") or "").strip()
        prompt = build_scene_prompt(
            summary=summary,
            visual_action=visual_action,
            mood=mood,
            characters_present=present,
            characters=characters,
            style_bible=style_bible,
            role=scene_role(order, len(starts)),
            location_time=location_time,
            scene_description=description,
        )
        drafts.append(
            SceneDraft(
                order=order,
                source_start=offsets[start - 1],
                source_end=min(len(text), offsets[start - 1] + len(excerpt)),
                summary=summary,
                characters_present=present,
                location_time=location_time,
                visual_action=visual_action,
                mood=mood,
                prompt=prompt,
                negative_constraints=DEFAULT_NEGATIVE,
                duration_ms=max(6000, min(45000, int(len(excerpt) * 95))),
                metadata={"analyzer": f"llm:{os.getenv('OPENAI_ANALYZER_MODEL', 'gpt-4.1-mini')}", "scene_description": description},
            )
        )
    return drafts

