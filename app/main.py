from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import analyzer
from .llm_analyzer import AnalyzerError, analyze_scenes_llm, llm_available
from .auth import install_auth
from .config import ASSETS_DIR, RENDERS_DIR
from .db import as_json, connect, execute, from_json, init_db, one, rows, utc_now
from .jobs import ProgressFn, active_job, error_text, latest_job, recover_interrupted, start_job
from .media import concat_audio, ffmpeg_status
from .providers import GoogleCloudTTS, OpenAIImageProvider, PlaceholderImageProvider, get_tts_provider, synthesize_cached, wav_duration_ms
from .narration import narration_units
from .reader import router as reader_router
from .renderer import render_episode, settings_json
from .scenes import scene_reference_paths
from .subtitles import write_srt
from .uploads import save_upload, upload_suffix
from .reader import READER_CSS
from .ui import character_item, option_panel, episode_item, esc, google_voice_option, job_panel, page, project_tile, provider_event_item, render_item, scene_card, voice_preview_form, voice_preview_panel

DEFAULT_SCENE_COUNT = 4

app = FastAPI(title="Web Novel YouTube Automation")
install_auth(app)
init_db()
recover_interrupted()
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")
app.mount("/renders", StaticFiles(directory=RENDERS_DIR), name="renders")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(reader_router)

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
AI_IMAGES_OFF_MESSAGE = "이 프로젝트는 AI 이미지 생성이 꺼져 있어 이미지 API를 호출하지 않습니다. 프로젝트 옵션에서 켠 뒤 다시 시도하세요."
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
DEFAULT_STYLE_BIBLE = "Korean webtoon, expressive faces, clean line art, cinematic lighting, consistent character proportions."


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return page(
        "웹소설 도구",
        f"""
        <style>{READER_CSS}</style>
        <nav class="top-nav"><a href="/settings">설정</a></nav>
        <section class="reader-modes">
          <a class="mode-card" href="/reader">
            <strong>📖 책 읽어주기</strong>
            <span>EPUB을 올려 TTS로 자연스럽게 들으세요. 챕터 선택, 속도 조절, 이어듣기를 지원하며 AI 이미지는 쓰지 않습니다.</span>
          </a>
          <a class="mode-card" href="/youtube">
            <strong>🎬 YouTube 만들기</strong>
            <span>대본을 장면·이미지·내레이션·자막·BGM으로 엮어 영상을 만듭니다. AI 이미지, 자막, BGM은 각각 켜고 끌 수 있습니다.</span>
          </a>
        </section>
        """,
    )


@app.get("/youtube", response_class=HTMLResponse)
def youtube_home() -> str:
    projects = rows("SELECT * FROM projects ORDER BY id DESC")
    body = "".join(project_tile(p) for p in projects) or "<p class='empty-state'>아직 만든 소설이 없습니다.</p>"
    return page(
        "YouTube 만들기",
        f"""
        <nav class="top-nav"><a href="/">홈</a><a href="/settings">설정</a></nav>
        <section class="intro">
          <div>
            <p class="eyebrow">Novel First Workflow</p>
            <h2>먼저 소설을 선택하고, 회차를 하나씩 영상으로 만듭니다.</h2>
          </div>
        </section>
        <section class="choice-grid">
          <div class="panel choice">
          <h2>새 소설 만들기</h2>
          <p class="muted">처음 시작하는 작품이면 여기서 소설 단위를 먼저 만듭니다.</p>
          <form action="/projects" method="post">
            <label>소설 제목 <input name="title" required></label>
            <button>새 소설 시작</button>
          </form>
          </div>
          <div class="panel choice">
            <h2>기존 소설 이어쓰기</h2>
            <p class="muted">이미 만든 작품을 선택해서 다음 회차를 추가합니다.</p>
            <div class="project-list">{body}</div>
          </div>
        </section>
        """,
    )


@app.get("/settings", response_class=HTMLResponse)
def settings() -> str:
    status = ffmpeg_status()
    image_ok, image_note = OpenAIImageProvider().health_check()
    google_ok, google_note = GoogleCloudTTS().health_check()
    return page(
        "설정",
        f"""
        <nav class="top-nav"><a href="/">홈</a><a href="/youtube">소설 선택</a></nav>
        <section class="panel">
          <h2>시스템 상태</h2>
          <p>FFmpeg: {'OK' if status.ffmpeg_ok else 'missing'} <code>{esc(status.ffmpeg_path or '')}</code></p>
          <p>FFprobe: {'OK' if status.ffprobe_ok else 'missing'} <code>{esc(status.ffprobe_path or '')}</code></p>
          <p>{esc(status.note)}</p>
          <p>OpenAI Images: {'available' if image_ok else 'unavailable'} — {esc(image_note)}</p>
          <p>Google Cloud TTS: {'available' if google_ok else 'unavailable'} — {esc(google_note)}</p>
        </section>
        """,
    )


@app.post("/projects")
def create_project(title: str = Form(...)):
    project_id = execute(
        "INSERT INTO projects (title, style_bible, subtitle_settings, created_at) VALUES (?, ?, ?, ?)",
        (title, DEFAULT_STYLE_BIBLE, as_json({"burn_in": True}), utc_now()),
    )
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(project_id: int) -> str:
    project = require_project(project_id)
    episodes = rows("SELECT * FROM episodes WHERE project_id = ? ORDER BY id DESC", (project_id,))
    episode_items = "".join(episode_item(ep) for ep in episodes) or "<p class='empty-state'>아직 추가된 회차가 없습니다.</p>"
    return page(
        project["title"],
        f"""
        <nav class="top-nav"><a href="/">홈</a><a href="/youtube">소설 선택</a><a href="/projects/{project_id}/settings">작품 설정</a></nav>
        <section class="intro">
          <div>
            <p class="eyebrow">Novel Workspace</p>
            <h2>이 소설에 회차를 하나씩 추가합니다.</h2>
          </div>
        </section>
        <section class="panel primary-flow">
          <h2>새 회차 추가</h2>
          <form action="/projects/{project_id}/episodes" method="post">
            <label>회차 제목 <input name="title" required></label>
            <label>대본 / 원고 <textarea name="manuscript" rows="14" required></textarea></label>
            <button>회차 만들기</button>
          </form>
        </section>
        <section class="panel">
          <h2>회차 목록</h2>
          <div class="episode-list">{episode_items}</div>
        </section>
        """,
    )


@app.get("/projects/{project_id}/settings", response_class=HTMLResponse)
def project_settings(project_id: int) -> str:
    project = require_project(project_id)
    characters = rows("SELECT * FROM characters WHERE project_id = ? ORDER BY id", (project_id,))
    google_tts = GoogleCloudTTS()
    google_ok, google_note = google_tts.health_check()
    google_voices = google_tts.list_voices() if google_ok else []
    google_voice_options = "".join(google_voice_option(v) for v in google_voices)
    character_items = "".join(character_item(c, google_voices, bool(project["use_ai_images"])) for c in characters) or "<li>아직 등록된 등장인물이 없습니다.</li>"
    return page(
        f"{project['title']} 설정",
        f"""
        <nav class="top-nav"><a href="/projects/{project_id}">회차 작업</a><a href="/">홈</a><a href="/youtube">소설 선택</a></nav>
        {voice_preview_panel(project_id, google_ok, google_note, google_voice_options)}
        <section class="grid">
          <div class="panel">
            <h2>등장인물</h2>
            <ul>{character_items}</ul>
            <form action="/projects/{project_id}/characters" method="post">
              <label>이름 <input name="name" required></label>
              <label>고정 설정 <textarea name="description_json" placeholder='머리, 눈, 나이대, 복장, 반복 소품'></textarea></label>
              <button>등장인물 추가</button>
            </form>
          </div>
          <div class="panel">
            <h2>그림체 설정</h2>
            <form action="/projects/{project_id}/style" method="post">
              <textarea name="style_bible">{esc(project['style_bible'])}</textarea>
              <button>그림체 저장</button>
            </form>
          </div>
        </section>
        """,
    )


@app.post("/projects/{project_id}/style")
def update_style(project_id: int, style_bible: str = Form("")):
    require_project(project_id)
    execute("UPDATE projects SET style_bible = ? WHERE id = ?", (style_bible, project_id))
    return RedirectResponse(f"/projects/{project_id}/settings", status_code=303)


@app.post("/projects/{project_id}/characters")
def create_character(project_id: int, name: str = Form(...), description_json: str = Form("")):
    require_project(project_id)
    identity_lock = build_identity_lock(name, description_json)
    execute(
        """
        INSERT INTO characters (project_id, name, description_json, reference_image_paths, identity_lock, voice_gender, voice_name)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, name, description_json, "[]", identity_lock, "", ""),
    )
    return RedirectResponse(f"/projects/{project_id}/settings", status_code=303)


@app.post("/characters/{character_id}/references")
async def upload_character_reference(character_id: int, image: UploadFile = File(...), lock_identity: str | None = Form(None)):
    character = one("SELECT * FROM characters WHERE id = ?", (character_id,))
    if not character:
        raise HTTPException(404)
    suffix = upload_suffix(image.filename, IMAGE_SUFFIXES, ".jpg")
    refs = from_json(character["reference_image_paths"], [])
    path = ASSETS_DIR / f"project_{character['project_id']}" / "characters" / f"character_{character_id}_ref_{len(refs) + 1}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        shutil.copyfileobj(image.file, fh)
    refs.append(str(path))
    locked_path = str(path) if lock_identity == "on" or not character.get("locked_reference_path") else character.get("locked_reference_path")
    execute("UPDATE characters SET reference_image_paths = ?, locked_reference_path = ? WHERE id = ?", (as_json(refs), locked_path, character_id))
    return RedirectResponse(f"/projects/{character['project_id']}/settings", status_code=303)


@app.post("/characters/{character_id}/lock-reference")
def lock_character_reference(character_id: int, reference_path: str = Form(...)):
    character = one("SELECT * FROM characters WHERE id = ?", (character_id,))
    if not character:
        raise HTTPException(404)
    refs = from_json(character["reference_image_paths"], [])
    if reference_path not in refs or not Path(reference_path).exists():
        raise HTTPException(400, "Reference image is not registered for this character.")
    execute("UPDATE characters SET locked_reference_path = ? WHERE id = ?", (reference_path, character_id))
    return RedirectResponse(f"/projects/{character['project_id']}/settings", status_code=303)


@app.post("/characters/{character_id}/identity")
def update_character_identity(character_id: int, description_json: str = Form(""), identity_lock: str = Form("")):
    character = one("SELECT * FROM characters WHERE id = ?", (character_id,))
    if not character:
        raise HTTPException(404)
    execute(
        "UPDATE characters SET description_json = ?, identity_lock = ? WHERE id = ?",
        (description_json, identity_lock or build_identity_lock(character["name"], description_json), character_id),
    )
    return RedirectResponse(f"/projects/{character['project_id']}/settings", status_code=303)


@app.post("/characters/{character_id}/voice")
def update_character_voice(character_id: int, voice_gender: str = Form(""), voice_name: str = Form("")):
    character = one("SELECT * FROM characters WHERE id = ?", (character_id,))
    if not character:
        raise HTTPException(404)
    execute(
        "UPDATE characters SET voice_gender = ?, voice_name = ? WHERE id = ?",
        (voice_gender, voice_name, character_id),
    )
    return RedirectResponse(f"/projects/{character['project_id']}/settings", status_code=303)


@app.post("/characters/{character_id}/openai-reference")
def generate_character_reference(character_id: int):
    character = one("SELECT * FROM characters WHERE id = ?", (character_id,))
    if not character:
        raise HTTPException(404)
    project = require_project(character["project_id"])
    if not project["use_ai_images"]:
        raise HTTPException(400, AI_IMAGES_OFF_MESSAGE)
    provider = OpenAIImageProvider()
    ok, reason = provider.health_check()
    if not ok:
        raise HTTPException(400, reason)
    refs = from_json(character["reference_image_paths"], [])
    path = ASSETS_DIR / f"project_{character['project_id']}" / "characters" / f"character_{character_id}_identity_{len(refs) + 1}.png"
    prompt = character_reference_prompt(character, project["style_bible"])
    created = 0
    try:
        provider.create(prompt, path, 0, [])
        created = 1
    finally:
        episode = one("SELECT id FROM episodes WHERE project_id = ? ORDER BY id DESC LIMIT 1", (character["project_id"],))
        if episode:
            record_provider_event(episode["id"], "openai-images", "character_reference", None, 1, created, {"character_id": character_id})
    refs.append(str(path))
    execute(
        "UPDATE characters SET reference_image_paths = ?, locked_reference_path = ? WHERE id = ?",
        (as_json(refs), str(path), character_id),
    )
    return RedirectResponse(f"/projects/{character['project_id']}/settings", status_code=303)


@app.post("/projects/{project_id}/episodes")
def create_episode(project_id: int, title: str = Form(...), manuscript: str = Form(...)):
    require_project(project_id)
    episode_id = execute(
        "INSERT INTO episodes (project_id, number, title, manuscript, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, 1, title, analyzer.normalize_text(manuscript), "DRAFT", utc_now()),
    )
    return RedirectResponse(f"/episodes/{episode_id}", status_code=303)


@app.get("/episodes/{episode_id}", response_class=HTMLResponse)
def episode_detail(episode_id: int) -> str:
    episode = require_episode(episode_id)
    project = require_project(episode["project_id"])
    characters = rows("SELECT * FROM characters WHERE project_id = ? ORDER BY id", (project["id"],))
    scenes = rows("SELECT * FROM scenes WHERE episode_id = ? ORDER BY scene_order", (episode_id,))
    next_scene_count = len(scenes) or DEFAULT_SCENE_COUNT
    pending_images = sum(1 for scene in scenes if not has_openai_image(scene)) if scenes else DEFAULT_SCENE_COUNT
    cached_images = len(scenes) - pending_images if scenes else 0
    ai_on = bool(project["use_ai_images"])
    image_cost_note = f"{pending_images} images will be generated (OpenAI 호출 {pending_images}회)."
    if cached_images:
        image_cost_note += f" 이미 생성된 {cached_images}개는 재사용합니다."
    image_controls = (
        f"""<form action="/episodes/{episode_id}/images" method="post">
              <label>이미지 생성 방식
                <select name="provider">
                  <option value="manual">직접 업로드</option>
                  <option value="openai">OpenAI gpt-image-1-mini</option>
                </select>
              </label>
              <p class="notice">OpenAI 선택 시: {image_cost_note} 버튼을 누르기 전에는 API를 호출하지 않습니다.</p>
              <button>선택한 방식으로 이미지 준비</button>
            </form>"""
        if ai_on
        else f"""<p class="notice">AI 이미지 생성이 꺼져 있습니다. 이미지 API는 호출되지 않습니다. 장면별로 이미지를 직접 올리거나 로컬 기본 이미지를 사용하세요.</p>
            <form action="/episodes/{episode_id}/placeholder-images" method="post">
              <button>로컬 기본 이미지로 채우기 (API 호출 없음)</button>
            </form>"""
    )
    segments = rows("SELECT * FROM narration_segments WHERE episode_id = ? ORDER BY segment_order", (episode_id,))
    renders = rows("SELECT * FROM renders WHERE episode_id = ? ORDER BY id DESC LIMIT 5", (episode_id,))
    provider_events = rows("SELECT * FROM provider_events WHERE episode_id = ? ORDER BY id DESC LIMIT 8", (episode_id,))
    google_tts = GoogleCloudTTS()
    google_ok, google_note = google_tts.health_check()
    google_voice_options = "".join(google_voice_option(v) for v in google_tts.list_voices()) if google_ok else ""
    scene_html = "".join(scene_card(scene, episode_id, characters, ai_on) for scene in scenes) or "<p>No analysis yet.</p>"
    segment_html = "".join(f"<li>{s['start_ms']}–{s['end_ms']} ms: {esc(s['text'][:90])}</li>" for s in segments) or "<li>No narration yet.</li>"
    render_html = "".join(render_item(r) for r in renders) or "<li>No renders yet.</li>"
    bgm_controls = (
        '<label>BGM 선택 <input type="file" name="bgm"></label>\n            <label>BGM 볼륨 <input type="number" name="bgm_volume" value="0.12" step="0.01" min="0" max="1"></label>'
        if project["use_bgm"]
        else ''
    )
    event_html = "".join(provider_event_item(event) for event in provider_events) or "<li>No provider calls recorded.</li>"
    return page(
        episode["title"],
        f"""
        <nav><a href="/projects/{project['id']}">소설 작업 화면</a></nav>
        {job_panel(episode_id)}
        {option_panel(project, f'/episodes/{episode_id}')}
        <section class="panel">
          <h2>1. 대본</h2>
          <form action="/episodes/{episode_id}/script" method="post">
            <label>회차 제목 <input name="title" value="{esc(episode['title'])}" required></label>
            <label>대본 / 원고 <textarea name="manuscript" rows="12">{esc(episode['manuscript'])}</textarea></label>
            <button>대본 저장</button>
          </form>
          <form action="/episodes/{episode_id}/analyze" method="post">
            <label>장면 수 <input type="number" name="scene_count" value="{next_scene_count}" min="0" max="12"></label>
            <p class="notice">기본 {DEFAULT_SCENE_COUNT}장면(이미지 {DEFAULT_SCENE_COUNT}장). 0이면 대본 길이에 맞춰 자동으로 나눕니다 (추천: {analyzer.recommended_scene_count(episode['manuscript'])}개).</p>
            <button>장면 분석</button>
          </form>
        </section>
        <section class="panel">
          <h2>2. 장면과 이미지</h2>
          <div class="scenes">{scene_html}</div>
          <div class="toolbar">
            {image_controls}
          </div>
        </section>
        <section class="panel">
          <h2>3. 내레이션</h2>
          <form action="/episodes/{episode_id}/narration" method="post">
            <label>음성 생성 방식
              <select name="provider">
                <option value="google" {'disabled' if not google_ok else ''}>Google Cloud TTS {'(사용 불가)' if not google_ok else ''}</option>
                <option value="local">로컬 TTS</option>
                <option value="test">테스트 톤</option>
              </select>
            </label>
            <label>Google 한국어 목소리
              <select name="voice">
                {google_voice_options or '<option value="">사용 가능한 Google 목소리 없음</option>'}
              </select>
            </label>
            <label>말하기 속도 <input type="number" name="speaking_rate" value="1.0" step="0.05" min="0.25" max="4"></label>
            <button>내레이션 + 자막 생성</button>
          </form>
          {voice_preview_form(project['id'], google_ok, google_voice_options)}
          <p class="muted">상태: {esc(google_note)}</p>
          <ul>{segment_html}</ul>
        </section>
        <section class="panel">
          <h2>4. 영상 만들기</h2>
          <form action="/episodes/{episode_id}/render" method="post" enctype="multipart/form-data">
            {bgm_controls}
            <p class="muted">자막: {'켜짐' if project['use_subtitles'] else '꺼짐'} · BGM: {'켜짐' if project['use_bgm'] else '꺼짐'}</p>
            <button>MP4 생성</button>
          </form>
          <ul>{render_html}</ul>
        </section>
        <details class="generation-log">
          <summary>생성 기록</summary>
          <ul>{event_html}</ul>
        </details>
        """,
    )


@app.get("/episodes/{episode_id}/job")
def job_status(episode_id: int):
    require_episode(episode_id)
    return latest_job(episode_id) or {}


@app.post("/episodes/{episode_id}/script")
def save_script(episode_id: int, title: str = Form(...), manuscript: str = Form(...)):
    require_episode(episode_id)
    execute("UPDATE episodes SET title = ?, manuscript = ?, status = ? WHERE id = ?", (title, analyzer.normalize_text(manuscript), "DRAFT", episode_id))
    return RedirectResponse(f"/episodes/{episode_id}", status_code=303)


@app.post("/episodes/{episode_id}/analyze")
def analyze_episode(episode_id: int, scene_count: int = Form(DEFAULT_SCENE_COUNT)):
    episode = require_episode(episode_id)
    project = require_project(episode["project_id"])
    characters = rows("SELECT * FROM characters WHERE project_id = ? ORDER BY id", (project["id"],))
    drafts = None
    fallback_reason = ""
    if llm_available():
        try:
            drafts = analyze_scenes_llm(episode["manuscript"], characters, project["style_bible"], scene_count or None)
        except AnalyzerError as exc:
            fallback_reason = str(exc)
    if drafts is None:
        drafts = analyzer.analyze_scenes(episode["manuscript"], characters, project["style_bible"], scene_count or None)
        if fallback_reason:
            for draft in drafts:
                draft.metadata["llm_fallback_reason"] = fallback_reason
    with connect() as conn:
        conn.execute("DELETE FROM scenes WHERE episode_id = ?", (episode_id,))
        for draft in drafts:
            conn.execute(
                """
                INSERT INTO scenes (
                    episode_id, scene_order, source_start, source_end, summary, characters_present,
                    location_time, visual_action, mood, prompt, negative_constraints, duration_ms, generation_metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode_id,
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
                    as_json(draft.metadata),
                ),
            )
        conn.execute("UPDATE episodes SET status = ? WHERE id = ?", ("ANALYZED", episode_id))
    return RedirectResponse(f"/episodes/{episode_id}", status_code=303)


@app.post("/episodes/{episode_id}/scenes/{scene_order}/characters")
def update_scene_characters(episode_id: int, scene_order: int, character_names: list[str] = Form(default=[])):
    episode = require_episode(episode_id)
    project = require_project(episode["project_id"])
    scene = require_scene(episode_id, scene_order)
    characters = rows("SELECT * FROM characters WHERE project_id = ? ORDER BY id", (project["id"],))
    known_names = {character["name"] for character in characters}
    selected = [name for name in character_names if name in known_names]
    prompt = analyzer.build_scene_prompt(
        summary=scene["summary"],
        visual_action=scene["visual_action"],
        mood=scene["mood"],
        characters_present=selected,
        characters=characters,
        style_bible=project["style_bible"],
        location_time=scene["location_time"] if scene["location_time"] != "Detected from manuscript context" else "",
        scene_description=from_json(scene["generation_metadata"], {}).get("scene_description", ""),
    )
    metadata = from_json(scene["generation_metadata"], {})
    metadata["character_assignment"] = "manual"
    execute(
        "UPDATE scenes SET characters_present = ?, prompt = ?, generation_metadata = ? WHERE id = ?",
        (as_json(selected), prompt, as_json(metadata), scene["id"]),
    )
    return RedirectResponse(f"/episodes/{episode_id}", status_code=303)


@app.post("/episodes/{episode_id}/scenes/{scene_order}/image")
async def upload_scene_image(episode_id: int, scene_order: int, image: UploadFile = File(...)):
    require_episode(episode_id)
    scene = require_scene(episode_id, scene_order)
    suffix = upload_suffix(image.filename, IMAGE_SUFFIXES, ".jpg")
    path = ASSETS_DIR / f"episode_{episode_id}" / f"scene_{scene_order}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        shutil.copyfileobj(image.file, fh)
    execute(
        "UPDATE scenes SET image_path = ?, generation_metadata = ? WHERE id = ?",
        (str(path), as_json({"source": "manual-upload", "filename": image.filename}), scene["id"]),
    )
    maybe_update_images_ready(episode_id)
    return RedirectResponse(f"/episodes/{episode_id}", status_code=303)


@app.post("/episodes/{episode_id}/placeholder-images")
def placeholder_images(episode_id: int):
    require_episode(episode_id)
    scenes = rows("SELECT * FROM scenes WHERE episode_id = ? ORDER BY scene_order", (episode_id,))
    if not scenes:
        raise HTTPException(400, "Analyze the episode before generating placeholder images.")
    provider = PlaceholderImageProvider()
    for scene in scenes:
        path = ASSETS_DIR / f"episode_{episode_id}" / f"scene_{scene['scene_order']}_placeholder.jpg"
        provider.create(scene["prompt"], path, scene["scene_order"])
        execute(
            "UPDATE scenes SET image_path = ?, generation_metadata = ? WHERE id = ?",
            (str(path), as_json({"source": "local-placeholder", "regenerated_scene_only": scene["scene_order"]}), scene["id"]),
        )
    execute("UPDATE episodes SET status = ? WHERE id = ?", ("IMAGES_READY", episode_id))
    return RedirectResponse(f"/episodes/{episode_id}", status_code=303)


@app.post("/episodes/{episode_id}/narration")
def generate_narration(episode_id: int, provider: str = Form("local"), voice: str = Form(""), speaking_rate: float = Form(1.0)):
    episode = require_episode(episode_id)
    project = require_project(episode["project_id"])
    characters = rows("SELECT * FROM characters WHERE project_id = ? ORDER BY id", (project["id"],))
    scenes = rows("SELECT * FROM scenes WHERE episode_id = ? ORDER BY scene_order", (episode_id,))
    if not scenes:
        raise HTTPException(400, "Analyze the episode first.")
    tts = get_tts_provider(provider)
    ok, reason = tts.health_check()
    if not ok:
        raise HTTPException(400, reason)
    narrator_voice = voice or (tts.list_voices()[0] if tts.list_voices() else "")
    character_voices = {character["name"]: character.get("voice_name") for character in characters if character.get("voice_name")}
    manuscript = episode["manuscript"]
    start_job(
        episode_id,
        "narration",
        "내레이션 + 자막 생성",
        lambda progress: run_narration(episode_id, manuscript, scenes, tts, provider, narrator_voice, character_voices, speaking_rate, progress),
    )
    return RedirectResponse(f"/episodes/{episode_id}", status_code=303)


def run_narration(
    episode_id: int,
    manuscript: str,
    scenes: list[dict],
    tts,
    provider: str,
    narrator_voice: str,
    character_voices: dict[str, str],
    speaking_rate: float,
    progress: ProgressFn,
) -> None:
    plan = []
    for scene in scenes:
        narration_text = manuscript[scene["source_start"] : scene["source_end"]].strip() or scene["summary"]
        scene_character_names = from_json(scene.get("characters_present"), [])
        plan.append((scene, list(narration_units(narration_text, character_voices, narrator_voice, scene_character_names))))
    total = sum(len(units) for _, units in plan)
    done = 0
    cache_hits = 0
    audio_paths: list[Path] = []
    segments: list[tuple[int, str, str, int, int]] = []
    scene_durations: list[tuple[int, int]] = []
    start = 0
    progress(0, total, "음성 합성 준비 중")
    for scene, units in plan:
        scene_duration = 0
        for chunk, selected_voice, _speaker in units:
            audio_path, hit = synthesize_cached(tts, provider, chunk, selected_voice, speaking_rate)
            cache_hits += int(hit)
            audio_paths.append(audio_path)
            try:
                duration = wav_duration_ms(audio_path)
            except Exception:
                duration = max(1000, int(len(chunk) * 95))
            segments.append((len(segments) + 1, chunk, str(audio_path), start, start + duration))
            start += duration
            scene_duration += duration
            done += 1
            progress(done, total, f"음성 {done}/{total} (캐시 {cache_hits})")
        scene_durations.append((max(1000, scene_duration), scene["id"]))
    progress(total, total, "오디오 합치고 자막 만드는 중")
    combined = ASSETS_DIR / f"episode_{episode_id}" / "audio" / "narration.wav"
    concat_audio(audio_paths, combined)
    with connect() as conn:
        conn.execute("DELETE FROM narration_segments WHERE episode_id = ?", (episode_id,))
        conn.executemany(
            "INSERT INTO narration_segments (episode_id, segment_order, text, audio_path, start_ms, end_ms) VALUES (?, ?, ?, ?, ?, ?)",
            [(episode_id, order, text, path, seg_start, seg_end) for order, text, path, seg_start, seg_end in segments],
        )
        conn.executemany("UPDATE scenes SET duration_ms = ? WHERE id = ?", scene_durations)
    stored = rows("SELECT * FROM narration_segments WHERE episode_id = ? ORDER BY segment_order", (episode_id,))
    write_srt(stored, ASSETS_DIR / f"episode_{episode_id}" / "subtitles.srt")
    record_provider_event(
        episode_id,
        f"tts:{provider}",
        "synthesize",
        None,
        (total - cache_hits) if provider == "google" else 0,
        total,
        {"voice": narrator_voice, "character_voices": character_voices, "speaking_rate": speaking_rate, "cache_hits": cache_hits},
    )
    execute("UPDATE episodes SET status = ? WHERE id = ?", ("SUBTITLES_READY", episode_id))


@app.post("/projects/{project_id}/voice-preview")
def voice_preview(project_id: int, provider: str = Form("google"), voice: str = Form(""), text: str = Form(...), speaking_rate: float = Form(1.0)):
    require_project(project_id)
    tts = get_tts_provider(provider)
    ok, reason = tts.health_check()
    if not ok:
        raise HTTPException(400, reason)
    output = ASSETS_DIR / f"project_{project_id}" / "voice_preview.wav"
    tts.synthesize(text[:500], voice or (tts.list_voices()[0] if tts.list_voices() else ""), output, {"speaking_rate": speaking_rate})
    return Response(output.read_bytes(), media_type="audio/wav")


@app.post("/episodes/{episode_id}/openai-images")
def generate_openai_images(episode_id: int):
    require_ai_images(require_episode(episode_id))
    scenes = rows("SELECT * FROM scenes WHERE episode_id = ? ORDER BY scene_order", (episode_id,))
    if not scenes:
        raise HTTPException(400, "Analyze the episode before generating OpenAI images.")
    provider = OpenAIImageProvider()
    ok, reason = provider.health_check()
    if not ok:
        raise HTTPException(400, reason)
    pending = [scene for scene in scenes if not has_openai_image(scene)]
    start_job(
        episode_id,
        "images",
        "OpenAI 이미지 생성",
        lambda progress: run_image_generation(episode_id, pending, len(scenes), provider, progress, "generate_episode_images"),
    )
    return RedirectResponse(f"/episodes/{episode_id}", status_code=303)


@app.post("/episodes/{episode_id}/images")
def generate_images_with_selected_provider(episode_id: int, provider: str = Form("manual")):
    if provider == "manual":
        return RedirectResponse(f"/episodes/{episode_id}", status_code=303)
    if provider == "openai":
        return generate_openai_images(episode_id)
    raise HTTPException(400, f"Unknown image provider: {provider}")


@app.post("/episodes/{episode_id}/scenes/{scene_order}/openai-image")
def regenerate_openai_image(episode_id: int, scene_order: int):
    require_ai_images(require_episode(episode_id))
    scene = require_scene(episode_id, scene_order)
    provider = OpenAIImageProvider()
    ok, reason = provider.health_check()
    if not ok:
        raise HTTPException(400, reason)
    start_job(
        episode_id,
        "images",
        f"장면 {scene_order} 이미지 다시 생성",
        lambda progress: run_image_generation(episode_id, [scene], 1, provider, progress, "regenerate_scene_image", scene_order),
    )
    return RedirectResponse(f"/episodes/{episode_id}", status_code=303)


IMAGE_WORKERS = 3


def has_openai_image(scene: dict) -> bool:
    return bool(
        from_json(scene["generation_metadata"], {}).get("source") == "openai-images"
        and scene.get("image_path")
        and Path(scene["image_path"]).exists()
    )


def run_image_generation(
    episode_id: int,
    pending: list[dict],
    requested: int,
    provider: OpenAIImageProvider,
    progress: ProgressFn,
    action: str,
    scene_order: int | None = None,
) -> None:
    total = len(pending)
    progress(0, total, "이미지 생성 중" if total else "생성할 이미지가 없습니다")
    failures: list[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=IMAGE_WORKERS) as pool:
        futures = {pool.submit(generate_one_openai_image, episode_id, scene, provider): scene["scene_order"] for scene in pending}
        for future in as_completed(futures):
            done += 1
            try:
                future.result()
            except Exception as exc:
                failures.append(f"장면 {futures[future]}: {error_text(exc)[:200]}")
            progress(done, total, f"이미지 {done}/{total}")
    succeeded = total - len(failures)
    if total:
        record_provider_event(episode_id, "openai-images", action, scene_order, total, succeeded, {"requested_images": requested, "failed": len(failures)})
    maybe_update_images_ready(episode_id)
    if failures:
        raise RuntimeError(f"{len(failures)}개 실패 (성공한 이미지는 저장됨, 다시 실행하면 나머지만 생성): " + " | ".join(failures))


@app.post("/episodes/{episode_id}/render")
async def render(episode_id: int, bgm_volume: float = Form(0.12), bgm: UploadFile | None = File(None)):
    project = require_project(require_episode(episode_id)["project_id"])
    use_bgm = bool(project["use_bgm"])
    burn_subtitles = bool(project["use_subtitles"])
    scenes = rows("SELECT * FROM scenes WHERE episode_id = ? ORDER BY scene_order", (episode_id,))
    narration = ASSETS_DIR / f"episode_{episode_id}" / "audio" / "narration.wav"
    srt = ASSETS_DIR / f"episode_{episode_id}" / "subtitles.srt"
    if not narration.exists() or not srt.exists():
        raise HTTPException(400, "Generate narration and subtitles before rendering.")
    if active_job(episode_id):
        return RedirectResponse(f"/episodes/{episode_id}", status_code=303)
    bgm_path = None
    if use_bgm and bgm and bgm.filename:
        suffix = upload_suffix(bgm.filename, AUDIO_SUFFIXES, ".mp3")
        bgm_path = ASSETS_DIR / f"episode_{episode_id}" / f"bgm{suffix}"
        save_upload(bgm, bgm_path, MAX_UPLOAD_BYTES)
    render_id = execute(
        "INSERT INTO renders (episode_id, status, settings_json, created_at) VALUES (?, ?, ?, ?)",
        (episode_id, "RENDERING", settings_json(bgm_volume=bgm_volume if bgm_path else 0, bgm=bool(bgm_path), burn_subtitles=burn_subtitles), utc_now()),
    )
    execute("UPDATE episodes SET status = ? WHERE id = ?", ("RENDERING", episode_id))
    output = RENDERS_DIR / f"episode_{episode_id}_render_{render_id}.mp4"

    def run_render(progress: ProgressFn) -> None:
        try:
            render_episode(scenes, narration, srt, output, bgm_path, bgm_volume, progress, burn_subtitles)
        except Exception as exc:
            execute("UPDATE renders SET status = ?, error_log = ? WHERE id = ?", ("FAILED", error_text(exc), render_id))
            execute("UPDATE episodes SET status = ? WHERE id = ?", ("SUBTITLES_READY", episode_id))
            raise
        execute("UPDATE renders SET status = ?, output_path = ? WHERE id = ?", ("COMPLETE", str(output), render_id))
        execute("UPDATE episodes SET status = ? WHERE id = ?", ("COMPLETE", episode_id))

    if start_job(episode_id, "render", "MP4 렌더링", run_render) is None:
        execute("UPDATE renders SET status = ?, error_log = ? WHERE id = ?", ("FAILED", "Another job was already running.", render_id))
    return RedirectResponse(f"/episodes/{episode_id}", status_code=303)


@app.get("/renders/{render_id}/download")
def download_render(render_id: int):
    render_row = one("SELECT * FROM renders WHERE id = ?", (render_id,))
    if not render_row or not render_row["output_path"]:
        raise HTTPException(404)
    return FileResponse(render_row["output_path"], media_type="video/mp4", filename=Path(render_row["output_path"]).name)


def build_identity_lock(name: str, description: str) -> str:
    return (
        f"{name} must keep the same face, apparent age, hairstyle, hair color, eye shape, body proportions, "
        f"signature outfit details, and recurring accessories in every scene. Source notes: {description}"
    ).strip()


def character_reference_prompt(character: dict, style_bible: str) -> str:
    identity_lock = character.get("identity_lock") or build_identity_lock(character["name"], character.get("description_json", ""))
    return "\n".join(
        part
        for part in [
            "Create a clean official character identity reference sheet for a Korean webtoon production.",
            f"Character name: {character['name']}",
            f"Character bible: {character.get('description_json', '')}",
            f"Identity lock: {identity_lock}",
            "Show one consistent person only, front-facing or three-quarter view, clear face, clear hairstyle, clear outfit details.",
            "Neutral background, no text, no logo, no watermark, no multiple alternate designs.",
            f"Style bible: {style_bible}" if style_bible else "Style bible: polished Korean webtoon, expressive but consistent proportions.",
        ]
        if part
    )


def generate_one_openai_image(episode_id: int, scene: dict, provider: OpenAIImageProvider) -> Path:
    path = ASSETS_DIR / f"episode_{episode_id}" / f"scene_{scene['scene_order']}_openai.png"
    refs = scene_reference_paths(scene)
    provider.create(scene["prompt"], path, scene["scene_order"], refs)
    execute(
        "UPDATE scenes SET image_path = ?, generation_metadata = ? WHERE id = ?",
        (
            str(path),
            as_json(
                {
                    "source": "openai-images",
                    "model": provider.model,
                    "quality": provider.quality,
                    "size": provider.size,
                    "reference_image_count": len(refs),
                    "identity_locked": bool(refs),
                }
            ),
            scene["id"],
        ),
    )
    return path


def record_provider_event(
    episode_id: int,
    provider: str,
    action: str,
    scene_order: int | None,
    call_count: int,
    asset_count: int,
    metadata: dict,
) -> None:
    execute(
        """
        INSERT INTO provider_events (episode_id, provider, action, scene_order, call_count, asset_count, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (episode_id, provider, action, scene_order, call_count, asset_count, as_json(metadata), utc_now()),
    )


def maybe_update_images_ready(episode_id: int) -> None:
    scenes = rows("SELECT image_path FROM scenes WHERE episode_id = ?", (episode_id,))
    if scenes and all(scene["image_path"] for scene in scenes):
        execute("UPDATE episodes SET status = ? WHERE id = ?", ("IMAGES_READY", episode_id))


def require_ai_images(episode: dict) -> None:
    project = require_project(episode["project_id"])
    if not project["use_ai_images"]:
        raise HTTPException(400, AI_IMAGES_OFF_MESSAGE)


@app.post("/projects/{project_id}/options")
def update_options(
    project_id: int,
    use_ai_images: str | None = Form(None),
    use_subtitles: str | None = Form(None),
    use_bgm: str | None = Form(None),
    next: str = Form(""),
):
    require_project(project_id)
    execute(
        "UPDATE projects SET use_ai_images = ?, use_subtitles = ?, use_bgm = ? WHERE id = ?",
        (int(use_ai_images is not None), int(use_subtitles is not None), int(use_bgm is not None), project_id),
    )
    target = next if next.startswith("/") and not next.startswith("//") else f"/projects/{project_id}"
    return RedirectResponse(target, status_code=303)


def require_project(project_id: int) -> dict:
    project = one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404)
    return project


def require_episode(episode_id: int) -> dict:
    episode = one("SELECT * FROM episodes WHERE id = ?", (episode_id,))
    if not episode:
        raise HTTPException(404)
    return episode


def require_scene(episode_id: int, scene_order: int) -> dict:
    scene = one("SELECT * FROM scenes WHERE episode_id = ? AND scene_order = ?", (episode_id, scene_order))
    if not scene:
        raise HTTPException(404)
    return scene


