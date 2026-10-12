from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import as_json, execute, init_db, one, utc_now


MANUSCRIPT = """비가 내리는 골목 끝에서 유나는 낡은 우산을 접었다. 오늘 밤, 오래된 서점의 불빛만이 거리를 지키고 있었다.

서점 안쪽에서 민재가 먼지 쌓인 책을 꺼냈다. 표지에는 두 사람이 어릴 적 보았던 문양이 선명하게 새겨져 있었다.

하지만 책장을 넘기는 순간, 검은 연기가 피어오르며 숨겨진 편지가 바닥으로 떨어졌다. 편지에는 유나의 아버지가 남긴 마지막 경고가 적혀 있었다.

그때 닫혀 있던 지하실 문이 저절로 열렸다. 어둠 속에서 누군가 유나의 이름을 불렀고, 민재는 굳은 얼굴로 손전등을 들었다."""


def main() -> None:
    init_db()
    existing = one("SELECT * FROM projects WHERE title = ?", ("Sample Web Novel",))
    if existing:
        print(f"Sample project already exists: {existing['id']}")
        return
    project_id = execute(
        "INSERT INTO projects (title, language, style_bible, default_voice, subtitle_settings, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "Sample Web Novel",
            "ko",
            "Korean webtoon, expressive faces, clean line art, cinematic lighting, rainy urban fantasy mood.",
            "test-tone",
            as_json({"burn_in": True}),
            utc_now(),
        ),
    )
    execute(
        "INSERT INTO characters (project_id, name, description_json, reference_image_paths) VALUES (?, ?, ?, ?)",
        (
            project_id,
            "유나",
            '{"age_range":"early 20s","hair":"long black hair","clothing":"cream trench coat","distinguishing_features":"silver pendant","personality":"brave but guarded"}',
            "[]",
        ),
    )
    execute(
        "INSERT INTO characters (project_id, name, description_json, reference_image_paths) VALUES (?, ?, ?, ?)",
        (
            project_id,
            "민재",
            '{"age_range":"early 20s","hair":"short brown hair","clothing":"dark knit sweater","distinguishing_features":"round glasses","personality":"careful and loyal"}',
            "[]",
        ),
    )
    episode_id = execute(
        "INSERT INTO episodes (project_id, number, title, manuscript, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, 1, "비밀 서점의 문", MANUSCRIPT, "DRAFT", utc_now()),
    )
    print(f"Created project {project_id}, episode {episode_id}")


if __name__ == "__main__":
    main()

