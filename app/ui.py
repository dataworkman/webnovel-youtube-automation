from __future__ import annotations

from pathlib import Path

from .config import ASSETS_DIR
from .db import from_json, rows
from .jobs import latest_job
from .providers import GOOGLE_RECOMMENDED_KO_VOICE
from .scenes import scene_reference_paths


def project_tile(project: dict) -> str:
    episodes = rows("SELECT COUNT(*) AS count FROM episodes WHERE project_id = ?", (project["id"],))[0]["count"]
    characters = rows("SELECT COUNT(*) AS count FROM characters WHERE project_id = ?", (project["id"],))[0]["count"]
    return f"""
      <a class="project-tile" href="/projects/{project['id']}">
        <strong>{esc(project['title'])}</strong>
        <span>{episodes}회차 · 등장인물 {characters}명</span>
      </a>
    """


def episode_item(episode: dict) -> str:
    return f"""
      <a class="episode-tile" href="/episodes/{episode['id']}">
        <strong>{esc(episode['title'])}</strong>
        <span>{esc(episode['status'])}</span>
      </a>
    """


def google_voice_option(voice: str, selected_voice: str | None = None) -> str:
    selected_name = GOOGLE_RECOMMENDED_KO_VOICE if selected_voice is None else selected_voice
    selected = " selected" if voice == selected_name else ""
    label = f"{voice} (추천)" if voice == GOOGLE_RECOMMENDED_KO_VOICE else voice
    return f"<option value='{esc(voice)}'{selected}>{esc(label)}</option>"


def voice_preview_panel(project_id: int, google_ok: bool, google_note: str, google_voice_options: str) -> str:
    return f"""
      <section class="panel voice-preview-panel">
        <h2>목소리 미리듣기</h2>
        {voice_preview_form(project_id, google_ok, google_voice_options)}
        <p class="muted">상태: {esc(google_note)}</p>
      </section>
    """


def voice_preview_form(project_id: int, google_ok: bool, google_voice_options: str) -> str:
    return f"""
      <form class="voice-preview-form" action="/projects/{project_id}/voice-preview" method="post" data-voice-preview>
        <input type="hidden" name="provider" value="google">
        <label>미리듣기 문장
          <input name="text" value="안녕하세요. 이 목소리로 웹소설 내레이션을 시작합니다.">
        </label>
        <label>목소리 선택
          <select name="voice">{google_voice_options or '<option value="">사용 가능한 Google 목소리 없음</option>'}</select>
        </label>
        <label>말하기 속도
          <input type="number" name="speaking_rate" value="1.0" step="0.05" min="0.25" max="4">
        </label>
        <button {'disabled' if not google_ok else ''}>선택한 목소리 들어보기</button>
        <audio controls preload="none"></audio>
        <p class="preview-status muted" aria-live="polite"></p>
      </form>
    """


def character_item(character: dict, google_voices: list[str], ai_images: bool = True) -> str:
    refs = from_json(character["reference_image_paths"], [])
    locked = character.get("locked_reference_path") or ""
    ref_text = f"{len(refs)} reference image(s)" if refs else "No reference images"
    locked_text = "Identity locked" if locked and Path(locked).exists() else "No locked identity reference"
    ref_items = "".join(reference_item(character["id"], ref, locked) for ref in refs) or "<li>No references yet</li>"
    voice_options = "".join(google_voice_option(voice, character.get("voice_name") or "") for voice in google_voices)
    gender = character.get("voice_gender") or ""
    openai_reference_form = (
        f"""<form action="/characters/{character['id']}/openai-reference" method="post">
          <button>Generate Locked Character Reference With OpenAI</button>
        </form>"""
        if ai_images
        else '<p class="muted">AI 이미지 생성이 꺼져 있어 OpenAI 레퍼런스 생성은 숨겼습니다.</p>'
    )
    return f"""
      <li>
        <strong>{esc(character['name'])}</strong>: {esc(character['description_json'])}
        <small>{esc(ref_text)} — {esc(locked_text)} — 목소리 {esc(character.get('voice_name') or '미지정')}</small>
        <form action="/characters/{character['id']}/voice" method="post">
          <label>목소리 성별/톤
            <select name="voice_gender">
              <option value="" {'selected' if gender == '' else ''}>미지정</option>
              <option value="female" {'selected' if gender == 'female' else ''}>여성</option>
              <option value="male" {'selected' if gender == 'male' else ''}>남성</option>
              <option value="young" {'selected' if gender == 'young' else ''}>어린/젊은 톤</option>
              <option value="mature" {'selected' if gender == 'mature' else ''}>성숙한 톤</option>
            </select>
          </label>
          <label>인물 Google 목소리
            <select name="voice_name">
              <option value="" {'selected' if not character.get('voice_name') else ''}>내레이터 목소리 사용</option>
              {voice_options or '<option value="">사용 가능한 Google 목소리 없음</option>'}
            </select>
          </label>
          <button>인물 목소리 저장</button>
        </form>
        <form action="/characters/{character['id']}/identity" method="post">
          <label>Identity notes <textarea name="description_json">{esc(character['description_json'])}</textarea></label>
          <label>Identity lock <textarea name="identity_lock">{esc(character.get('identity_lock') or '')}</textarea></label>
          <button>Save Identity Lock</button>
        </form>
        <ul>{ref_items}</ul>
        <form action="/characters/{character['id']}/references" method="post" enctype="multipart/form-data">
          <input type="file" name="image" accept="image/*" required>
          <label><input type="checkbox" name="lock_identity" checked> Lock as identity reference</label>
          <button>Add Reference</button>
        </form>
        {openai_reference_form}
      </li>
    """


def reference_item(character_id: int, reference_path: str, locked_path: str) -> str:
    status = "locked" if reference_path == locked_path else "reference"
    image = asset_img(reference_path)
    return f"""
      <li>
        {image}
        <small>{esc(status)}</small>
        <form action="/characters/{character_id}/lock-reference" method="post">
          <input type="hidden" name="reference_path" value="{esc(reference_path)}">
          <button>Lock This Face</button>
        </form>
      </li>
    """


def asset_img(path: str | None) -> str:
    if not path:
        return ""
    path_obj = Path(path)
    try:
        relative = path_obj.resolve().relative_to(ASSETS_DIR.resolve())
        src = f"/assets/{relative.as_posix()}"
    except ValueError:
        src = f"/assets/{path_obj.name}"
    return f"<img src='{esc(src)}' alt='Reference image'>"


def provider_event_item(event: dict) -> str:
    scene = f" scene {event['scene_order']}" if event["scene_order"] else ""
    return (
        f"<li>{esc(event['provider'])} {esc(event['action'])}{scene}: "
        f"{event['call_count']} call(s), {event['asset_count']} asset(s)</li>"
    )


def scene_card(scene: dict, episode_id: int, characters: list[dict], ai_images: bool = True) -> str:
    img = asset_img(scene["image_path"]) if scene.get("image_path") else "<div class='empty'>No image</div>"
    selected_names = set(from_json(scene.get("characters_present"), []))
    character_controls = "".join(scene_character_checkbox(character, selected_names) for character in characters) or "<p>No project characters registered.</p>"
    reference_count = len(scene_reference_paths(scene))
    reference_note = f"{reference_count} locked/reference image(s) will be sent to OpenAI." if reference_count else "No identity reference will be sent unless you assign characters with locked references."
    if not ai_images:
        reference_note = "AI 이미지 생성이 꺼져 있어 레퍼런스를 외부로 보내지 않습니다."
    regenerate_form = (
        f"""<form action="/episodes/{episode_id}/scenes/{scene['scene_order']}/openai-image" method="post">
          <button>Regenerate This Scene With OpenAI</button>
        </form>"""
        if ai_images
        else ""
    )
    return f"""
      <article class="scene">
        {img}
        <h3>Scene {scene['scene_order']}</h3>
        <p>{esc(scene['summary'])}</p>
        <form action="/episodes/{episode_id}/scenes/{scene['scene_order']}/characters" method="post">
          <fieldset>
            <legend>Characters in this scene</legend>
            {character_controls}
          </fieldset>
          <p class="muted">{esc(reference_note)}</p>
          <button>Save Scene Characters</button>
        </form>
        <details><summary>Prompt</summary><pre>{esc(scene['prompt'])}</pre></details>
        <form action="/episodes/{episode_id}/scenes/{scene['scene_order']}/image" method="post" enctype="multipart/form-data">
          <input type="file" name="image" accept="image/*" required>
          <button>Replace This Image</button>
        </form>
        {regenerate_form}
      </article>
    """


def scene_character_checkbox(character: dict, selected_names: set[str]) -> str:
    checked = "checked" if character["name"] in selected_names else ""
    locked = character.get("locked_reference_path")
    status = "locked" if locked and Path(locked).exists() else "no locked reference"
    return f"""
      <label>
        <input type="checkbox" name="character_names" value="{esc(character['name'])}" {checked}>
        {esc(character['name'])} <small>{esc(status)}</small>
      </label>
    """


def render_item(render_row: dict) -> str:
    if render_row["status"] == "COMPLETE":
        return f"<li>Complete: <a href='/renders/{render_row['id']}/download'>Download MP4</a> <video controls src='/renders/{Path(render_row['output_path']).name}'></video></li>"
    error = f"<pre>{esc(render_row['error_log'])}</pre>" if render_row["error_log"] else ""
    return f"<li>{esc(render_row['status'])}{error}</li>"


def option_panel(project: dict, next_url: str) -> str:
    def box(name: str, label: str, hint: str) -> str:
        checked = " checked" if project[name] else ""
        return f'<label><input type="checkbox" name="{name}" value="1"{checked}> {label} <small>{hint}</small></label>'

    return f"""
      <section class="panel">
        <h2>영상 옵션 <small>(이 소설 전체에 적용)</small></h2>
        <form action="/projects/{project['id']}/options" method="post">
          <input type="hidden" name="next" value="{esc(next_url)}">
          {box('use_ai_images', 'AI 이미지 생성', '꺼두면 이미지 API를 아예 호출하지 않습니다 (직접 업로드/로컬 기본 이미지 사용)')}
          {box('use_subtitles', '자막', '끄면 영상에 자막을 입히지 않습니다')}
          {box('use_bgm', 'BGM', '끄면 배경음악 없이 내레이션만 사용합니다')}
          <p class="muted">TTS 내레이션은 항상 사용합니다.</p>
          <button>옵션 저장</button>
        </form>
      </section>
    """


def job_panel(episode_id: int) -> str:
    job = latest_job(episode_id)
    if not job:
        return f'<section class="panel job-panel" data-job-url="/episodes/{episode_id}/job" hidden></section>'
    active = job["status"] in ("QUEUED", "RUNNING")
    if active:
        total = job["progress_total"] or 1
        body = (
            f'<h2>{esc(job["label"])} 진행 중</h2>'
            f'<progress max="{total}" value="{job["progress_current"]}"></progress>'
            f'<p class="muted">{esc(job["message"] or "대기 중")}</p>'
        )
    elif job["status"] == "FAILED":
        body = f'<h2>{esc(job["label"])} 실패</h2><pre class="job-error">{esc(job["error"] or "")}</pre>'
    else:
        return f'<section class="panel job-panel" data-job-url="/episodes/{episode_id}/job" hidden></section>'
    return f'<section class="panel job-panel" data-job-url="/episodes/{episode_id}/job" data-active="{int(active)}">{body}</section>'


def page(title: str, body: str) -> str:
    return f"""
    <!doctype html>
    <html lang="ko">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{esc(title)}</title>
      <style>{CSS}</style>
    </head>
    <body>
      <main>
        <header><h1>{esc(title)}</h1></header>
        {body}
      </main>
      <script>
      document.querySelectorAll(".job-panel[data-active='1']").forEach((panel) => {{
        const poll = async () => {{
          try {{
            const job = await (await fetch(panel.dataset.jobUrl)).json();
            if (job.status === "QUEUED" || job.status === "RUNNING") {{
              const bar = panel.querySelector("progress");
              const text = panel.querySelector("p");
              if (bar) {{ bar.max = job.progress_total || 1; bar.value = job.progress_current; }}
              if (text) text.textContent = job.message || "대기 중";
              return setTimeout(poll, 1500);
            }}
          }} catch (error) {{
            return setTimeout(poll, 3000);
          }}
          location.reload();
        }};
        setTimeout(poll, 1500);
      }});
      document.querySelectorAll("[data-voice-preview]").forEach((form) => {{
        const audio = form.querySelector("audio");
        const status = form.querySelector(".preview-status");
        const button = form.querySelector("button");
        let previousUrl = "";
        form.addEventListener("submit", async (event) => {{
          event.preventDefault();
          if (!audio || !button) return;
          const originalText = button.textContent;
          button.disabled = true;
          button.textContent = "생성 중...";
          if (status) status.textContent = "목소리를 만들고 있습니다.";
          try {{
            const response = await fetch(form.action, {{
              method: "POST",
              body: new FormData(form),
            }});
            if (!response.ok) {{
              throw new Error(await response.text());
            }}
            const blob = await response.blob();
            if (previousUrl) URL.revokeObjectURL(previousUrl);
            previousUrl = URL.createObjectURL(blob);
            audio.src = previousUrl;
            await audio.play();
            if (status) status.textContent = "재생 중입니다.";
          }} catch (error) {{
            if (status) status.textContent = "미리듣기 실패: " + String(error).slice(0, 160);
          }} finally {{
            button.disabled = false;
            button.textContent = originalText;
          }}
        }});
      }});
      </script>
    </body>
    </html>
    """


def esc(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


CSS = """
* { box-sizing:border-box; }
:root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#f7f7f4; color:#1f2328; }
html, body { margin:0; max-width:100%; overflow-x:hidden; }
main { width:100%; max-width:1180px; margin:0 auto; padding:28px; }
header { display:flex; align-items:end; justify-content:space-between; gap:16px; margin-bottom:20px; min-width:0; }
h1 { font-size:32px; margin:0; letter-spacing:0; overflow-wrap:anywhere; }
h2 { margin:0 0 14px; font-size:18px; }
a { color:#0f5c5c; overflow-wrap:anywhere; }
.top-nav { display:flex; flex-wrap:wrap; gap:10px; align-items:center; justify-content:flex-end; margin-bottom:12px; }
.top-nav a { display:inline-flex; align-items:center; min-height:36px; border:1px solid #c7d0cc; border-radius:6px; padding:8px 10px; background:#fff; text-decoration:none; font-weight:800; }
.intro { display:flex; justify-content:space-between; align-items:end; border-bottom:1px solid #d8ddd8; padding:4px 0 20px; margin-bottom:16px; }
.intro h2 { font-size:24px; line-height:1.25; margin:4px 0 0; overflow-wrap:anywhere; }
.eyebrow { margin:0; color:#647067; font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:.08em; }
.job-panel progress { width:100%; height:18px; }
.job-error { white-space:pre-wrap; overflow-wrap:anywhere; color:#9b1c1c; font-size:13px; margin:0; }
.panel { min-width:0; max-width:100%; background:#fff; border:1px solid #d8ddd8; border-radius:8px; padding:18px; margin:14px 0; box-shadow:0 1px 2px rgba(0,0,0,.04); }
.grid { display:grid; grid-template-columns:minmax(0, 1fr) minmax(0, 1fr); gap:16px; }
.choice-grid { display:grid; grid-template-columns:minmax(0, 1fr) minmax(0, 1fr); gap:18px; align-items:start; }
.choice { min-height:260px; }
.primary-flow { border-color:#b9cbc4; }
label { display:block; max-width:100%; font-size:13px; font-weight:700; margin:12px 0; overflow-wrap:anywhere; }
input, textarea, select { width:100%; min-width:0; max-width:100%; border:1px solid #c7d0cc; border-radius:6px; padding:10px; font:inherit; background:#fff; }
input[type="checkbox"], input[type="radio"] { width:auto; min-width:auto; max-width:none; padding:0; }
input[type="file"] { padding:8px; }
textarea { min-height:120px; resize:vertical; }
button { max-width:100%; border:0; border-radius:6px; padding:10px 14px; background:#174c4f; color:white; font-weight:800; cursor:pointer; white-space:normal; overflow-wrap:anywhere; }
button:hover { background:#0f383a; }
ul { padding-left:20px; }
code, pre { max-width:100%; background:#eef1ee; border-radius:6px; padding:2px 5px; white-space:pre-wrap; overflow-wrap:anywhere; }
.project-list, .episode-list { display:grid; gap:10px; }
.project-tile, .episode-tile { display:block; min-width:0; max-width:100%; text-decoration:none; border:1px solid #d8ddd8; border-radius:8px; padding:14px; background:#fbfbf9; color:#1f2328; }
.project-tile strong, .episode-tile strong { display:block; margin-bottom:6px; }
.project-tile span, .episode-tile span, small, .muted { color:#647067; }
.empty-state { color:#647067; }
.system-status, .generation-log { max-width:100%; margin:14px 0; border:1px solid #d8ddd8; border-radius:8px; padding:12px 14px; background:#fbfbf9; }
.system-status summary, .generation-log summary { cursor:pointer; font-weight:800; color:#43504a; }
.inline-details { margin:12px 0; }
.inline-details summary { cursor:pointer; font-weight:800; color:#43504a; }
.toolbar { display:grid; gap:12px; margin-top:16px; }
fieldset { min-width:0; border:1px solid #d8ddd8; border-radius:8px; padding:10px; margin:12px 0; }
legend { font-size:13px; font-weight:800; color:#43504a; }
.scenes { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:16px; }
.scene { min-width:0; max-width:100%; border:1px solid #d8ddd8; border-radius:8px; padding:12px; background:#fff; }
.scene img, .scene .empty { width:100%; max-width:100%; aspect-ratio:16/9; object-fit:cover; border-radius:6px; background:#eef1ee; display:grid; place-items:center; color:#647067; }
.scene h3 { margin:12px 0 8px; overflow-wrap:anywhere; }
.scene p, .scene summary, .scene label, .scene small { overflow-wrap:anywhere; }
video { width:100%; max-width:520px; display:block; margin-top:10px; border-radius:6px; }
@media (max-width: 760px) {
  main { padding:16px; }
  header, .intro { display:block; }
  .grid, .choice-grid, .scenes { grid-template-columns:minmax(0, 1fr); }
  .choice { min-height:0; }
}
@media (max-width: 420px) {
  main { padding:12px; }
  h1 { font-size:26px; }
  .intro h2 { font-size:20px; }
  .panel, .scene { padding:14px; }
}
"""
