from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import tts_queue
from .chunking import chunk_paragraphs, text_hash
from .config import ASSETS_DIR
from .db import as_json, connect, execute, from_json, one, rows, utc_now
from .epub import EpubError, parse_epub
from .providers import GOOGLE_RECOMMENDED_KO_VOICE, ProviderError, get_tts_provider, is_tts_cached, synthesize_cached
from .ui import esc, page
from .uploads import remove_tree, save_upload

router = APIRouter(prefix="/reader")

BOOKS_DIR = ASSETS_DIR / "books"
MAX_EPUB_BYTES = 100 * 1024 * 1024
READER_PROVIDERS = {"google": "Google Cloud TTS", "local": "로컬 TTS"}
PREFETCH_LIMIT = 8
PREVIEW_TEXT = "안녕하세요. 이 목소리로 책을 읽어 드립니다."

READER_CSS = """
.reader-modes { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:18px; }
.mode-card { display:block; text-decoration:none; color:#1f2328; border:1px solid #b9cbc4; border-radius:10px; padding:28px; background:#fff; }
.mode-card:hover { border-color:#174c4f; box-shadow:0 2px 8px rgba(0,0,0,.08); }
.mode-card strong { display:block; font-size:24px; margin-bottom:8px; }
.mode-card span { color:#647067; }
.book-tile { display:block; text-decoration:none; color:#1f2328; border:1px solid #d8ddd8; border-radius:8px; padding:14px; background:#fbfbf9; }
.controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:14px 0; }
.controls button { min-width:48px; }
.controls button.secondary { background:#e7ecea; color:#174c4f; }
.now-playing { background:#f4f7f5; border-radius:8px; padding:14px; min-height:84px; line-height:1.7; font-size:17px; white-space:pre-wrap; }
.chapter-list { list-style:none; padding:0; margin:10px 0; max-height:460px; overflow:auto; }
.chapter-list li { display:flex; gap:10px; align-items:center; padding:8px 4px; border-bottom:1px solid #eef1ee; }
.chapter-list li.current { background:#e8f1ee; font-weight:800; }
.chapter-list .title { flex:1; min-width:0; overflow-wrap:anywhere; }
.chapter-list button { padding:6px 10px; }
.status-line { color:#647067; font-size:13px; min-height:20px; }
.speed-row { display:flex; gap:10px; align-items:center; }
.speed-row input { flex:1; }
@media (max-width: 760px) { .reader-modes { grid-template-columns:minmax(0, 1fr); } }
"""


class StateIn(BaseModel):
    chapter_id: int | None = None
    chunk_order: int = 0
    offset_sec: float = 0.0
    provider: str = "google"
    voice: str = ""
    speed: float = 1.0
    selected: list[int] = []


def resolve_voice(provider: str, voice: str) -> str:
    if provider not in READER_PROVIDERS:
        raise HTTPException(400, f"지원하지 않는 음성 방식입니다: {provider}")
    voice = (voice or "").strip()[:100]
    if provider == "google":
        return voice or GOOGLE_RECOMMENDED_KO_VOICE
    return voice


def require_book(book_id: int) -> dict:
    book = one("SELECT * FROM books WHERE id = ?", (book_id,))
    if not book:
        raise HTTPException(404)
    return book


def chunk_ready(provider: str, voice: str, text: str) -> bool:
    return is_tts_cached(provider, text, voice, 1.0, "mp3") or is_tts_cached(provider, text, voice, 1.0, "wav")


def synthesize_chunk(book_id: int, text: str, provider: str, voice: str) -> Path:
    path, hit = synthesize_cached(get_tts_provider(provider), provider, text, voice, 1.0, audio_format="mp3")
    execute(
        "INSERT INTO book_tts_log (book_id, provider, voice, chars, cache_hit, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (book_id, provider, voice, len(text), int(hit), utc_now()),
    )
    return path


def store_book_content(book_id: int, parsed) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM book_chapters WHERE book_id = ?", (book_id,))
        for order, chapter in enumerate(parsed.chapters, start=1):
            chunks = chunk_paragraphs(chapter.paragraphs)
            chapter_id = conn.execute(
                "INSERT INTO book_chapters (book_id, chapter_order, title, char_count) VALUES (?, ?, ?, ?)",
                (book_id, order, chapter.title[:200], sum(len(c) for c in chunks)),
            ).lastrowid
            conn.executemany(
                "INSERT INTO book_chunks (book_id, chapter_id, chunk_order, text, text_hash) VALUES (?, ?, ?, ?, ?)",
                [(book_id, chapter_id, i, text, text_hash(text)) for i, text in enumerate(chunks)],
            )


@router.get("", response_class=HTMLResponse)
def reader_home() -> str:
    books = rows("SELECT * FROM books ORDER BY id DESC")
    tiles = "".join(book_tile(book) for book in books) or "<p class='empty-state'>아직 올린 책이 없습니다.</p>"
    return page(
        "책 읽어주기",
        f"""
        <style>{READER_CSS}</style>
        <nav class="top-nav"><a href="/">홈</a><a href="/youtube">YouTube 만들기</a></nav>
        <section class="panel primary-flow">
          <h2>EPUB 올리기</h2>
          <form action="/reader/books" method="post" enctype="multipart/form-data">
            <label>EPUB 파일 <input type="file" name="epub" accept=".epub,application/epub+zip" required></label>
            <p class="notice muted">올리기만 해서는 음성을 만들지 않습니다. 재생하거나 '미리 생성'을 누를 때만 TTS를 호출하고, 한 번 만든 음성은 저장해서 다시 쓰기 때문에 같은 글은 다시 과금되지 않습니다.</p>
            <button>책 추가</button>
          </form>
        </section>
        <section class="panel">
          <h2>내 책</h2>
          <div class="project-list">{tiles}</div>
        </section>
        """,
    )


def book_tile(book: dict) -> str:
    chapters = one("SELECT COUNT(*) AS n FROM book_chapters WHERE book_id = ?", (book["id"],))["n"]
    resume = ""
    if book["progress_chapter_id"]:
        chapter = one("SELECT title FROM book_chapters WHERE id = ?", (book["progress_chapter_id"],))
        if chapter:
            resume = f" · 이어듣기: {esc(chapter['title'])}"
    author = f" · {esc(book['author'])}" if book["author"] else ""
    return f"""
      <a class="book-tile" href="/reader/books/{book['id']}">
        <strong>{esc(book['title'])}</strong>
        <span class="muted">{chapters}개 챕터{author}{resume}</span>
      </a>
    """


@router.post("/books")
def upload_book(epub: UploadFile = File(...)):
    if Path(epub.filename or "").suffix.lower() != ".epub":
        raise HTTPException(400, "EPUB(.epub) 파일만 올릴 수 있습니다.")
    book_id = execute(
        "INSERT INTO books (title, epub_path, created_at) VALUES (?, ?, ?)",
        (Path(epub.filename or "book").stem, "", utc_now()),
    )
    path = BOOKS_DIR / f"book_{book_id}" / "book.epub"
    try:
        save_upload(epub, path, MAX_EPUB_BYTES)
        parsed = parse_epub(path)
    except (EpubError, HTTPException) as exc:
        execute("DELETE FROM books WHERE id = ?", (book_id,))
        remove_tree(path.parent)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        execute("DELETE FROM books WHERE id = ?", (book_id,))
        remove_tree(path.parent)
        raise
    execute("UPDATE books SET title = ?, author = ?, epub_path = ? WHERE id = ?", (parsed.title[:200], parsed.author[:200], str(path), book_id))
    store_book_content(book_id, parsed)
    return RedirectResponse(f"/reader/books/{book_id}", status_code=303)


@router.post("/books/{book_id}/replace")
def replace_book(book_id: int, epub: UploadFile = File(...)):
    book = require_book(book_id)
    if Path(epub.filename or "").suffix.lower() != ".epub":
        raise HTTPException(400, "EPUB(.epub) 파일만 올릴 수 있습니다.")
    path = Path(book["epub_path"])
    staged = path.with_suffix(".new")
    try:
        save_upload(epub, staged, MAX_EPUB_BYTES)
        parsed = parse_epub(staged)
    except EpubError as exc:
        staged.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc
    chapter = one("SELECT chapter_order FROM book_chapters WHERE id = ?", (book["progress_chapter_id"],)) if book["progress_chapter_id"] else None
    order_by_id = {r["id"]: r["chapter_order"] for r in rows("SELECT id, chapter_order FROM book_chapters WHERE book_id = ?", (book_id,))}
    selected_orders = [order_by_id[i] for i in selected_ids(book) if i in order_by_id]
    staged.replace(path)
    execute("UPDATE books SET title = ?, author = ? WHERE id = ?", (parsed.title[:200], parsed.author[:200], book_id))
    store_book_content(book_id, parsed)
    new_chapters = rows("SELECT * FROM book_chapters WHERE book_id = ? ORDER BY chapter_order", (book_id,))
    by_order = {c["chapter_order"]: c["id"] for c in new_chapters}
    progress_chapter = by_order.get(chapter["chapter_order"]) if chapter else None
    execute(
        "UPDATE books SET progress_chapter_id = ?, progress_chunk_order = ?, selected_chapters = ? WHERE id = ?",
        (
            progress_chapter,
            book["progress_chunk_order"] if progress_chapter else 0,
            as_json([by_order[o] for o in selected_orders if o in by_order]),
            book_id,
        ),
    )
    return RedirectResponse(f"/reader/books/{book_id}", status_code=303)


@router.post("/books/{book_id}/delete")
def delete_book(book_id: int):
    book = require_book(book_id)
    execute("DELETE FROM books WHERE id = ?", (book_id,))
    remove_tree(Path(book["epub_path"]).parent)
    return RedirectResponse("/reader", status_code=303)


def selected_ids(book: dict) -> list[int]:
    return [int(x) for x in from_json(book["selected_chapters"], []) if isinstance(x, int)]


@router.get("/voices")
def voices(provider: str = "google"):
    if provider not in READER_PROVIDERS:
        raise HTTPException(400)
    tts = get_tts_provider(provider)
    ok, note = tts.health_check()
    return {"ok": ok, "note": note, "voices": tts.list_voices() if ok else [], "default": GOOGLE_RECOMMENDED_KO_VOICE if provider == "google" else ""}


@router.get("/books/{book_id}", response_class=HTMLResponse)
def book_player(book_id: int) -> str:
    book = require_book(book_id)
    chapters = rows("SELECT * FROM book_chapters WHERE book_id = ? ORDER BY chapter_order", (book_id,))
    provider = book["tts_provider"] if book["tts_provider"] in READER_PROVIDERS else "google"
    voice = resolve_voice(provider, book["tts_voice"])
    tts = get_tts_provider(provider)
    ok, note = tts.health_check()
    voice_list = tts.list_voices() if ok else []
    if voice and voice not in voice_list and ok:
        voice_list = [voice, *voice_list]
    chunk_rows = rows("SELECT chapter_id, text FROM book_chunks WHERE book_id = ?", (book_id,))
    ready: dict[int, int] = {}
    total: dict[int, int] = {}
    for chunk in chunk_rows:
        total[chunk["chapter_id"]] = total.get(chunk["chapter_id"], 0) + 1
        if chunk_ready(provider, voice, chunk["text"]):
            ready[chunk["chapter_id"]] = ready.get(chunk["chapter_id"], 0) + 1
    selected = selected_ids(book) or [c["id"] for c in chapters]
    state = {
        "bookId": book_id,
        "title": book["title"],
        "chapters": [{"id": c["id"], "title": c["title"], "chars": c["char_count"], "chunks": total.get(c["id"], 0), "ready": ready.get(c["id"], 0)} for c in chapters],
        "selected": selected,
        "progress": {
            "chapterId": book["progress_chapter_id"],
            "chunkOrder": book["progress_chunk_order"],
            "offsetSec": book["progress_offset_sec"],
            "updatedAt": book["progress_updated_at"],
        },
        "provider": provider,
        "voice": voice,
        "speed": book["speed"],
    }
    state_json = json.dumps(state, ensure_ascii=False).replace("</", "<\\/")
    stats = tts_stats(book_id)
    provider_options = "".join(f"<option value='{k}'{' selected' if k == provider else ''}>{esc(v)}</option>" for k, v in READER_PROVIDERS.items())
    voice_options = "".join(f"<option value='{esc(v)}'{' selected' if v == voice else ''}>{esc(v)}{' (추천)' if v == GOOGLE_RECOMMENDED_KO_VOICE else ''}</option>" for v in voice_list) or "<option value=''>사용 가능한 목소리 없음</option>"
    total_chunks = sum(total.values())
    ready_chunks = sum(ready.values())
    return page(
        book["title"],
        f"""
        <style>{READER_CSS}</style>
        <nav class="top-nav"><a href="/">홈</a><a href="/reader">내 책</a></nav>
        <section class="panel">
          <h2>지금 읽는 부분</h2>
          <div class="now-playing" id="now-playing">▶ 재생을 누르면 읽기 시작합니다.</div>
          <p class="status-line" id="status"></p>
          <div class="controls">
            <button class="secondary" id="prev-chapter" title="이전 챕터">⏮ 챕터</button>
            <button class="secondary" id="prev-chunk" title="이전 단락">◀ 단락</button>
            <button id="play-toggle">▶ 재생</button>
            <button class="secondary" id="next-chunk" title="다음 단락">단락 ▶</button>
            <button class="secondary" id="next-chapter" title="다음 챕터">챕터 ⏭</button>
          </div>
          <p class="muted" id="resume-hint"></p>
          <label>읽기 속도 <span id="speed-label"></span>
            <span class="speed-row"><input type="range" id="speed" min="0.6" max="2.0" step="0.05" value="{book['speed']}"></span>
          </label>
          <p class="muted">속도는 재생 속도로 조절하므로 바꿔도 음성을 다시 만들지 않습니다(비용 없음).</p>
        </section>
        <section class="panel">
          <h2>음성</h2>
          <label>음성 방식 <select id="provider">{provider_options}</select></label>
          <label>목소리 <select id="voice">{voice_options}</select></label>
          <div class="controls"><button class="secondary" id="preview">미리듣기</button><audio id="preview-audio"></audio></div>
          <p class="muted" id="provider-note">{esc(note)}</p>
        </section>
        <section class="panel">
          <h2>챕터 ({len(chapters)})</h2>
          <div class="controls">
            <button class="secondary" id="select-all">전체 선택</button>
            <button class="secondary" id="select-none">전체 해제</button>
            <button class="secondary" id="pregenerate">선택 챕터 음성 미리 생성</button>
          </div>
          <p class="muted">체크한 챕터만 이어서 재생합니다. 미리 생성은 버튼을 눌렀을 때만 TTS를 호출하며, 이미 만든 조각은 건너뜁니다.</p>
          <p class="status-line" id="pregen-status"></p>
          <ul class="chapter-list" id="chapter-list"></ul>
        </section>
        <section class="panel">
          <h2>음성 준비 현황 · 사용량</h2>
          <p>전체 {total_chunks}조각 중 {ready_chunks}조각 준비됨 (현재 선택한 목소리 기준).</p>
          <p class="muted">신규 합성 {stats['calls']}회 / {stats['new_chars']}자 · 저장된 음성 재사용 {stats['hits']}회</p>
          <details class="inline-details"><summary>EPUB 교체 / 삭제</summary>
            <form action="/reader/books/{book_id}/replace" method="post" enctype="multipart/form-data">
              <label>수정한 EPUB 올리기 <input type="file" name="epub" accept=".epub" required></label>
              <p class="muted">바뀐 문단이 포함된 조각만 새로 합성되고 나머지는 저장된 음성을 재사용합니다. 재생 위치와 챕터 선택은 챕터 번호 기준으로 유지됩니다.</p>
              <button>교체</button>
            </form>
            <form action="/reader/books/{book_id}/delete" method="post" onsubmit="return confirm('이 책을 삭제할까요? 저장된 음성 캐시는 남습니다.')"><button class="secondary">책 삭제</button></form>
          </details>
        </section>
        <script type="application/json" id="reader-state">{state_json}</script>
        <script src="/static/reader.js" defer></script>
        """,
    )


def tts_stats(book_id: int) -> dict:
    row = one(
        """
        SELECT COALESCE(SUM(CASE WHEN cache_hit = 0 THEN 1 ELSE 0 END), 0) AS calls,
               COALESCE(SUM(CASE WHEN cache_hit = 0 THEN chars ELSE 0 END), 0) AS new_chars,
               COALESCE(SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END), 0) AS hits
        FROM book_tts_log WHERE book_id = ?
        """,
        (book_id,),
    )
    return row or {"calls": 0, "new_chars": 0, "hits": 0}


@router.get("/books/{book_id}/chapters/{chapter_id}/chunks")
def chapter_chunks(book_id: int, chapter_id: int, provider: str = "google", voice: str = ""):
    require_book(book_id)
    voice = resolve_voice(provider, voice)
    chunks = rows("SELECT id, chunk_order, text FROM book_chunks WHERE book_id = ? AND chapter_id = ? ORDER BY chunk_order", (book_id, chapter_id))
    if not chunks:
        raise HTTPException(404)
    return [{"id": c["id"], "order": c["chunk_order"], "text": c["text"], "ready": chunk_ready(provider, voice, c["text"])} for c in chunks]


@router.get("/chunks/{chunk_id}/audio")
def chunk_audio(chunk_id: int, provider: str = "google", voice: str = ""):
    chunk = one("SELECT * FROM book_chunks WHERE id = ?", (chunk_id,))
    if not chunk:
        raise HTTPException(404)
    voice = resolve_voice(provider, voice)
    try:
        path = synthesize_chunk(chunk["book_id"], chunk["text"], provider, voice)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"음성 생성 실패: {type(exc).__name__}: {str(exc)[:300]}") from exc
    return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=86400"})


class PrefetchIn(BaseModel):
    chunk_ids: list[int]
    provider: str = "google"
    voice: str = ""


@router.post("/books/{book_id}/prefetch")
def prefetch(book_id: int, body: PrefetchIn):
    require_book(book_id)
    voice = resolve_voice(body.provider, body.voice)
    queued = ready = 0
    for chunk_id in body.chunk_ids[:PREFETCH_LIMIT]:
        chunk = one("SELECT * FROM book_chunks WHERE id = ? AND book_id = ?", (chunk_id, book_id))
        if not chunk:
            continue
        if chunk_ready(body.provider, voice, chunk["text"]):
            ready += 1
            continue
        if tts_queue.enqueue(body.provider, voice, chunk["text"], lambda c=chunk: _background_synthesize(book_id, c["text"], body.provider, voice)):
            queued += 1
    return {"queued": queued, "ready": ready}


def _background_synthesize(book_id: int, text: str, provider: str, voice: str) -> None:
    try:
        synthesize_chunk(book_id, text, provider, voice)
    except Exception:
        pass


@router.get("/books/{book_id}/estimate")
def estimate(book_id: int, chapters: str = "", provider: str = "google", voice: str = ""):
    require_book(book_id)
    voice = resolve_voice(provider, voice)
    ids = [int(x) for x in chapters.split(",") if x.strip().isdigit()]
    if not ids:
        return {"chunks": 0, "to_generate": 0, "chars": 0}
    marks = ",".join("?" * len(ids))
    chunks = rows(f"SELECT text FROM book_chunks WHERE book_id = ? AND chapter_id IN ({marks})", (book_id, *ids))
    missing = [c["text"] for c in chunks if not chunk_ready(provider, voice, c["text"])]
    return {"chunks": len(chunks), "to_generate": len(missing), "chars": sum(len(t) for t in missing)}


class PregenerateIn(BaseModel):
    chapter_ids: list[int]
    provider: str = "google"
    voice: str = ""


@router.post("/books/{book_id}/pregenerate")
def pregenerate(book_id: int, body: PregenerateIn):
    require_book(book_id)
    voice = resolve_voice(body.provider, body.voice)
    if not body.chapter_ids:
        return {"queued": 0, "ready": 0}
    marks = ",".join("?" * len(body.chapter_ids))
    chunks = rows(f"SELECT text FROM book_chunks WHERE book_id = ? AND chapter_id IN ({marks}) ORDER BY chapter_id, chunk_order", (book_id, *body.chapter_ids))
    queued = ready = 0
    for chunk in chunks:
        if chunk_ready(body.provider, voice, chunk["text"]):
            ready += 1
        elif tts_queue.enqueue(body.provider, voice, chunk["text"], lambda t=chunk["text"]: _background_synthesize(book_id, t, body.provider, voice)):
            queued += 1
    return {"queued": queued, "ready": ready}


@router.post("/books/{book_id}/state")
def save_state(book_id: int, state: StateIn):
    require_book(book_id)
    voice = resolve_voice(state.provider, state.voice)
    chapter_ids = {c["id"] for c in rows("SELECT id FROM book_chapters WHERE book_id = ?", (book_id,))}
    chapter_id = state.chapter_id if state.chapter_id in chapter_ids else None
    selected = [c for c in state.selected if c in chapter_ids]
    execute(
        """
        UPDATE books SET progress_chapter_id = ?, progress_chunk_order = ?, progress_offset_sec = ?, progress_updated_at = ?,
                         tts_provider = ?, tts_voice = ?, speed = ?, selected_chapters = ?
        WHERE id = ?
        """,
        (
            chapter_id,
            max(0, state.chunk_order),
            max(0.0, state.offset_sec),
            utc_now(),
            state.provider,
            voice,
            min(3.0, max(0.5, state.speed)),
            as_json(selected),
            book_id,
        ),
    )
    return JSONResponse({"saved": True})


@router.post("/preview")
def preview(provider: str = Form("google"), voice: str = Form("")):
    voice = resolve_voice(provider, voice)
    try:
        path, _ = synthesize_cached(get_tts_provider(provider), provider, PREVIEW_TEXT, voice, 1.0, audio_format="mp3")
    except Exception as exc:
        raise HTTPException(502, f"미리듣기 실패: {str(exc)[:300]}") from exc
    return FileResponse(path, media_type="audio/mpeg")
