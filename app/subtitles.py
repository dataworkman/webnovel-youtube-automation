from __future__ import annotations

from pathlib import Path

LINE_CHARS = 20
MAX_LINES = 2
MIN_CUE_MS = 800


def ms_to_srt(ms: int) -> str:
    seconds, milli = divmod(ms, 1000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02}:{minute:02}:{sec:02},{milli:03}"


def write_srt(segments: list[dict], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    for segment in segments:
        for text, start_ms, end_ms in split_cues(segment["text"], segment["start_ms"], segment["end_ms"]):
            blocks.append(f"{len(blocks) + 1}\n{ms_to_srt(start_ms)} --> {ms_to_srt(end_ms)}\n{text}\n")
    output_path.write_text("\n".join(blocks), encoding="utf-8")
    return output_path


def split_cues(text: str, start_ms: int, end_ms: int) -> list[tuple[str, int, int]]:
    """Split a segment into cues of at most MAX_LINES lines, timed proportionally to text length; no text is dropped."""
    lines = _wrap_lines(" ".join(text.split()))
    if not lines:
        return []
    groups = ["\n".join(lines[i : i + MAX_LINES]) for i in range(0, len(lines), MAX_LINES)]
    max_cues = max(1, (end_ms - start_ms) // MIN_CUE_MS)
    if len(groups) > max_cues:
        per_cue = -(-len(lines) // max_cues)
        groups = ["\n".join(lines[i : i + per_cue]) for i in range(0, len(lines), per_cue)]
    total_chars = sum(len(g.replace("\n", "")) for g in groups) or 1
    cues: list[tuple[str, int, int]] = []
    cursor = start_ms
    consumed = 0
    for index, group in enumerate(groups):
        consumed += len(group.replace("\n", ""))
        cue_end = end_ms if index == len(groups) - 1 else start_ms + round((end_ms - start_ms) * consumed / total_chars)
        cues.append((group, cursor, cue_end))
        cursor = cue_end
    return cues


def _wrap_lines(clean: str) -> list[str]:
    lines: list[str] = []
    current = ""
    for token in clean.split(" "):
        while len(token) > LINE_CHARS:
            if current:
                lines.append(current)
                current = ""
            lines.append(token[:LINE_CHARS])
            token = token[LINE_CHARS:]
        if current and len(current) + 1 + len(token) > LINE_CHARS:
            lines.append(current)
            current = token
        else:
            current = f"{current} {token}".strip()
    if current:
        lines.append(current)
    return [line for line in lines if line]
