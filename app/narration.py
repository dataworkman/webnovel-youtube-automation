from __future__ import annotations

import re


def chunk_text(text: str, max_chars: int = 900) -> list[str]:
    clean = " ".join(text.split())
    if len(clean) <= max_chars:
        return [clean]
    chunks: list[str] = []
    current = ""
    for sentence in clean.replace("?", "?.").replace("!", "!.").split("."):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(current) + len(sentence) + 1 > max_chars and current:
            chunks.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current.strip())
    return chunks


def narration_units(
    text: str,
    character_voices: dict[str, str],
    narrator_voice: str,
    scene_character_names: list[str] | None = None,
) -> list[tuple[str, str, str]]:
    units: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        clean = line.strip()
        if not clean:
            continue
        speaker = ""
        voice = narrator_voice
        spoken_text = clean
        match = re.match(r"^([^:：]{1,30})\s*[:：]\s*(.+)$", clean)
        if match:
            candidate = match.group(1).strip()
            if candidate in character_voices:
                speaker = candidate
                voice = character_voices[candidate]
                spoken_text = match.group(2).strip()
        if speaker:
            for chunk in chunk_text(spoken_text):
                units.append((chunk, voice, speaker))
            continue
        units.extend(quoted_dialogue_units(clean, character_voices, narrator_voice, scene_character_names or []))
    if units:
        return units
    return [(chunk, narrator_voice, "narrator") for chunk in chunk_text(text)]


def quoted_dialogue_units(
    text: str,
    character_voices: dict[str, str],
    narrator_voice: str,
    scene_character_names: list[str],
) -> list[tuple[str, str, str]]:
    quote_pattern = re.compile(r'"([^"]+)"|“([^”]+)”|‘([^’]+)’')
    units: list[tuple[str, str, str]] = []
    cursor = 0
    matched = False
    for match in quote_pattern.finditer(text):
        matched = True
        before = text[cursor : match.start()].strip()
        if before:
            units.extend((chunk, narrator_voice, "narrator") for chunk in chunk_text(before))
        quote_text = next(group for group in match.groups() if group is not None).strip()
        speaker = infer_dialogue_speaker(text, match.start(), match.end(), character_voices, scene_character_names)
        voice = character_voices.get(speaker, narrator_voice)
        units.extend((chunk, voice, speaker or "narrator") for chunk in chunk_text(quote_text))
        cursor = match.end()
    after = text[cursor:].strip()
    if after:
        units.extend((chunk, narrator_voice, "narrator") for chunk in chunk_text(after))
    if matched:
        return units
    return [(chunk, narrator_voice, "narrator") for chunk in chunk_text(text)]


def infer_dialogue_speaker(
    text: str,
    quote_start: int,
    quote_end: int,
    character_voices: dict[str, str],
    scene_character_names: list[str],
) -> str:
    voiced_names = [name for name in character_voices if name]
    window_start = max(0, quote_start - 90)
    window_end = min(len(text), quote_end + 90)
    window = text[window_start:window_end]
    nearby_names = [name for name in voiced_names if name in window]
    if len(nearby_names) == 1:
        return nearby_names[0]
    scene_voiced_names = [name for name in scene_character_names if name in character_voices]
    if len(scene_voiced_names) == 1:
        return scene_voiced_names[0]
    return ""
