/* Host console: reorder/skip/remove/clear + now-playing control.
   Session already carries host auth (set via /host?key=). */

function render(state) {
  const now = $("#now-card");
  if (state.now_playing) {
    const it = state.now_playing;
    now.className = "";
    now.innerHTML = `<b>${it.title}</b> — ${it.artist}<br>` +
      singerChip(it.singer, it.singer_color) +
      (it.singer2 ? " &amp; " + singerChip(it.singer2, it.singer2_color) : "");
  } else {
    now.className = "muted";
    now.textContent = "Idle";
  }

  $("#queue-count").textContent = `(${state.queue.length})`;
  const ol = $("#host-queue");
  ol.innerHTML = "";
  state.queue.forEach((it, i) => {
    const li = document.createElement("li");
    li.innerHTML =
      `<span class="pos">${i + 1}</span>` +
      `<span class="meta"><b>${it.title}</b> — ${it.artist}<br>` +
      singerChip(it.singer, it.singer_color) +
      (it.singer2 ? " &amp; " + singerChip(it.singer2, it.singer2_color) : "") +
      (it.entry_status === "downloading" ? ' <span class="badge video-only">downloading…</span>' : "") +
      (it.entry_status === "failed" ? ' <span class="badge" style="background:#5c1f24;color:#ffb4b4">download failed</span>' : "") +
      `</span><span class="controls">` +
      `<button data-a="up" title="Move up">▲</button>` +
      `<button data-a="down" title="Move down">▼</button>` +
      `<button data-a="rm" title="Remove">✕</button></span>`;
    li.querySelector('[data-a="up"]').addEventListener("click", () =>
      api(`/api/queue/${it.id}/move`, "POST", { direction: "up" }).catch((e) => toast(e.message, "error")));
    li.querySelector('[data-a="down"]').addEventListener("click", () =>
      api(`/api/queue/${it.id}/move`, "POST", { direction: "down" }).catch((e) => toast(e.message, "error")));
    li.querySelector('[data-a="rm"]').addEventListener("click", () =>
      api(`/api/queue/${it.id}/remove`, "POST").catch((e) => toast(e.message, "error")));
    ol.appendChild(li);
  });
}

socket.on("state", render);

/* ---------- lyrics sync controls ---------- */

let hasChart = false;

socket.on("now_playing", async (p) => {
  hasChart = !!(p && p.lyrics);
  $("#sync-controls").classList.toggle("hidden", !hasChart);
  $("#sync-status").textContent = "";
  if (hasChart) {
    try {
      const r = await api("/api/playback/sync");
      $("#sync-value").textContent = r.sync_offset_ms ?? "—";
      $("#autosync-mic").checked = r.autosync_mic !== false;
    } catch (e) { /* no chart — stays hidden */ }
  }
});
socket.on("lyrics", (lyr) => {
  hasChart = !!lyr;
  $("#sync-controls").classList.toggle("hidden", !hasChart);
  if (lyr && lyr.offset_ms != null)
    $("#sync-value").textContent = lyr.offset_ms;
});

document.querySelectorAll("[data-sync]").forEach((b) =>
  b.addEventListener("click", async () => {
    try {
      const r = await api("/api/playback/sync", "POST",
                          { delta_ms: parseInt(b.dataset.sync, 10) });
      $("#sync-value").textContent = r.sync_offset_ms;
      toast(`Sync ${r.sync_offset_ms >= 0 ? "+" : ""}${r.sync_offset_ms} ms`);
    } catch (e) { toast(e.message, "error"); }
  }));

$("#btn-resync").addEventListener("click", async () => {
  const b = $("#btn-resync");
  b.disabled = true;
  b.textContent = "Listening… (~10s)";
  try {
    await api("/api/playback/resync", "POST", {});
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

$("#autosync-mic").addEventListener("change", async (e) => {
  try {
    await api("/api/playback/autosync", "POST",
              { enabled: e.target.checked });
    toast(`Mic auto-sync ${e.target.checked ? "on" : "off"}`);
  } catch (e2) { toast(e2.message, "error"); }
});

socket.on("sing:autosync", (r) => {
  if (r && r.offset_ms != null) {
    $("#sync-value").textContent = r.offset_ms;
    $("#sync-status").textContent =
      `· auto-locked via ${r.method || "pitch"}`;
  }
});

$("#btn-skip").addEventListener("click", () =>
  api("/api/playback/next", "POST").catch((e) => toast(e.message, "error")));
$("#btn-play").addEventListener("click", () =>
  api("/api/playback/next", "POST").catch((e) => toast(e.message, "error")));
$("#btn-clear").addEventListener("click", async () => {
  if (!confirm("Clear the whole queue?")) return;
  try { await api("/api/queue/clear", "POST"); toast("Queue cleared"); }
  catch (e) { toast(e.message, "error"); }
});

$("#btn-end-session").addEventListener("click", async () => {
  if (!confirm("End the session? Playback stops, the queue clears, and host is released.")) return;
  try { await api("/api/session/end", "POST"); toast("Session ended"); }
  catch (e) { toast(e.message, "error"); }
});

/* ---------- audio output + mic controls ---------- */

let micMuted = false;
let micLevelTimer = null;
let musicVol = null, micVol = null;

async function loadAudio() {
  try {
    const s = await api("/api/audio");
    $("#audio-empty").classList.toggle("hidden",
      s.outputs.length > 0 || s.mics.length > 0);

    // output targets: live sinks + cards that need enabling
    const sel = $("#audio-out");
    sel.innerHTML = "";
    s.outputs.forEach((o) => {
      const opt = document.createElement("option");
      opt.value = o.kind === "sink" ? `s:${o.id}` : `c:${o.card}`;
      opt.textContent = (o.needs_enable ? "Enable " : "") + o.name;
      if (o.default) opt.selected = true;
      sel.appendChild(opt);
    });

    // one row per capture device: name, live level, singer assignment
    const ml = $("#mic-list");
    ml.innerHTML = "";
    s.mics.forEach((m) => {
      const row = document.createElement("div");
      row.className = "muted";
      row.style.cssText =
        "display:flex;align-items:center;gap:.5rem;margin:.3rem 0";
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
        `<span style="min-width:8em;max-width:10em;overflow:hidden;` +
        `text-overflow:ellipsis;white-space:nowrap">${m.name}</span>` +
        `<div style="flex:1;height:6px;background:#333;border-radius:3px;` +
        `overflow:hidden"><div class="mic-lvl" data-dev="${m.device}" ` +
        `style="height:100%;width:0;background:${m.running ? "var(--ok)" : "#a33"};` +
        `transition:width .15s"></div></div>`;
      row.appendChild(selP);
      ml.appendChild(row);
    });
    startMicMeter();

    // monitor mix: volume/mute/delay shown beside the controls
    if (s.monitor) {
      micVol = Math.round(s.monitor.volume * 100);
      micMuted = s.monitor.muted;
      $("#mic-vol-v").textContent = micVol;
      $("#btn-mute").textContent = micMuted ? "Unmute" : "Mute";
    }
    const ms = await api("/api/mic");
    musicVol = Math.round((ms.music_volume ?? 1) * 100);
    $("#song-vol-v").textContent = musicVol;
    $("#mic-delay-v").textContent =
      `delay ${Math.round(ms.mic_delay_ms || 0)} ms`;
  } catch (e) { /* audio panel is best-effort */ }
}

$("#audio-out").addEventListener("change", async (e) => {
  const [k, v] = e.target.value.split(":");
  try {
    const r = await api("/api/audio/output", "POST",
      k === "s" ? { sink: +v } : { card: +v });
    toast(`Audio → ${r.name}`);
    loadAudio();
  } catch (e2) { toast(e2.message, "error"); }
});

function startMicMeter() {
  if (micLevelTimer) return;
  micLevelTimer = setInterval(async () => {
    if (!document.body.contains($("#mic-list"))) {
      clearInterval(micLevelTimer); micLevelTimer = null; return;
    }
    try {
      const s = await api("/api/mic");
      document.querySelectorAll(".mic-lvl").forEach((el) => {
        const m = (s.mics || []).find((x) => x.device === el.dataset.dev);
        if (!m) return;
        el.style.width = m.running
          ? Math.min(100, Math.round(m.level * 300)) + "%" : "0";
        el.style.background = !m.running ? "#a33"
          : m.level > 0.25 ? "#e33" : "var(--ok)";
      });
    } catch (e) { /* ignore */ }
  }, 500);
}

// music + mic-monitor volume steppers
document.querySelectorAll("[data-vold]").forEach((b) =>
  b.addEventListener("click", async () => {
    if (musicVol == null) return;
    musicVol = Math.max(0, Math.min(100, musicVol + +b.dataset.vold));
    $("#song-vol-v").textContent = musicVol;
    try { await api("/api/playback/volume", "POST",
                    { volume: musicVol / 100 }); }
    catch (e) { toast(e.message, "error"); }
  }));
document.querySelectorAll("[data-mvold]").forEach((b) =>
  b.addEventListener("click", async () => {
    if (micVol == null) return;
    micVol = Math.max(0, Math.min(200, micVol + +b.dataset.mvold));
    $("#mic-vol-v").textContent = micVol;
    try { await api("/api/mic", "POST", { volume: micVol / 100 }); }
    catch (e) { toast(e.message, "error"); }
  }));

$("#btn-mute").addEventListener("click", async () => {
  micMuted = !micMuted;
  $("#btn-mute").textContent = micMuted ? "Unmute" : "Mute";
  try { await api("/api/mic", "POST", { muted: micMuted }); }
  catch (e) { toast(e.message, "error"); }
});

$("#btn-calib").addEventListener("click", async () => {
  const b = $("#btn-calib");
  b.disabled = true;
  try {
    const r = await api("/api/mic/calibrate", "POST", {});
    if (r.mic_delay_ms != null) {
      $("#mic-delay-v").textContent = `delay ${Math.round(r.mic_delay_ms)} ms`;
      toast(`Mic delay ${Math.round(r.mic_delay_ms)} ms`);
    } else {
      toast(r.error || "Calibration failed", "error");
    }
  } catch (e) { toast(e.message, "error"); }
  b.disabled = false;
});

loadAudio();

/* ---------- chart verification ---------- */

async function loadPendingCharts() {
  const { charts } = await api("/api/catalog/pending-charts");
  $("#chart-count").textContent = charts.length ? `(${charts.length})` : "";
  $("#charts-empty").classList.toggle("hidden", charts.length > 0);
  const ul = $("#pending-charts");
  ul.innerHTML = "";
  charts.forEach((c) => {
    const li = document.createElement("li");
    li.innerHTML =
      `<span class="meta"><b>${c.artist} — ${c.title}</b><br>` +
      `<span class="muted">${c.file_path.split(/[\\/]/).pop()} · ${c.source_repo || "local"}` +
      (c.is_duet ? ' · <span class="badge duet">duet</span>' : "") +
      `</span></span>` +
      `<button class="primary">Verify</button>`;
    li.querySelector("button").addEventListener("click", async () => {
      try {
        await api(`/api/catalog/chart/${c.id}/verify`, "POST",
                  { media_entry_id: c.media_entry_id });
        toast(`Verified: ${c.artist} — ${c.title}`);
        loadPendingCharts();
      } catch (e) { toast(e.message, "error"); }
    });
    ul.appendChild(li);
  });
}

$("#btn-rescan").addEventListener("click", async () => {
  try {
    const r = await api("/api/catalog/rescan", "POST");
    toast(`Indexed ${r.charts_added} new charts`);
    loadPendingCharts();
  } catch (e) { toast(e.message, "error"); }
});

loadPendingCharts();
