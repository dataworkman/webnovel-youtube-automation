(() => {
  const state = JSON.parse(document.getElementById("reader-state").textContent);
  const $ = (id) => document.getElementById(id);
  const enc = encodeURIComponent;
  const chapters = state.chapters;
  const order = chapters.map((c) => c.id);
  const byId = new Map(chapters.map((c) => [c.id, c]));
  const selected = new Set(state.selected);
  let provider = state.provider;
  let voice = state.voice;
  let speed = state.speed || 1;

  const players = [new Audio(), new Audio()];
  players.forEach((p) => { p.preload = "auto"; p.preservesPitch = true; });
  let active = 0;
  let pos = null;
  let playing = false;
  let loadToken = 0;
  let preparedPos = null;
  let preparing = false;
  let retries = 0;
  let saveTimer = null;
  const chunkCache = new Map();

  const setStatus = (text) => { $("status").textContent = text || ""; };
  const playlist = () => order.filter((id) => selected.has(id));
  const audioUrl = (chunk) => `/reader/chunks/${chunk.id}/audio?provider=${enc(provider)}&voice=${enc(voice)}`;

  async function getChunks(chapterId) {
    if (!chunkCache.has(chapterId)) {
      const res = await fetch(`/reader/books/${state.bookId}/chapters/${chapterId}/chunks?provider=${enc(provider)}&voice=${enc(voice)}`);
      if (!res.ok) throw new Error(`단락을 불러오지 못했습니다 (${res.status})`);
      chunkCache.set(chapterId, await res.json());
    }
    return chunkCache.get(chapterId);
  }

  async function nextPos(p) {
    const chunks = await getChunks(p.chapterId);
    if (p.chunkIdx + 1 < chunks.length) return { chapterId: p.chapterId, chunkIdx: p.chunkIdx + 1 };
    const here = order.indexOf(p.chapterId);
    const nextId = playlist().find((id) => order.indexOf(id) > here);
    return nextId === undefined ? null : { chapterId: nextId, chunkIdx: 0 };
  }

  async function prevPos(p) {
    if (p.chunkIdx > 0) return { chapterId: p.chapterId, chunkIdx: p.chunkIdx - 1 };
    const here = order.indexOf(p.chapterId);
    const prevId = playlist().filter((id) => order.indexOf(id) < here).pop();
    if (prevId === undefined) return null;
    return { chapterId: prevId, chunkIdx: (await getChunks(prevId)).length - 1 };
  }

  function currentChunkSync() {
    if (!pos) return null;
    const chunks = chunkCache.get(pos.chapterId);
    return chunks ? chunks[pos.chunkIdx] : null;
  }

  function updateUI() {
    const chunk = currentChunkSync();
    $("play-toggle").textContent = playing ? "⏸ 일시정지" : "▶ 재생";
    if (chunk && pos) {
      const chapter = byId.get(pos.chapterId);
      const total = chunkCache.get(pos.chapterId).length;
      $("now-playing").textContent = chunk.text;
      setStatus(`${chapter.title} · 단락 ${pos.chunkIdx + 1}/${total}`);
      if ("mediaSession" in navigator) {
        navigator.mediaSession.metadata = new MediaMetadata({ title: chapter.title, artist: state.title });
      }
    }
    document.querySelectorAll("#chapter-list li").forEach((li) => {
      li.classList.toggle("current", pos && Number(li.dataset.id) === pos.chapterId);
    });
  }

  function saveProgress(beacon = false) {
    const fallback = state.progress;
    const body = JSON.stringify({
      chapter_id: pos ? pos.chapterId : fallback.chapterId,
      chunk_order: pos ? pos.chunkIdx : fallback.chunkOrder || 0,
      offset_sec: pos ? players[active].currentTime || 0 : fallback.offsetSec || 0,
      provider,
      voice,
      speed,
      selected: [...selected],
    });
    const url = `/reader/books/${state.bookId}/state`;
    if (beacon && navigator.sendBeacon) {
      navigator.sendBeacon(url, new Blob([body], { type: "application/json" }));
      return;
    }
    fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true }).catch(() => {});
  }

  function startSaving() {
    if (!saveTimer) saveTimer = setInterval(() => playing && saveProgress(), 5000);
  }

  function attach(player, chunk, offset) {
    player.src = audioUrl(chunk);
    player.defaultPlaybackRate = speed;
    player.playbackRate = speed;
    if (offset > 0) {
      player.addEventListener("loadedmetadata", () => { player.currentTime = offset; }, { once: true });
    }
  }

  async function startAt(p, offset = 0) {
    const token = ++loadToken;
    setStatus("불러오는 중…");
    let chunks;
    try {
      chunks = await getChunks(p.chapterId);
    } catch (error) {
      setStatus(String(error.message || error));
      return;
    }
    if (token !== loadToken) return;
    const chunk = chunks[Math.min(p.chunkIdx, chunks.length - 1)];
    pos = { chapterId: p.chapterId, chunkIdx: chunk.order };
    const current = players[active];
    const other = players[1 - active];
    other.pause();
    other.removeAttribute("src");
    preparedPos = null;
    preparing = false;
    attach(current, chunk, offset);
    playing = true;
    updateUI();
    saveProgress();
    startSaving();
    try {
      await current.play();
    } catch (error) {
      handlePlayError(error);
    }
  }

  function handlePlayError(error) {
    if (error && error.name === "AbortError") return;
    playing = false;
    updateUI();
    setStatus(error && error.name === "NotAllowedError" ? "재생 버튼을 눌러 주세요." : `재생 오류: ${error && error.message}`);
  }

  async function prepareNext() {
    if (!pos || preparing || preparedPos) return;
    preparing = true;
    const token = loadToken;
    try {
      const np = await nextPos(pos);
      if (token !== loadToken || !np) return;
      const chunk = (await getChunks(np.chapterId))[np.chunkIdx];
      const other = players[1 - active];
      attach(other, chunk, 0);
      other.load();
      preparedPos = np;
      prefetchAhead(np);
    } catch (error) {
      /* the next chunk is retried when playback reaches it */
    } finally {
      preparing = false;
    }
  }

  async function prefetchAhead(from) {
    const ids = [];
    let p = from;
    for (let i = 0; i < 2; i += 1) {
      p = await nextPos(p);
      if (!p) break;
      ids.push((await getChunks(p.chapterId))[p.chunkIdx].id);
    }
    if (!ids.length) return;
    fetch(`/reader/books/${state.bookId}/prefetch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chunk_ids: ids, provider, voice }),
    }).catch(() => {});
  }

  async function advance() {
    if (!preparedPos) {
      const np = pos && (await nextPos(pos));
      if (!np) return finish();
      return startAt(np, 0);
    }
    const next = players[1 - active];
    const np = preparedPos;
    preparedPos = null;
    players[active].pause();
    active = 1 - active;
    pos = np;
    loadToken += 1;
    playing = true;
    updateUI();
    saveProgress();
    try {
      await next.play();
    } catch (error) {
      handlePlayError(error);
    }
    prepareNext();
  }

  function finish() {
    playing = false;
    updateUI();
    setStatus("선택한 챕터를 모두 읽었습니다.");
    saveProgress();
  }

  players.forEach((player) => {
    player.addEventListener("ended", () => { if (player === players[active]) advance(); });
    player.addEventListener("playing", () => {
      if (player !== players[active]) return;
      retries = 0;
      prepareNext();
    });
    player.addEventListener("waiting", () => { if (player === players[active] && playing) setStatus("음성을 준비하는 중…"); });
    player.addEventListener("error", () => {
      if (player !== players[active]) { preparedPos = null; return; }
      if (!playing || !player.getAttribute("src")) return;
      retries += 1;
      if (retries > 3) {
        playing = false;
        updateUI();
        setStatus("음성 생성/재생에 실패했습니다. 설정(API 키)을 확인한 뒤 다시 재생해 주세요.");
        return;
      }
      setStatus(`재시도 중… (${retries}/3)`);
      const offset = player.currentTime || 0;
      setTimeout(() => pos && startAt(pos, offset), 2000 * retries);
    });
  });

  async function resume() {
    const saved = state.progress;
    if (saved.chapterId && byId.has(saved.chapterId)) {
      return startAt({ chapterId: saved.chapterId, chunkIdx: saved.chunkOrder || 0 }, saved.offsetSec || 0);
    }
    const first = playlist()[0];
    if (first === undefined) return setStatus("재생할 챕터를 선택하세요.");
    return startAt({ chapterId: first, chunkIdx: 0 }, 0);
  }

  async function toggle() {
    const current = players[active];
    if (playing) {
      current.pause();
      playing = false;
      updateUI();
      saveProgress();
      return;
    }
    if (pos && current.getAttribute("src")) {
      playing = true;
      updateUI();
      try { await current.play(); } catch (error) { handlePlayError(error); }
      return;
    }
    await resume();
  }

  async function chunkStep(direction) {
    if (!pos) return resume();
    if (direction < 0 && (players[active].currentTime || 0) > 3) return startAt(pos, 0);
    const target = direction > 0 ? await nextPos(pos) : await prevPos(pos);
    if (!target) return;
    if (direction > 0 && preparedPos) return advance();
    return startAt(target, 0);
  }

  async function chapterStep(direction) {
    const list = playlist();
    const here = pos ? order.indexOf(pos.chapterId) : -1;
    const candidates = direction > 0 ? list.filter((id) => order.indexOf(id) > here) : list.filter((id) => order.indexOf(id) < here);
    const target = direction > 0 ? candidates[0] : candidates.pop();
    if (target === undefined) return;
    return startAt({ chapterId: target, chunkIdx: 0 }, 0);
  }

  function renderChapters() {
    const list = $("chapter-list");
    list.innerHTML = "";
    chapters.forEach((chapter, index) => {
      const li = document.createElement("li");
      li.dataset.id = chapter.id;
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = selected.has(chapter.id);
      box.addEventListener("change", () => {
        if (box.checked) selected.add(chapter.id); else selected.delete(chapter.id);
        saveProgress();
        if (preparedPos) { preparedPos = null; prepareNext(); }
      });
      const title = document.createElement("span");
      title.className = "title";
      title.textContent = `${index + 1}. ${chapter.title}`;
      const meta = document.createElement("small");
      meta.textContent = `${chapter.chars.toLocaleString()}자 · 준비 ${chapter.ready}/${chapter.chunks}`;
      const play = document.createElement("button");
      play.className = "secondary";
      play.textContent = "▶";
      play.title = "이 챕터부터 재생";
      play.addEventListener("click", () => {
        selected.add(chapter.id);
        box.checked = true;
        startAt({ chapterId: chapter.id, chunkIdx: 0 }, 0);
      });
      li.append(box, title, meta, play);
      list.append(li);
    });
  }

  async function loadVoices(nextProvider) {
    const res = await fetch(`/reader/voices?provider=${enc(nextProvider)}`);
    const data = await res.json();
    $("provider-note").textContent = data.note;
    const select = $("voice");
    select.innerHTML = "";
    (data.voices.length ? data.voices : [""]).forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name || "사용 가능한 목소리 없음";
      if (name === data.default) option.textContent += " (추천)";
      select.append(option);
    });
    select.value = data.voices.includes(data.default) ? data.default : data.voices[0] || "";
    voice = select.value;
  }

  function voiceChanged() {
    saveProgress();
    if (preparedPos) { preparedPos = null; prepareNext(); }
  }

  $("provider").addEventListener("change", async (event) => {
    provider = event.target.value;
    await loadVoices(provider);
    voiceChanged();
  });
  $("voice").addEventListener("change", (event) => { voice = event.target.value; voiceChanged(); });
  $("speed").addEventListener("input", (event) => {
    speed = Number(event.target.value);
    $("speed-label").textContent = `${speed.toFixed(2)}x`;
    players.forEach((p) => { p.defaultPlaybackRate = speed; p.playbackRate = speed; });
    clearTimeout(startSaving.debounce);
    startSaving.debounce = setTimeout(() => saveProgress(), 600);
  });
  $("preview").addEventListener("click", async () => {
    const button = $("preview");
    button.disabled = true;
    try {
      const form = new FormData();
      form.append("provider", provider);
      form.append("voice", voice);
      const res = await fetch("/reader/preview", { method: "POST", body: form });
      if (!res.ok) throw new Error((await res.json()).detail || res.status);
      const audio = $("preview-audio");
      audio.src = URL.createObjectURL(await res.blob());
      await audio.play();
    } catch (error) {
      setStatus(`미리듣기 실패: ${error.message || error}`);
    } finally {
      button.disabled = false;
    }
  });

  $("play-toggle").addEventListener("click", toggle);
  $("prev-chunk").addEventListener("click", () => chunkStep(-1));
  $("next-chunk").addEventListener("click", () => chunkStep(1));
  $("prev-chapter").addEventListener("click", () => chapterStep(-1));
  $("next-chapter").addEventListener("click", () => chapterStep(1));
  $("select-all").addEventListener("click", () => { order.forEach((id) => selected.add(id)); renderChapters(); saveProgress(); });
  $("select-none").addEventListener("click", () => { selected.clear(); renderChapters(); saveProgress(); });

  $("pregenerate").addEventListener("click", async () => {
    const ids = playlist();
    const status = $("pregen-status");
    if (!ids.length) { status.textContent = "챕터를 선택하세요."; return; }
    const query = `chapters=${ids.join(",")}&provider=${enc(provider)}&voice=${enc(voice)}`;
    const estimate = await (await fetch(`/reader/books/${state.bookId}/estimate?${query}`)).json();
    if (!estimate.to_generate) { status.textContent = "선택한 챕터의 음성이 이미 모두 준비되어 있습니다."; return; }
    if (!confirm(`${estimate.to_generate}개 조각(약 ${estimate.chars.toLocaleString()}자)을 새로 합성합니다. 계속할까요?`)) return;
    await fetch(`/reader/books/${state.bookId}/pregenerate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chapter_ids: ids, provider, voice }),
    });
    const total = estimate.to_generate;
    for (let i = 0; i < 2000; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 3000));
      const now = await (await fetch(`/reader/books/${state.bookId}/estimate?${query}`)).json();
      status.textContent = `음성 생성 중… ${total - now.to_generate}/${total}`;
      if (!now.to_generate) { status.textContent = `완료: ${total}개 조각 생성`; return; }
    }
  });

  if ("mediaSession" in navigator) {
    navigator.mediaSession.setActionHandler("play", toggle);
    navigator.mediaSession.setActionHandler("pause", toggle);
    navigator.mediaSession.setActionHandler("previoustrack", () => chapterStep(-1));
    navigator.mediaSession.setActionHandler("nexttrack", () => chapterStep(1));
  }
  document.addEventListener("visibilitychange", () => { if (document.hidden) saveProgress(true); });
  window.addEventListener("pagehide", () => saveProgress(true));

  $("speed-label").textContent = `${speed.toFixed(2)}x`;
  renderChapters();
  const saved = state.progress;
  if (saved.chapterId && byId.has(saved.chapterId)) {
    const minutes = Math.floor((saved.offsetSec || 0) / 60);
    const seconds = Math.floor((saved.offsetSec || 0) % 60);
    $("resume-hint").textContent = `이어듣기 위치: ${byId.get(saved.chapterId).title} · 단락 ${(saved.chunkOrder || 0) + 1} (${minutes}:${String(seconds).padStart(2, "0")}) — ▶ 재생을 누르면 여기서 이어집니다.`;
    $("play-toggle").textContent = "▶ 이어듣기";
  }
})();
