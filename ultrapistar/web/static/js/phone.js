/* Phone remote: join with a name, search catalog, confirm + queue, watch state. */

let myName = localStorage.getItem("ups_name") || "";
let myPerformerId = null;
let pendingEntry = null;
let isHost = false;
let paused = false;

const joinCard = $("#join"), remote = $("#remote");

/* host unlock: any player with the host key gets console controls */
function setHost(on) {
  isHost = on;
  $("#btn-host").classList.toggle("hidden", on);
  $("#host-badge").classList.toggle("hidden", !on);
  $("#host-controls").classList.toggle("hidden", !on);
  $("#tab-btn-host").classList.toggle("hidden", !on);
  if (on) {
    $("#tab-btn-host").click();   // jump straight to the host menu
    refreshMicUI();
    refreshSyncUI(true);
  } else if ($("#tab-btn-host").classList.contains("active")) {
    // host tab is gone — fall back to Search
    document.querySelector('.tab[data-tab="search"]').click();
  }
  // delete buttons on the Songs tab appear/disappear with host rights
  if (!$("#tab-songs").classList.contains("hidden")) loadSongs();
}

$("#btn-host").addEventListener("click", async () => {
  const key = prompt("Host code — 4 letters/numbers shown on the TV:");
  if (!key) return;
  await ensureJoined();   // rejoin silently if the session was lost
  try {
    // TV shows capitals; phones may type lowercase — normalize both ways
    await api("/api/host/login", "POST", { key: key.trim().toUpperCase() });
    setHost(true);
    socket.emit("state:pull");   // re-render queue with host controls now
    toast("Host mode on — Host tab has pause/next, sync + audio controls");
  } catch (e) { toast(e.message || "Wrong host key", "error"); }
});

$("#btn-pause").addEventListener("click", async () => {
  try {
    await api(`/api/playback/${paused ? "resume" : "pause"}`, "POST");
    paused = !paused;
    $("#btn-pause").textContent = paused ? "Resume" : "Pause";
  } catch (e) { toast(e.message, "error"); }
});

$("#btn-next").addEventListener("click", () =>
  api("/api/playback/next", "POST").catch((e) => toast(e.message, "error")));

$("#btn-release").addEventListener("click", async () => {
  try {
    await api("/api/host/release", "POST");
    setHost(false);
    $("#btn-host").classList.remove("hidden");
    toast("You stepped down — someone else can take host");
  } catch (e) { toast(e.message, "error"); }
});

$("#btn-end-session").addEventListener("click", async () => {
  if (!confirm("End the session? Playback stops, the queue clears, and host is released for someone else.")) return;
  try { await api("/api/session/end", "POST"); }
  catch (e) { toast(e.message, "error"); }
});

/* live chart sync nudge (scored songs only) */
document.querySelectorAll("[data-sync]").forEach((b) =>
  b.addEventListener("click", async () => {
    try {
      const r = await api("/api/playback/sync", "POST",
                          { delta_ms: parseInt(b.dataset.sync, 10) });
      $("#sync-value").textContent = r.sync_offset_ms;
      toast(`Sync ${r.sync_offset_ms >= 0 ? "+" : ""}${r.sync_offset_ms} ms`);
    } catch (e) { toast(e.message, "error"); }
  }));

/* on-demand resync: collects ~10s of room audio then re-solves sync */
$("#btn-resync").addEventListener("click", async () => {
  const b = $("#btn-resync");
  b.disabled = true;
  b.textContent = "Listening… (~10s)";
  try {
    await api("/api/playback/resync", "POST", {});
    // the result arrives on sing:autosync; restore the button as a
    // fallback in case the solve never lands
    setTimeout(() => {
      b.disabled = false;
      b.textContent = "Resync from audio";
    }, 25000);
  } catch (e) {
    b.disabled = false;
    b.textContent = "Resync from audio";
    toast(e.message, "error");
  }
});

/* mic monitor + audio routing controls (host) */
let micMuted = false;

async function refreshMicUI() {
  // host-only panel: audio-out routing + mic→singer assignments
  if (!isHost) {
    $("#mic-controls").classList.add("hidden");
  } else {
    try {
      const s = await api("/api/audio");
      if (!s.mics.length && !s.outputs.length) {
        $("#mic-controls").classList.add("hidden");
      } else {
        $("#mic-controls").classList.remove("hidden");
    // output targets: PipeWire sinks + cards that need enabling
    const sel = $("#audio-out");
    sel.innerHTML = "";
    s.outputs.forEach((o) => {
      const opt = document.createElement("option");
      opt.value = o.kind === "sink" ? `s:${o.id}` : `c:${o.card}`;
      opt.textContent = (o.needs_enable ? "Enable " : "") + o.name;
      if (o.default) opt.selected = true;
      sel.appendChild(opt);
    });
    // one row per capture device: name, level meter, singer assignment
    const ml = $("#mic-list");
    ml.innerHTML = "";
    s.mics.forEach((m) => {
      const row = document.createElement("div");
      row.className = "muted";
      row.style.cssText = "display:flex;align-items:center;gap:.5rem;margin:.3rem 0";
      const selP = document.createElement("select");
      selP.style.maxWidth = "7em";
      selP.innerHTML = '<option value="">—</option>' +
        s.performers.map((p) =>
          `<option value="${p.id}">${p.name}</option>`).join("");
      if (m.assigned_to) selP.value = m.assigned_to;
      selP.title = "Assign mic to singer";
      selP.addEventListener("change", async () => {
        try {
          await api("/api/mic/assign", "POST",
            { device: m.device,
              performer_id: selP.value ? +selP.value : null });
          toast(`${m.name} → ${selP.selectedOptions[0].textContent}`);
        } catch (e) { toast(e.message, "error"); }
      });
      row.innerHTML =
        `<span style="min-width:7em;max-width:9em;overflow:hidden;` +
        `text-overflow:ellipsis;white-space:nowrap">${m.name}</span>` +
        `<div style="flex:1;height:6px;background:#333;border-radius:3px;` +
        `overflow:hidden"><div class="mic-lvl" data-dev="${m.device}" ` +
        `style="height:100%;width:0;background:${m.running ? "var(--accent,#0f0)" : "#a33"};` +
        `transition:width .15s"></div></div>`;
      row.appendChild(selP);
      ml.appendChild(row);
    });
        startMicMeter();
      }
    } catch (e) { /* ignore */ }
  }
  // sound card: host always; the singer at the mic while their song plays
  try {
    const s = await api("/api/mic");
    if (!s.can_mix) { $("#mix-card").classList.add("hidden"); return; }
    $("#mix-card").classList.remove("hidden");
    micMuted = s.muted;
    $("#btn-mute").textContent = micMuted ? "Unmute" : "Mute";
    $("#mic-vol").value = Math.round(s.volume * 100);
    $("#mic-vol-v").textContent = Math.round(s.volume * 100);
    // voice FX sliders show MY saved profile when I have a performer
    // identity — they travel with the singer, not the room defaults
    const fx = s.my_fx || {};
    $("#mic-echo").value = Math.round((fx.echo_wet ?? s.echo_wet) * 100);
    $("#mic-echo-v").textContent = Math.round((fx.echo_wet ?? s.echo_wet) * 100);
    $("#mic-verb").value = Math.round((fx.reverb_wet ?? s.reverb_wet ?? 0) * 100);
    $("#mic-verb-v").textContent =
      Math.round((fx.reverb_wet ?? s.reverb_wet ?? 0) * 100);
    const p = Math.round(fx.pitch ?? s.pitch ?? 0);
    $("#mic-pitch").value = p;
    $("#mic-pitch-v").textContent = (p > 0 ? "+" : "") + p;
    const t = Math.round((fx.autotune ?? s.autotune ?? 0) * 100);
    $("#mic-tune").value = t;
    $("#mic-tune-v").textContent = t;
    $("#mic-polish").checked = !!(fx.polish ?? s.polish);
    const mv = Math.round((s.music_volume ?? 1) * 100);
    $("#song-vol").value = mv;
    $("#song-vol-v").textContent = mv;
    $("#mic-delay-v").textContent = Math.round(s.mic_delay_ms || 0);
  } catch (e) { /* ignore */ }
}

$("#audio-out").addEventListener("change", async (e) => {
  const [k, v] = e.target.value.split(":");
  try {
    const r = await api("/api/audio/output", "POST",
      k === "s" ? { sink: +v } : { card: +v });
    toast(`Audio → ${r.name}`);
    refreshMicUI();
  } catch (e2) { toast(e2.message, "error"); }
});

let micMeterTimer = null;
function startMicMeter() {
  if (micMeterTimer) return;
  micMeterTimer = setInterval(async () => {
    const box = $("#mic-controls");
    if (box.classList.contains("hidden") || !document.body.contains(box)) {
      clearInterval(micMeterTimer); micMeterTimer = null; return;
    }
    try {
      const s = await api("/api/mic");
      document.querySelectorAll(".mic-lvl").forEach((el) => {
        const m = (s.mics || []).find((x) => x.device === el.dataset.dev);
        if (!m) return;
        el.style.width = m.running
          ? Math.min(100, Math.round(m.level * 300)) + "%" : "0";
        el.style.background = !m.running ? "#a33"
          : m.level > 0.25 ? "#e33" : "var(--accent,#0f0)";
      });
    } catch (e) { /* ignore */ }
  }, 500);
}

$("#btn-mute").addEventListener("click", async () => {
  micMuted = !micMuted;
  $("#btn-mute").textContent = micMuted ? "Unmute" : "Mute";
  try { await api("/api/mic", "POST", { muted: micMuted }); }
  catch (e) { toast(e.message, "error"); }
});

$("#mic-vol").addEventListener("input", async (e) => {
  const v = parseInt(e.target.value, 10);
  $("#mic-vol-v").textContent = v;
  try { await api("/api/mic", "POST", { volume: v / 100 }); }
  catch (e) { /* ignore */ }
});

$("#mic-echo").addEventListener("input", async (e) => {
  const v = parseInt(e.target.value, 10);
  $("#mic-echo-v").textContent = v;
  try { await api("/api/mic", "POST", { echo_wet: v / 100 }); }
  catch (e) { /* ignore */ }
});

$("#mic-verb").addEventListener("input", async (e) => {
  const v = parseInt(e.target.value, 10);
  $("#mic-verb-v").textContent = v;
  try { await api("/api/mic", "POST", { reverb: v / 100 }); }
  catch (e) { /* ignore */ }
});

$("#mic-pitch").addEventListener("input", async (e) => {
  const v = parseInt(e.target.value, 10);
  $("#mic-pitch-v").textContent = (v > 0 ? "+" : "") + v;
  try { await api("/api/mic", "POST", { pitch: v }); }
  catch (e) { /* ignore */ }
});

$("#mic-tune").addEventListener("input", async (e) => {
  const v = parseInt(e.target.value, 10);
  $("#mic-tune-v").textContent = v;
  try { await api("/api/mic", "POST", { autotune: v / 100 }); }
  catch (e) { /* ignore */ }
});

$("#mic-polish").addEventListener("change", async (e) => {
  try { await api("/api/mic", "POST", { polish: e.target.checked }); }
  catch (e) { /* ignore */ }
});

// big-tap controls: steppers nudge by data-d, presets set data-v — both
// drive the hidden range input so the existing 'input' handlers fire
document.querySelectorAll(".fxbtn").forEach((b) => {
  b.addEventListener("click", () => {
    const el = $("#" + b.dataset.for);
    if (!el) return;
    el.value = b.dataset.v !== undefined
      ? b.dataset.v
      : Math.min(+el.max, Math.max(+el.min, +el.value + +b.dataset.d));
    el.dispatchEvent(new Event("input"));
  });
});

$("#song-vol").addEventListener("input", async (e) => {
  const v = parseInt(e.target.value, 10);
  $("#song-vol-v").textContent = v;
  try { await api("/api/playback/volume", "POST", { volume: v / 100 }); }
  catch (e) { /* ignore */ }
});

$("#btn-calib").addEventListener("click", async () => {
  const b = $("#btn-calib");
  b.disabled = true; b.textContent = "Listening…";
  try {
    const r = await api("/api/mic/calibrate", "POST", {});
    $("#mic-delay-v").textContent = Math.round(r.mic_delay_ms);
    toast(`Mic delay: ${Math.round(r.mic_delay_ms)} ms — scoring adjusted`);
  } catch (e) { toast(e.message, "error"); }
  finally { b.disabled = false; b.textContent = "Calibrate"; }
});

async function refreshSyncUI(playing) {
  const box = $("#sync-controls");
  if (!isHost || !playing) { box.classList.add("hidden"); return; }
  try {
    const r = await api("/api/playback/sync");
    if (r.sync_offset_ms == null) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    $("#sync-value").textContent = r.sync_offset_ms;
  } catch (e) { /* ignore */ }
}

function setJoined(name) {
  myName = name;
  localStorage.setItem("ups_name", name);
  joinCard.classList.add("hidden");
  remote.classList.remove("hidden");
  if (!isHost) $("#btn-host").classList.remove("hidden");
}

function showJoin() {
  remote.classList.add("hidden");
  joinCard.classList.remove("hidden");
}

// server-side session can be lost even though localStorage still shows a
// name — rejoin silently so host/queue calls don't fail out from under us
async function ensureJoined() {
  if (!myName) return;
  try {
    const me = await api("/api/me");
    if (me.performer) { myPerformerId = me.performer.id; return; }
    const res = await api("/api/join", "POST", { name: myName });
    if (res.performer) myPerformerId = res.performer.id;
  } catch (e) { /* offline */ }
}

if (myName) {
  $("#join-name").value = myName;
  setJoined(myName);
}

// restore host + performer state (session persists across reloads)
api("/api/me").then(async (me) => {
  if (me.performer) myPerformerId = me.performer.id;
  else await ensureJoined();
  if (me.is_host) setHost(true);
  $("#btn-host").classList.toggle("hidden", me.is_host || !myName);
  refreshMicUI();
}).catch(() => {});

$("#join-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("#join-name").value.trim();
  if (!name) return;
  try {
    const res = await api("/api/join", "POST", { name });
    if (res.performer) myPerformerId = res.performer.id;
    setJoined(name);
    toast(`Welcome, ${name}!`);
  } catch (err) { toast(err.message, "error"); }
});

/* tabs */
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.add("hidden"));
    btn.classList.add("active");
    $("#tab-" + btn.dataset.tab).classList.remove("hidden");
    if (btn.dataset.tab === "scores") loadScores();
    if (btn.dataset.tab === "songs") loadSongs();
  });
});

/* song/version management — every downloaded media file, grouped by song */
function fmtSize(b) {
  if (b >= 1e9) return (b / 1e9).toFixed(1) + " GB";
  if (b >= 1e6) return Math.round(b / 1e6) + " MB";
  if (b >= 1e3) return Math.round(b / 1e3) + " KB";
  return "—";
}

function fmtDur(s) {
  if (!s) return "";
  return Math.floor(s / 60) + ":" + String(Math.round(s % 60)).padStart(2, "0");
}

async function loadSongs() {
  try {
    const { songs } = await api("/api/manage/songs");
    renderSongs(songs);
  } catch (e) { /* offline */ }
}

function renderSongs(rows) {
  const box = $("#songs-list");
  box.innerHTML = "";
  const groups = new Map();
  rows.forEach((r) => {
    if (!groups.has(r.song_id)) {
      groups.set(r.song_id, { id: r.song_id, artist: r.artist,
                              title: r.title, is_duet: r.is_duet,
                              entries: [] });
    }
    groups.get(r.song_id).entries.push(r);
  });
  let total = 0;
  groups.forEach((g) =>
    g.entries.forEach((e) => { total += e.size_bytes || 0; }));
  $("#songs-empty").classList.toggle("hidden", groups.size > 0);
  $("#songs-count").textContent = groups.size
    ? `${groups.size} song${groups.size === 1 ? "" : "s"} · ` +
      `${rows.length} version${rows.length === 1 ? "" : "s"} · ` +
      `${fmtSize(total)} on disk` : "";

  groups.forEach((g) => {
    const card = document.createElement("div");
    card.className = "card";
    card.style.marginBottom = ".6rem";
    card.innerHTML =
      `<div class="row" style="justify-content:space-between;` +
      `align-items:flex-start;gap:.4rem">` +
      `<div style="min-width:0"><b>${g.title}</b> — ${g.artist}` +
      (g.is_duet ? ' <span class="badge duet">duet</span>' : "") +
      `<br><span class="muted">${g.entries.length} version` +
      `${g.entries.length === 1 ? "" : "s"}</span></div>` +
      (isHost ? `<button class="danger del-song">Delete</button>` : "") +
      `</div>`;
    if (isHost) {
      card.querySelector(".del-song").addEventListener("click", async () => {
        if (!confirm(`Delete "${g.title}" and all its versions? ` +
                     `Video files are removed; scores are kept.`)) return;
        try {
          await api(`/api/manage/song/${g.id}/delete`, "POST");
          toast(`Deleted ${g.title}`);
          loadSongs();
        } catch (e) { toast(e.message, "error"); }
      });
    }
    const ul = document.createElement("ul");
    ul.className = "results";
    g.entries.forEach((e) => {
      const li = document.createElement("li");
      const status = e.status !== "available"
        ? ` <span class="badge video-only">${e.status}</span>` : "";
      const chart = e.processing
        ? ' <span class="badge duet">processing…</span>'
        : e.chart_source === "generated"
          ? ' <span class="badge scoring">generated</span>'
          : e.chart_source
            ? ` <span class="badge scoring">${e.chart_source}</span>`
            : e.scored
              ? ' <span class="badge scoring">scored</span>'
              : e.possible_chart
                ? ' <span class="badge video-only">chart?</span>'
                : ' <span class="badge video-only">no chart</span>';
      const inst = e.has_inst ? ' <span class="badge duet">karaoke</span>' : "";
      const live = e.playing
        ? ' <span class="badge scoring">playing</span>'
        : e.queued
          ? ` <span class="badge duet">queued ×${e.queued}</span>` : "";
      const label = e.local_path || e.source_id;
      const btns = isHost
        ? `<div class="row" style="gap:.4rem">` +
          (e.local_path && e.status === "available"
            ? `<button class="chip repair-btn">Repair</button>` : "") +
          (!e.playing ? `<button class="danger del-btn">Delete</button>` : "") +
          `</div>` : "";
      li.innerHTML =
        `<div class="meta"><div class="title" style="font-size:.95em;` +
        `word-break:break-all">${label}</div>` +
        `<div class="muted">${e.provider}${fmtDur(e.duration_sec) ?
        " · " + fmtDur(e.duration_sec) : ""}` +
        `${e.size_bytes ? " · " + fmtSize(e.size_bytes) : ""}` +
        `${status}${chart}${inst}${live}</div></div>` + btns;
      const rep = li.querySelector(".repair-btn");
      if (rep) rep.addEventListener("click", async () => {
        rep.disabled = true;
        rep.textContent = "…";
        try {
          const r = await api(`/api/manage/entry/${e.entry_id}/repair`,
                              "POST");
          toast(r.regenerating ? `Regenerating chart for ${label}`
                               : `Reprocessing ${label}`);
          setTimeout(loadSongs, 4000);
          setTimeout(loadSongs, 70000);
        } catch (err) {
          toast(err.message, "error");
          rep.disabled = false;
          rep.textContent = "Repair";
        }
      });
      const del = li.querySelector(".del-btn");
      if (del) del.addEventListener("click", async () => {
        if (!confirm(`Delete this version (${label})?`)) return;
        try {
          await api(`/api/manage/entry/${e.entry_id}/delete`, "POST");
          toast(`Deleted ${label}`);
          loadSongs();
        } catch (err) { toast(err.message, "error"); }
      });
      ul.appendChild(li);
    });
    card.appendChild(ul);
    box.appendChild(card);
  });
  if (!isHost && groups.size) {
    const hint = document.createElement("p");
    hint.className = "muted";
    hint.textContent = "Tap Host (top-right) and enter the TV code " +
                       "to get repair and delete buttons.";
    box.appendChild(hint);
  }
}

/* scoreboard — phone-only view (TV keeps showing the party screen) */
async function loadScores() {
  try {
    const { songs, singers } = await api("/api/scores");
    const sb = $("#singer-board");
    sb.innerHTML = "";
    singers.forEach((p, i) => {
      const li = document.createElement("li");
      li.innerHTML = `<b class="muted">#${i + 1}</b> <span class="who">🏆 ${p.name}</span>` +
        `<span class="muted">${p.songs} song${p.songs === 1 ? "" : "s"} · best ${p.best} · avg ${p.avg}</span>` +
        `<span class="pts">${p.total}</span>`;
      sb.appendChild(li);
    });
    $("#singer-empty").classList.toggle("hidden", singers.length > 0);
    const gb = $("#song-board");
    gb.innerHTML = "";
    songs.forEach((s) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="who">${s.title} — ${s.artist}` +
        `<br><span class="muted">🏆 ${s.scorer}</span></span>` +
        `<span class="pts">${s.score}</span>`;
      gb.appendChild(li);
    });
    $("#song-empty").textContent = songs.length ? "" : "No records yet.";
  } catch (e) { /* offline */ }
}

/* search + browse */
let browseFilter = "all";
let lastResults = [];

document.querySelectorAll("#browse-filters .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll("#browse-filters .chip")
      .forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    browseFilter = chip.dataset.f;
    renderResults(lastResults, $("#search-q").value);
  });
});

function matchesFilter(r) {
  switch (browseFilter) {
    case "duet": return r.is_duet || r.chart_duet;
    case "scored": return r.scoring_available;
    case "chart": return r.scoring_available || r.possible_chart;
    default: return true;
  }
}

function resultRow(r) {
  const li = document.createElement("li");
  const thumb = r.thumbnail
    ? `<img class="thumb" src="${r.thumbnail}" alt="" loading="lazy">` : "";
  const disabled = r.error ? "disabled" : "";
  const top = r.top_scorer
    ? `<div class="topscore" title="Record held by ${r.top_scorer}">🏆 ${r.top_scorer}<br>${r.top_score}</div>` : "";
  li.innerHTML = thumb +
    `<div class="meta"><div class="title">${r.title}</div>` +
    `<div class="muted">${r.artist}${r.provider === "youtube" ? " · YouTube" : ""}` +
    (r.karaoke === true ? ' <span class="badge video-only">karaoke</span>'
      : r.karaoke === false ? ' <span class="badge scoring">original</span>' : "") +
    (r.in_library ? ' <span class="badge scoring">in library</span>' : "") +
    `</div>${badges(r)}</div>` + top +
    `<button class="primary" ${disabled}>Queue</button>`;
  if (!r.error) li.querySelector("button").addEventListener("click", () => openConfirm(r));
  return li;
}

function renderResults(results, q) {
  const ul = $("#results");
  ul.innerHTML = "";
  // dedupe rows pointing at the same catalog entry (local row + remote row
  // marked in_library both carry entry_id)
  const seen = new Set();
  const rows = results.filter((r) => {
    if (r.entry_id == null) return true;
    if (seen.has(r.entry_id)) return false;
    seen.add(r.entry_id);
    return true;
  }).filter(matchesFilter);

  if (!q && rows.length) {
    // browse mode: A-Z grouped list like a proper songbook
    let letter = "";
    const sorted = [...rows].sort((a, b) =>
      (a.artist + a.title).localeCompare(b.artist + b.title));
    sorted.forEach((r) => {
      const l = (r.artist || "#")[0].toUpperCase();
      if (l !== letter) {
        letter = l;
        const h = document.createElement("li");
        h.className = "letter";
        h.textContent = letter;
        ul.appendChild(h);
      }
      ul.appendChild(resultRow(r));
    });
  } else {
    rows.forEach((r) => ul.appendChild(resultRow(r)));
  }
  $("#song-count").textContent =
    !q && rows.length ? `${rows.length} songs in library` : "";
  if (!rows.length) ul.innerHTML = '<li class="muted">No matches.</li>';
}

async function runSearch(q = "") {
  const { results } = await api("/api/search?q=" + encodeURIComponent(q));
  lastResults = results;
  renderResults(results, q);
}
$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  runSearch($("#search-q").value);
});
runSearch(""); // show library on open

/* playlist import — expands server-side; songs queue one at a time
   through the serial download pipeline */
$("#playlist-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("#playlist-url").value.trim();
  if (!url) return;
  const st = $("#playlist-status");
  st.classList.remove("hidden");
  st.textContent = "Expanding playlist…";
  try {
    const r = await api("/api/playlist", "POST", { url, name: myName });
    st.textContent = `Importing “${r.title}” — ${r.total} songs…`;
  } catch (err) {
    st.textContent = err.message;
  }
});
socket.on("playlist:progress", (p) => {
  const st = $("#playlist-status");
  st.classList.remove("hidden");
  st.textContent = `Importing ${p.done}/${p.total} — ${p.queued} queued, ${p.skipped} already in library`;
});
socket.on("playlist:done", (d) => {
  const st = $("#playlist-status");
  st.classList.remove("hidden");
  st.textContent = `“${d.title}” done — ${d.queued} queued, ${d.skipped} skipped, ${d.failed} failed`;
  $("#playlist-url").value = "";
});

/* confirm sheet */
function openConfirm(entry) {
  pendingEntry = entry;
  $("#confirm-title").textContent = `${entry.title} — ${entry.artist}`;
  $("#confirm-meta").textContent = `${entry.provider}${entry.language ? " · " + entry.language : ""}`;
  $("#confirm-chart-note").innerHTML = badges(entry) +
    (entry.scoring_available ? "" :
      '<p class="muted">No verified chart for this video — it will play without scoring.</p>');
  $("#confirm-rom-wrap").classList.toggle("hidden", !entry.chart_alt);
  const isDuet = entry.is_duet || entry.chart_duet;
  $("#confirm-singer2-wrap").classList.toggle("hidden", !isDuet);
  const partWrap = $("#confirm-part-wrap");
  partWrap.classList.toggle("hidden", !isDuet);
  if (isDuet && entry.tracks) {
    const sel = $("#confirm-part");
    sel.innerHTML = '<option value="">Either</option>' +
      entry.tracks.map((t) => `<option>${t}</option>`).join("");
  }
  $("#confirm-name").value = myName;
  $("#confirm").showModal();
}

$("#confirm-form").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const body = {
    entry_id: pendingEntry.entry_id,
    provider: pendingEntry.provider,
    source_id: pendingEntry.source_id,
    title: pendingEntry.title,
    artist: pendingEntry.artist,
    duration_sec: pendingEntry.duration_sec,
    thumbnail: pendingEntry.thumbnail,
    name: $("#confirm-name").value.trim() || myName,
    part: $("#confirm-part").value,
    name2: $("#confirm-singer2").value.trim(),
    vocal_cut: $("#confirm-vocal-cut").checked ? 1 : 0,
    lyrics_rom: ($("#confirm-rom").checked
                 && !$("#confirm-rom-wrap").classList.contains("hidden")) ? 1 : 0,
  };
  try {
    await api("/api/queue", "POST", body);
    $("#confirm").close();
    setJoined(body.name);
    toast(`Queued: ${pendingEntry.title}`);
  } catch (err) { toast(err.message, "error"); }
});

/* upload your own mp3/mp4 — lands as a local entry, pipeline starts
   immediately, then the normal confirm sheet queues it */
$("#btn-upload").addEventListener("click", () => $("#upload").showModal());
$("#upload-form").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const f = $("#up-file").files[0];
  if (!f) { toast("Pick a file first", "error"); return; }
  const fd = new FormData();
  fd.append("file", f);
  fd.append("artist", $("#up-artist").value.trim());
  fd.append("title", $("#up-title").value.trim());
  const btn = $("#up-add");
  btn.disabled = true;
  btn.textContent = "Uploading…";
  try {
    const res = await fetch("/api/upload", { method: "POST", body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    $("#upload").close();
    $("#up-file").value = "";
    toast(`Added "${data.entry.title}" — building its chart in the background`);
    openConfirm(data.entry);
  } catch (err) {
    toast(err.message || "Upload failed", "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Upload";
  }
});

/* live state */
socket.on("state", (state) => {
  refreshMicUI();   // can_mix flips when our song starts or ends
  const list = $("#queue-list");
  list.innerHTML = "";
  $("#queue-empty").classList.toggle("hidden", state.queue.length > 0);
  state.queue.forEach((it, i) => {
    const li = document.createElement("li");
    // open duet slot anyone else can claim
    const openDuet = it.is_duet && !it.singer2 && it.performer_id !== myPerformerId;
    li.innerHTML =
      `<span class="pos">${i + 1}</span>` +
      `<span class="meta"><b>${it.title}</b> — ${it.artist}<br>` +
      singerChip(it.singer, it.singer_color) +
      (it.singer2 ? " &amp; " + singerChip(it.singer2, it.singer2_color) : "") +
      (it.vocal_cut ? ' <span class="badge scoring">no vocals</span>' : "") +
      (it.lyrics_rom ? ' <span class="badge scoring">romaji</span>' : "") +
      (openDuet ? ' <span class="badge scoring">duet — P2 open</span>' : "") +
      (it.entry_status === "downloading" ? ' <span class="badge video-only">downloading</span>' : "") +
      (it.entry_status === "failed" ? ' <span class="badge" style="background:#5c1f24;color:#ffb4b4">download failed</span>' : "") +
      `</span>` +
      (openDuet ? `<button class="duet-join chip">Join P2</button>` : "") +
      (isHost ? `<button class="mv" data-d="up">▲</button>` +
                `<button class="mv" data-d="down">▼</button>` : "") +
      `<button class="rm">✕</button>`;
    if (openDuet) li.querySelector(".duet-join").addEventListener("click", async () => {
      try {
        await api(`/api/queue/${it.id}/join-duet`, "POST");
        toast(`You're singing P2 on "${it.title}"!`);
      } catch (err) { toast(err.message, "error"); }
    });
    li.querySelector(".rm").addEventListener("click", async () => {
      try { await api(`/api/queue/${it.id}/remove`, "POST"); }
      catch (err) { toast(err.message, "error"); }
    });
    li.querySelectorAll(".mv").forEach((b) =>
      b.addEventListener("click", () =>
        api(`/api/queue/${it.id}/move`, "POST", { direction: b.dataset.d })
          .catch((e) => toast(e.message, "error"))));
    list.appendChild(li);
  });

  const now = $("#now-card");
  if (state.now_playing) {
    const it = state.now_playing;
    const openDuet = it.is_duet && !it.singer2 && it.performer_id !== myPerformerId;
    now.className = "card";
    now.innerHTML = `<b>${it.title}</b> — ${it.artist}<br>` +
      singerChip(it.singer, it.singer_color) +
      (it.singer2 ? " &amp; " + singerChip(it.singer2, it.singer2_color) : "") +
      (openDuet ? ` <button id="duet-join-now" class="chip">Join P2</button>` : "") +
      `<div id="live-score" class="muted"></div>`;
    const dj = $("#duet-join-now");
    if (dj) dj.addEventListener("click", async () => {
      try {
        await api(`/api/queue/${it.id}/join-duet`, "POST");
        toast(`You're singing P2 on "${it.title}"!`);
      } catch (err) { toast(err.message, "error"); }
    });
    refreshSyncUI(true);
  } else {
    refreshSyncUI(false);
    now.className = "card muted";
    now.textContent = "Nothing is playing.";
  }
});

socket.on("session:ended", () => {
  setHost(false);
  $("#btn-host").classList.remove("hidden");
  toast("Session ended — the code on the TV is free to claim again");
});

/* autosync result — feedback for the Resync button + keeps the host's
   displayed offset current after automatic solves too */
socket.on("sing:autosync", (r) => {
  const b = $("#btn-resync");
  if (b && b.disabled) {
    b.disabled = false;
    b.textContent = "Resync from audio";
  }
  if (r.offset_ms != null) $("#sync-value").textContent = r.offset_ms;
  if (b) {
    toast(r.delta_ms
      ? `Lyrics synced ${r.delta_ms >= 0 ? "+" : ""}${r.delta_ms} ms` +
        (r.method ? ` (${r.method})` : "")
      : "Sync checked — already aligned");
  }
});

/* live score mirror on the phone */
socket.on("sing:update", (u) => {
  const el = $("#live-score");
  if (!el) return;
  el.innerHTML = Object.entries(u.players).map(([t, p]) =>
    `<b>${t}</b> ${p.score}`).join(" · ") +
    (u.tone_str && u.tone_str !== "-" ? ` · singing ${u.tone_str}` : "");
});
