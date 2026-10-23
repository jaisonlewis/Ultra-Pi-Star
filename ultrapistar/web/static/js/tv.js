/* TV screen: idle/join + full-screen player.
   The server pushes now_playing payloads; this page owns the <video> clock and
   reports lifecycle events back (player:ended / player:error / player:position).
   Scoring overlays (Impl Phase 3) will attach to #lyric-band/#score-strip. */

const video = $("#video");
const lane = $("#pitch-lane");
const PRE_COUNT_SECONDS = 5;
const LANE_WINDOW_MS = 4200;   // visible time window on the pitch lane
const LANE_PAD_MS = 600;
let clockTimer = null;
let countdownTimer = null;
let neverStartTimer = null;
function armNeverStart() {
  /* play() that never takes leaves the element paused with no error
     event — usually a dead audio path (Chromium can't open a PipeWire
     stream, so the media clock can't start). Report it so the server
     can recover the stack instead of the show silently hanging. */
  clearTimeout(neverStartTimer);
  neverStartTimer = setTimeout(() => {
    /* 'playing' can fire while the media clock never advances — Chromium
       reports the pipeline ready but a dead audio output pins
       currentTime at 0. Progress (timeupdate) is the real heartbeat. */
    if (currentItemId && !video.ended && video.currentTime < 0.05)
      socket.emit("player:stuck", { item: currentItemId });
  }, 10000);
}
let lyricTracks = null;   // {P1:[{start_ms,end_ms,text}], P2:[...]} from chart
let noteLanes = null;     // {P1:[{start_ms,end_ms,tone,kind}]}
let liveScores = {};      // {track: {score, streak, ...}}
let sungTrails = {};      // {track: recent sung tones {ms, tone, hit}}
let micLive = false;
let trackColors = { P1: "#ff5a5a", P2: "#4da3ff" };
let trackNames = { P1: "P1", P2: "P2" };
let laneMeta = null;      // {beat_ms, gap_ms, offset_ms, warp:[[beat,off]]}
let hitMarks = {};        // {track: {noteKey: litThroughMs}} — notes already sung
let sparks = [];          // hit particles {x_ms, tone, track, born}
let burstDone = new Set(); // noteKeys whose completion ring already fired
let chipMap = {};          // track -> persistent score chip elements
let prevStreak = {};       // track -> streak last frame (milestone detect)
let scoreTween = 0;        // rAF id while score digits are animating
let currentItemId = null;   // ignore full re-pushes for the same song
let waitingEntryId = null;  // entry on the "Preparing…" screen — stage updates target it

function show(id) {
  $("#idle").classList.toggle("hidden", id !== "idle");
  $("#player").classList.toggle("hidden", id !== "player");
}

function renderQueue(state) {
  const list = $("#tv-queue-list");
  list.innerHTML = "";
  $("#tv-queue-empty").classList.toggle("hidden", state.queue.length > 0);
  state.queue.slice(0, 8).forEach((it, i) => {
    const li = document.createElement("li");
    li.innerHTML =
      `<span class="pos">${i + 1}</span>` +
      `<span class="meta"><b>${it.title}</b> — ${it.artist}<br>` +
      singerChip(it.singer, it.singer_color) +
      (it.singer2 ? " &amp; " + singerChip(it.singer2, it.singer2_color) : "") +
      (it.entry_status === "downloading" ? ' <span class="badge video-only">downloading…</span>' : "") +
      `</span>`;
    list.appendChild(li);
  });
}

function startClock() {
  stopClock();
  let lastPos = -1, lastAdvance = Date.now(), stallReported = false;
  clockTimer = setInterval(() => {
    if (!video.paused && !video.seeking) {
      if (Math.abs(video.currentTime - lastPos) > 0.05) {
        lastPos = video.currentTime;
        lastAdvance = Date.now();
        stallReported = false;
      } else if (video.src && !stallReported &&
                 Date.now() - lastAdvance > 10000) {
        // clock frozen for 10s — decoder/network stall, end never fires
        stallReported = true;
        socket.emit("player:error", { reason: "stalled" });
      }
      socket.emit("player:position", { position_sec: video.currentTime });
      renderLyrics(video.currentTime * 1000);
      renderLane(video.currentTime * 1000);
      if (video.duration) {
        $("#song-progress-fill").style.width =
          ((video.currentTime / video.duration) * 100).toFixed(2) + "%";
      }
    }
  }, 50);
}

/* chart payload -> lane state. Kept in one place so now_playing,
   mid-song lyric re-pushes, and host sync nudges all stay consistent. */
function setLaneData(lyr) {
  lyricTracks = lyr ? lyr.tracks : null;
  noteLanes = lyr ? lyr.lanes : null;
  laneMeta = lyr ? {
    beat_ms: lyr.beat_ms, gap_ms: lyr.gap_ms || 0,
    offset_ms: lyr.offset_ms || 0, warp: lyr.warp || [],
  } : null;
  hitMarks = {};
  sungTrails = {};
  burstDone = new Set();
  chipMap = {}; prevStreak = {};
  $("#score-strip").innerHTML = "";
  resetLyrics();
}

/* Show the active lyric line per track against the video clock.
   Charted lines get per-syllable spans filled karaoke-style via --f. */
let lyricDom = {};   // {track: {p, key, syls:[{el,s,e}]}}

function resetLyrics() {
  lyricDom = {};
  $("#lyric-lines").innerHTML = "";
}

function renderLyrics(tMs) {
  const box = $("#lyric-lines");
  if (!lyricTracks) { resetLyrics(); return; }
  Object.keys(lyricTracks).sort().forEach((track) => {
    const lines = lyricTracks[track];
    const cur = lines.find((l) => l.start_ms <= tMs && tMs < l.end_ms);
    const next = lines.find((l) => l.start_ms > tMs);
    const slot = lyricDom[track] = lyricDom[track] || {};
    if (!slot.p) {
      slot.p = document.createElement("p");
      slot.p.className = "lyric-line";
      slot.p.style.color = trackColors[track] || "#fff";
      box.appendChild(slot.p);
      slot.n = document.createElement("p");
      slot.n.className = "lyric-next";
      slot.n.style.color = trackColors[track] || "#fff";
      box.appendChild(slot.n);
      slot.key = null; slot.syls = null; slot.nkey = null;
    }
    // upcoming line preview — singers read ahead during gaps too
    const nkey = next ? next.start_ms : null;
    if (slot.nkey !== nkey) {
      slot.nkey = nkey;
      slot.n.textContent = next ? next.text : "";
    }
    slot.n.style.opacity = next ? "1" : "0";
    if (!cur) {
      const key = next ? "wait" : "done";
      if (slot.key !== key) {
        slot.key = key;
        slot.syls = null;
        slot.p.textContent = next ? "♪" : "";
        slot.p.style.opacity = next ? "1" : "0";
      }
      return;
    }
    slot.p.style.opacity = "1";
    if (slot.key !== cur.start_ms) {   // line changed — rebuild spans
      slot.key = cur.start_ms;
      slot.p.textContent = "";
      slot.syls = null;
      if (cur.syls && cur.syls.length) {
        slot.syls = cur.syls.map((s, i) => {
          if (i && s.wb) slot.p.appendChild(document.createTextNode(" "));
          const el = document.createElement("span");
          el.className = "syl";
          el.textContent = s.t;
          slot.p.appendChild(el);
          return { el, s: s.start_ms, e: s.end_ms };
        });
      } else {
        slot.p.textContent = cur.text;   // no syllable data in payload
      }
    }
    if (slot.syls) {
      const c = trackColors[track] || "#fff";
      slot.syls.forEach((s) => {
        const f = Math.max(0, Math.min(1,
          (tMs - s.s) / Math.max(1, s.e - s.s)));
        s.el.style.setProperty("--f", f.toFixed(3));
        s.el.style.setProperty("--c", c);
      });
    }
  });
}
function stopClock() {
  if (clockTimer) clearInterval(clockTimer);
  clockTimer = null;
}

/* ---------- pitch lane + live score (Phase 3) ---------- */

const RATINGS = ["", "cool", "good", "great", "excellent", "superb",
                 "amazing", "sensational", "perfect"];

function rr(ctx, x, y, w, h, r) {
  /* rounded-rect path; r clamps so tiny notes stay pill-shaped */
  r = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  if (ctx.roundRect) { ctx.roundRect(x, y, w, h, r); return; }
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function shade(hex, f) {
  /* lighten (f>1) / darken (f<1) a #rrggbb color */
  const m = /^#?([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  const c = (v) => Math.max(0, Math.min(255, Math.round(v * f)));
  return `rgb(${c(n >> 16)},${c((n >> 8) & 255)},${c(n & 255)})`;
}

/* chart beat -> media ms under the learned warp map (mirrors
   catalog.warp_at: a mark's offset applies from its beat onward) */
function beatMediaMs(beat) {
  if (!laneMeta) return beat;
  let off = laneMeta.gap_ms + laneMeta.offset_ms;
  for (const [wb, wo] of laneMeta.warp) {
    if (wb <= beat) off = laneMeta.gap_ms + wo; else break;
  }
  return off + beat * laneMeta.beat_ms;
}

const noteKey = (n) => `${n.start_ms | 0}:${n.tone}`;

function renderLane(tMs) {
  if (!noteLanes) { lane.classList.add("hidden"); return; }
  lane.classList.remove("hidden");
  const dpr = window.devicePixelRatio || 1;
  const W = lane.clientWidth, H = lane.clientHeight;
  if (!W) return;
  if (lane.width !== Math.round(W * dpr)) {
    lane.width = Math.round(W * dpr); lane.height = Math.round(H * dpr);
  }
  const ctx = lane.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const tracks = Object.keys(noteLanes).sort();
  const t0 = tMs - LANE_PAD_MS, t1 = tMs + LANE_WINDOW_MS;
  const x = (ms) => ((ms - t0) / (t1 - t0)) * W;
  const nowX = x(tMs);

  tracks.forEach((track, ti) => {
    const bandH = H / tracks.length;
    const y0 = ti * bandH;
    const color = trackColors[track] || "#fff";

    // lane backdrop — dark glass band with a colored edge so the two
    // duet lanes read as separate player zones
    const bg = ctx.createLinearGradient(0, y0, 0, y0 + bandH);
    bg.addColorStop(0, "rgba(10,14,28,.72)");
    bg.addColorStop(1, "rgba(6,9,20,.78)");
    ctx.fillStyle = bg;
    rr(ctx, 6, y0 + 2, W - 12, bandH - 4, 12);
    ctx.fill();
    ctx.strokeStyle = color + "30";
    ctx.lineWidth = 1;
    rr(ctx, 6.5, y0 + 2.5, W - 13, bandH - 5, 11);
    ctx.stroke();

    const vis = noteLanes[track]
      .filter((n) => n.end_ms > t0 && n.start_ms < t1);
    if (!vis.length) return;
    let lo = Math.min(...vis.map((n) => n.tone));
    let hi = Math.max(...vis.map((n) => n.tone));
    lo -= 2; hi += 2;                       // breathing room
    const rowH = Math.max(6, (bandH - 16) / (hi - lo));
    const y = (tone) =>
      y0 + bandH - 8 - ((tone - lo) / (hi - lo)) * (bandH - 16) - rowH;

    // phrase backgrounds — alternate tint per lyric line so the grid
    // reads as musical phrases, not a continuous ribbon
    const lines = (lyricTracks && lyricTracks[track]) || [];
    lines.forEach((ln, i) => {
      if (ln.end_ms < t0 || ln.start_ms > t1) return;
      const lx = Math.max(6, x(ln.start_ms));
      const lw = Math.min(x(ln.end_ms), W - 6) - lx;
      ctx.fillStyle = i % 2 ? "#ffffff07" : "#ffffff02";
      rr(ctx, lx, y0 + 4, Math.max(0, lw), bandH - 8, 6);
      ctx.fill();
    });

    // beat grid — a thin tick every beat, a brighter bar line every 4th.
    // Warp segments are walked so grid lines stay glued to the notes
    // even across learned gap edits.
    if (laneMeta) {
      const segs = [[-1e9, laneMeta.offset_ms]]
        .concat(laneMeta.warp.map(([b, o]) => [b, o]));
      ctx.lineWidth = 1;
      for (let s = 0; s < segs.length; s++) {
        const segFrom = segs[s][0];
        const segTo = s + 1 < segs.length ? segs[s + 1][0] : 1e9;
        const off = laneMeta.gap_ms + segs[s][1];
        // first/last beat whose media time lands in the window
        const bFrom = Math.max(segFrom,
          Math.ceil((t0 - off) / laneMeta.beat_ms));
        const bTo = Math.min(segTo,
          Math.floor((t1 - off) / laneMeta.beat_ms));
        for (let b = bFrom; b <= bTo; b++) {
          const gx = x(off + b * laneMeta.beat_ms);
          if (gx < 6 || gx > W - 6) continue;
          const bar = ((b % 4) + 4) % 4 === 0;
          ctx.strokeStyle = bar ? "#ffffff2a" : "#ffffff0e";
          ctx.beginPath();
          ctx.moveTo(gx, y0 + 6); ctx.lineTo(gx, y0 + bandH - 6);
          ctx.stroke();
        }
      }
    }

    // semitone rows — octave lines a touch brighter
    ctx.lineWidth = 1;
    for (let tone = lo; tone <= hi; tone++) {
      ctx.strokeStyle = tone % 12 === 0 ? "#ffffff20" : "#ffffff0a";
      const gy = y(tone) + rowH / 2;
      ctx.beginPath(); ctx.moveTo(6, gy); ctx.lineTo(W - 6, gy); ctx.stroke();
    }

    const trail = (sungTrails[track] =
      (sungTrails[track] || []).filter((p) => p.ms > t0));
    const marks = hitMarks[track] = hitMarks[track] || {};
    // a trail point inside a note's span (within ~1.5 semitones) lights
    // that note through to the point's time
    vis.forEach((n) => {
      if (n.kind === "freestyle") return;
      const k = noteKey(n);
      let lit = marks[k] || 0;
      for (const p of trail) {
        if (p.ms < n.start_ms || p.ms > n.end_ms) break;
        if (p.hit && Math.abs(p.tone - n.tone) <= 1.5)
          lit = Math.max(lit, p.ms);
      }
      if (lit) marks[k] = lit;
    });

    // chart note pills — gradient + gloss; the sung portion fills white
    vis.forEach((n) => {
      const nx = Math.max(6, x(n.start_ms));
      const nw = Math.max(4, Math.min(x(n.end_ms), W - 6) - nx);
      const ny = y(n.tone);
      const golden = n.kind === "golden" || n.kind === "rap-golden";
      if (n.kind === "freestyle") {
        ctx.strokeStyle = color; ctx.globalAlpha = .5; ctx.lineWidth = 1.5;
        rr(ctx, nx, ny, nw, rowH, rowH / 2); ctx.stroke();
        ctx.globalAlpha = 1;
        return;
      }
      const g = ctx.createLinearGradient(0, ny, 0, ny + rowH);
      if (golden) {
        g.addColorStop(0, "#fff0b3"); g.addColorStop(.5, "#ffd24d");
        g.addColorStop(1, "#c98d1b");
        ctx.shadowColor = "#ffd24d"; ctx.shadowBlur = 14;
      } else {
        g.addColorStop(0, shade(color, 1.4)); g.addColorStop(.55, color);
        g.addColorStop(1, shade(color, .6));
      }
      ctx.fillStyle = g;
      rr(ctx, nx, ny, nw, rowH, rowH / 2); ctx.fill();
      ctx.shadowBlur = 0;
      ctx.fillStyle = "#ffffff2e";   // top gloss stripe
      rr(ctx, nx + 1, ny + 1, Math.max(0, nw - 2), rowH * .36, rowH / 4);
      ctx.fill();

      // hit fill — the sung portion glows white-hot from the left
      const lit = marks[noteKey(n)];
      if (lit) {
        const fx = Math.min(x(Math.min(lit, n.end_ms)), W - 6);
        const fw = Math.max(0, fx - nx);
        if (fw > 2) {
          const fg = ctx.createLinearGradient(0, ny, 0, ny + rowH);
          fg.addColorStop(0, "#ffffff");
          fg.addColorStop(.6, golden ? "#fff3c4" : "#ffe9b0");
          fg.addColorStop(1, golden ? "#ffd24d" : "#ffc95e");
          ctx.fillStyle = fg;
          ctx.shadowColor = "#ffd24d"; ctx.shadowBlur = 12;
          rr(ctx, nx, ny, fw, rowH, rowH / 2);
          ctx.fill();
          ctx.shadowBlur = 0;
        }
      }
      // note fully sung — one-shot expanding ring at its tail
      if (lit >= n.end_ms && !burstDone.has(noteKey(n))) {
        burstDone.add(noteKey(n));
        sparks.push({ ring: 1, x: Math.min(x(n.end_ms), W - 6),
                      y: ny + rowH / 2, col: golden ? "#ffd24d" : color,
                      born: performance.now() });
      }
    });

    // sung-tone trail — soft wide underlay + bright core, joined
    ctx.lineCap = "round"; ctx.lineJoin = "round";
    for (let pass = 0; pass < 2; pass++) {
      for (let i = 1; i < trail.length; i++) {
        const a = trail[i - 1], b = trail[i];
        if (b.ms - a.ms > 260) continue;    // voice break — don't connect
        const fade = (b.ms - t0) / (t1 - t0);
        const good = b.hit;
        ctx.strokeStyle = good ? color : "#ff5a5a";
        if (pass === 0) {   // glow underlay
          ctx.globalAlpha = .12 + .28 * Math.max(0, fade);
          ctx.lineWidth = Math.max(6, rowH * .9);
          ctx.shadowColor = ctx.strokeStyle; ctx.shadowBlur = 12;
        } else {            // crisp core
          ctx.globalAlpha = .35 + .65 * Math.max(0, fade);
          ctx.lineWidth = Math.max(3, rowH * .4);
          ctx.shadowBlur = 0;
        }
        ctx.beginPath();
        ctx.moveTo(x(a.ms), y(a.tone)); ctx.lineTo(x(b.ms), y(b.tone));
        ctx.stroke();
      }
    }
    ctx.shadowBlur = 0; ctx.globalAlpha = 1;

    // hit sparks at the cursor while the sung tone lands on a note
    const last = trail[trail.length - 1];
    if (last && last.ms > tMs - 300 && last.hit) {
      const sx = x(last.ms), sy = y(last.tone);
      for (let i = 0; i < 2; i++) {
        sparks.push({ x: sx + (Math.random() - .5) * 10,
                      y: sy + (Math.random() - .5) * rowH,
                      vx: 20 + Math.random() * 60, vy: -15 - Math.random() * 45,
                      r: 1.5 + Math.random() * 2.5, born: performance.now(),
                      col: color });
      }
      if (sparks.length > 140) sparks.splice(0, sparks.length - 140);
    }
    if (last) {   // current-pitch head — ring + core, bigger on hit
      const hx = x(last.ms), hy = y(last.tone);
      const hc = last.hit ? color : "#ff5a5a";
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 2;
      ctx.shadowColor = hc; ctx.shadowBlur = 16;
      ctx.beginPath();
      ctx.arc(hx, hy, last.hit ? 8 : 6, 0, Math.PI * 2);
      ctx.stroke();
      ctx.fillStyle = "#fff";
      ctx.beginPath();
      ctx.arc(hx, hy, last.hit ? 4 : 3.2, 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;
    }
  });

  // sparks drift up-right and fade — game juice, cheap to draw
  const nowPerf = performance.now();
  sparks = sparks.filter((p) => nowPerf - p.born < (p.ring ? 520 : 420));
  for (const p of sparks) {
    if (p.ring) {          // note-complete ring — expands and fades
      const age = (nowPerf - p.born) / 520;
      ctx.globalAlpha = (1 - age) * .85;
      ctx.strokeStyle = p.col;
      ctx.lineWidth = Math.max(1, 4 * (1 - age));
      ctx.shadowColor = p.col; ctx.shadowBlur = 10;
      ctx.beginPath();
      ctx.arc(p.x, p.y, 8 + age * 46, 0, Math.PI * 2);
      ctx.stroke();
      continue;
    }
    const age = (nowPerf - p.born) / 420;
    ctx.globalAlpha = (1 - age) * .9;
    ctx.fillStyle = age < .5 ? "#fff" : p.col;
    ctx.shadowColor = p.col; ctx.shadowBlur = 6;
    ctx.beginPath();
    ctx.arc(p.x + p.vx * age * .42, p.y + p.vy * age * .42,
            p.r * (1 - age * .5), 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.shadowBlur = 0; ctx.globalAlpha = 1;

  // "now" target column — soft glow bar with a bright core line
  const cg = ctx.createLinearGradient(nowX - 14, 0, nowX + 14, 0);
  cg.addColorStop(0, "#ffffff00"); cg.addColorStop(.5, "#ffffff14");
  cg.addColorStop(1, "#ffffff00");
  ctx.fillStyle = cg;
  ctx.fillRect(nowX - 14, 0, 28, H);
  ctx.strokeStyle = "#ffffffcc"; ctx.lineWidth = 2;
  ctx.shadowColor = "#fff"; ctx.shadowBlur = 10;
  ctx.beginPath(); ctx.moveTo(nowX, 0); ctx.lineTo(nowX, H); ctx.stroke();
  ctx.shadowBlur = 0; ctx.lineWidth = 1;
}

/* "+N" point floater rising off a score chip — feels like points land */
function floatPts(chip, d) {
  const f = document.createElement("span");
  f.className = "pts-float";
  f.textContent = `+${d}`;
  chip.appendChild(f);
  setTimeout(() => f.remove(), 1400);
}

/* digits ease toward the new total instead of snapping */
function tickScores() {
  let running = false;
  for (const t in chipMap) {
    const c = chipMap[t];
    if (c.shown !== c.target) {
      c.shown += (c.target - c.shown) * .22;
      if (Math.abs(c.target - c.shown) < .6) c.shown = c.target;
      c.ptsEl.textContent = String(Math.round(c.shown)).padStart(4, "0");
      if (c.shown !== c.target) running = true;
    }
  }
  scoreTween = running ? requestAnimationFrame(tickScores) : 0;
}

/* big streak milestones get their own center-stage pop */
function milestonePop(text) {
  const el = document.createElement("div");
  el.className = "rating-pop r6 streak-pop";
  el.textContent = text;
  $("#player").appendChild(el);
  setTimeout(() => el.remove(), 2200);
}

function renderScores() {
  const strip = $("#score-strip");
  for (const track of Object.keys(chipMap)) {
    if (liveScores[track]) continue;
    chipMap[track].chip.remove(); chipMap[track].badge.remove();
    delete chipMap[track];
  }
  Object.keys(liveScores).forEach((track) => {
    const p = liveScores[track];
    const c = trackColors[track] || "#fff";
    let e = chipMap[track];
    if (!e) {
      const chip = document.createElement("span");
      chip.className = "score-chip";
      chip.style.borderColor = c;
      chip.innerHTML =
        `<span class="sc-dot" style="background:${c}"></span>` +
        `<span class="sc-name">${trackNames[track] || track}</span>` +
        `<span class="sc-pts" style="color:${c}">0000</span>`;
      strip.appendChild(chip);
      const badge = document.createElement("span");
      badge.className = "streak-badge hidden";
      strip.appendChild(badge);
      e = chipMap[track] = { chip, badge, shown: 0, target: 0,
                             ptsEl: chip.querySelector(".sc-pts") };
    }
    const delta = p.score - e.target;
    e.target = p.score;
    if (delta >= 40) floatPts(e.chip, delta);
    if (!scoreTween && e.shown !== e.target)
      scoreTween = requestAnimationFrame(tickScores);

    const st = p.streak || 0;
    const prev = prevStreak[track] || 0;
    prevStreak[track] = st;
    e.badge.classList.toggle("hidden", st < 3);
    if (st >= 3) {
      e.badge.textContent = `×${st}`;
      e.badge.classList.toggle("hot", st >= 10);
    }
    for (const m of [5, 10, 25, 50, 100])
      if (prev < m && st >= m) milestonePop(`×${m} STREAK`);
  });
  let warn = strip.querySelector(".video-only");
  if (!micLive && !warn) {
    warn = document.createElement("span");
    warn.className = "badge video-only";
    warn.textContent = "no mic";
    strip.appendChild(warn);
  } else if (micLive && warn) warn.remove();
}

socket.on("sing:update", (u) => {
  liveScores = u.players || {};
  micLive = u.mic;
  const tones = u.tones || {};
  for (const track in tones) {
    if (tones[track] == null || u.ms == null) continue;
    const hit = u.hit && u.hit.hit;
    (sungTrails[track] = sungTrails[track] || []).push(
      { ms: u.ms, tone: tones[track], hit: !!hit });
  }
  renderScores();
});

socket.on("sing:line", (ev) => {
  // phrase rating popup — color tiers climb toward the gold perfect
  const el = document.createElement("div");
  const perfect = ev.rating >= RATINGS.length - 1;
  el.className = `rating-pop r${ev.rating}` + (perfect ? " perfect" : "");
  el.textContent = perfect ? "PERFECT LINE" : (RATINGS[ev.rating] || "");
  $("#player").appendChild(el);
  setTimeout(() => el.remove(), 2200);
  if (perfect) {   // full-screen gold bloom — the big payoff moment
    const fl = document.createElement("div");
    fl.className = "perfect-flash";
    $("#player").appendChild(fl);
    setTimeout(() => fl.remove(), 800);
  }
});

socket.on("sing:autosync", (res) => {
  if (!res.delta_ms) return;
  const how = res.method === "whistle" ? " (voice match)" : "";
  toast(`Lyrics auto-synced ${res.delta_ms > 0 ? "+" : ""}${res.delta_ms} ms${how}`);
});

/* results-screen confetti — one-shot particle burst on #fx */
function fxConfetti() {
  const cv = $("#fx");
  if (!cv) return;
  cv.classList.remove("hidden");
  const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
  cv.width = innerWidth * dpr; cv.height = innerHeight * dpr;
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const cols = ["#ffd24d", "#ff6ec7", "#5ad27e", "#7ea4ff",
                "#ff9d4d", "#ffffff"];
  const parts = [];
  for (let i = 0; i < 110; i++) {
    parts.push({
      x: innerWidth / 2 + (Math.random() - .5) * innerWidth * .34,
      y: innerHeight * .4,
      vx: (Math.random() - .5) * 900,
      vy: -280 - Math.random() * 720,
      w: 5 + Math.random() * 7, h: 3 + Math.random() * 5,
      rot: Math.random() * Math.PI, vr: (Math.random() - .5) * 10,
      col: cols[(Math.random() * cols.length) | 0],
    });
  }
  const t0 = performance.now();
  let last = t0;
  const draw = (now) => {
    const dt = Math.min(.05, (now - last) / 1000); last = now;
    ctx.clearRect(0, 0, innerWidth, innerHeight);
    for (const p of parts) {
      p.vy += 1400 * dt; p.vx *= (1 - 1.6 * dt);
      p.x += p.vx * dt; p.y += p.vy * dt;
      p.rot += p.vr * dt;
      ctx.save();
      ctx.translate(p.x, p.y); ctx.rotate(p.rot);
      ctx.globalAlpha = Math.max(0, 1 - (now - t0) / 3200);
      ctx.fillStyle = p.col;
      ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
      ctx.restore();
    }
    if (now - t0 < 3400 && !cv.classList.contains("hidden"))
      requestAnimationFrame(draw);
    else cv.classList.add("hidden");
  };
  requestAnimationFrame(draw);
}

socket.on("sing:final", (res) => {
  const box = $("#final-score");
  box.classList.remove("hidden");
  const tracks = Object.entries(res.tracks || {});
  const winner = tracks.length > 1
    ? tracks.reduce((a, b) => (b[1].score > a[1].score ? b : a))[0]
    : null;
  box.innerHTML = '<h2 class="fs-title">Results</h2><div class="fs-rows">' +
    tracks.map(([t, r]) => {
      const acc = r.notes ? r.hits / r.notes : 0;
      const grade = acc >= .9 ? "S" : acc >= .8 ? "A" :
                    acc >= .65 ? "B" : acc >= .45 ? "C" : "D";
      const win = t === winner;
      return `<div class="fs-row${win ? " win" : ""}">` +
        `<span class="fs-grade g${grade}">${grade}</span>` +
        `<span class="fs-name" style="color:${trackColors[t] || "#fff"}">` +
        `${trackNames[t] || t}` +
        (win ? '<i class="fs-winner">winner</i>' : "") + `</span>` +
        `<span class="fs-score" data-target="${r.score | 0}">0</span>` +
        `<span class="fs-sub">${r.hits}/${r.notes} hit · ` +
        `${r.perfect} perfect · best ×${r.max_streak || 0}</span>` +
        `</div>`;
    }).join("") + "</div>";
  // staggered count-up — winner counts last so it lands on the reveal
  const els = box.querySelectorAll(".fs-score");
  const t0 = performance.now();
  const step = (now) => {
    let running = false;
    els.forEach((el, i) => {
      const k = Math.min(1, Math.max(0, (now - t0 - i * 350) / 1500));
      el.textContent =
        Math.round((+el.dataset.target) * (1 - Math.pow(1 - k, 3)))
          .toLocaleString();
      if (k < 1) running = true;
    });
    if (running && !box.classList.contains("hidden"))
      requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
  fxConfetti();
  setTimeout(() => box.classList.add("hidden"), 14000);
});

function preSong(payload) {
  /* Pre-song handoff: who's up, mic handoff hint, scoring mode, countdown.
     The countdown runs to payload.play_at (mic-handoff timer) when set,
     so singers have time to pass the mic before the song starts. */
  const { item, entry, scoring } = payload;
  $("#pre-song-title").textContent = `${item.title} — ${item.artist}`;
  $("#pre-song-singers").innerHTML =
    singerChip(item.singer, item.singer_color) +
    (item.singer2 ? " &amp; " + singerChip(item.singer2, item.singer2_color) : "");
  $(".pre-label").textContent = item.singer2
    ? `Mics to ${item.singer} & ${item.singer2}!`
    : `Mic to ${item.singer}!`;
  const mode = $("#pre-song-mode");
  mode.className = "badge " + (scoring ? "scoring" : "video-only");
  mode.textContent = scoring
    ? ((entry.chart_duet || entry.is_duet ? "Duet — scored" : "Scored") +
       (payload.mic ? "" : " (no mic — watch only)"))
    : "Video only — no scoring";

  const pre = $("#pre-song");
  const band = $("#lyric-band");
  pre.classList.remove("hidden");
  band.classList.add("hidden");
  $("#score-strip").innerHTML = "";
  $("#lyric-lines").innerHTML = "";

  const endsAt = payload.play_at
    ? payload.play_at * 1000
    : Date.now() + PRE_COUNT_SECONDS * 1000;
  const count = $("#pre-count");
  const tick = () => {
    const n = Math.ceil((endsAt - Date.now()) / 1000);
    if (n <= 0) {
      clearInterval(countdownTimer);
      pre.classList.add("hidden");
      video.play().catch(() => {});
      armNeverStart();
      if (scoring) band.classList.remove("hidden");
      return;
    }
    count.textContent = n;
  };
  count.textContent = Math.ceil((endsAt - Date.now()) / 1000);
  clearInterval(countdownTimer);
  countdownTimer = setInterval(tick, 250);
}

/* ---- audio-only visuals ----
   MP3s (and audio-only mp4s) still play through <video> — it stays the
   clock. When loadedmetadata reports no video track we hide the element
   and draw a beat-reactive scene on a fullscreen canvas fed by a WebAudio
   analyser. createMediaElementSource may only be called once per element,
   so once wired the analyser stays connected for the session. */
const viz = $("#viz");
let vizCtx = null;             // lazy — canvas binds to whichever ctx is made first
let vizGL = null;              // null=untried, false=no WebGL, true=FractalViz up
let vizOn = false, vizRaf = 0, vizLastT = 0;
let audioCtx = null, analyser = null, freqData = null, waveData = null;
let vizHue = 0, vizPulse = 0, vizPhase = 0;

function analyserReady() {
  if (analyser) return true;
  try {
    audioCtx = audioCtx ||
      new (window.AudioContext || window.webkitAudioContext)();
    const src = audioCtx.createMediaElementSource(video);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.78;
    src.connect(analyser);
    analyser.connect(audioCtx.destination);
    freqData = new Uint8Array(analyser.frequencyBinCount);
    waveData = new Uint8Array(analyser.fftSize);
    return true;
  } catch (e) { return false; }
}

function setVisuals(on) {
  if (on === vizOn) return;
  if (on) {
    if (!analyserReady()) return;
    audioCtx.resume();
    // unhide BEFORE measuring — a display:none element reports a 0x0
    // rect, which would leave us drawing a 2x2 canvas stretched over
    // the whole screen
    viz.classList.remove("hidden");
    video.classList.add("audio-only");
    const r = viz.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    viz.width = Math.max(2, Math.round(r.width * dpr));
    viz.height = Math.max(2, Math.round(r.height * dpr));
    if (vizGL === null) {
      try { vizGL = !!FractalViz.init(viz); }
      catch (e) { vizGL = false; }
    }
    if (!vizGL) {
      // no WebGL — fall back to the 2D canvas scene
      vizCtx = vizCtx || viz.getContext("2d");
      vizCtx.fillStyle = "#04040c";
      vizCtx.fillRect(0, 0, viz.width, viz.height);
    }
    vizOn = true;
    vizLastT = performance.now();
    vizRaf = requestAnimationFrame(drawViz);
  } else {
    vizOn = false;
    cancelAnimationFrame(vizRaf);
    viz.classList.add("hidden");
    video.classList.remove("audio-only");
  }
}

function vizBands() {
  analyser.getByteFrequencyData(freqData);
  analyser.getByteTimeDomainData(waveData);
  const avg = (a, b) => {
    let s = 0;
    for (let i = a; i < b; i++) s += freqData[i];
    return s / ((b - a) * 255);
  };
  return { bass: avg(2, 24), mid: avg(24, 90), tre: avg(90, 200) };
}

function drawViz() {
  vizRaf = requestAnimationFrame(drawViz);
  const now = performance.now();
  const dt = Math.min(0.1, Math.max(0.001, (now - vizLastT) / 1000));
  vizLastT = now;
  const bands = vizBands();
  if (vizGL) {
    // false = context lost mid-run — drop to the 2D scene for good
    if (FractalViz.render(bands, dt)) return;
    vizGL = false;
  }
  drawViz2D(bands);
}

function drawViz2D(bands) {
  const c = vizCtx;
  if (!c) return;
  const W = viz.width, H = viz.height, cx = W / 2, cy = H / 2;
  const bass = bands.bass;
  vizPulse = vizPulse * 0.82 + bass * 0.18;
  vizHue = (vizHue + 0.25 + vizPulse * 1.6) % 360;
  vizPhase += 0.004 + vizPulse * 0.05;
  // motion trails — translucent fill instead of a clear
  c.fillStyle = "rgba(4,4,12,0.30)";
  c.fillRect(0, 0, W, H);
  c.globalCompositeOperation = "lighter";
  const TAU = Math.PI * 2;
  const baseR = Math.min(W, H) * 0.15 * (1 + vizPulse * 0.45);
  // radial spectrum spokes
  const N = 96, usable = Math.floor(freqData.length * 0.72);
  for (let i = 0; i < N; i++) {
    const v = freqData[Math.floor((i / N) * usable)] / 255;
    const ang = vizPhase + (i / N) * TAU;
    const len = 2 + v * v * Math.min(W, H) * 0.30;
    c.strokeStyle =
      `hsla(${(vizHue + i * (240 / N)) % 360},90%,${42 + v * 30}%,${0.25 + v * 0.6})`;
    c.lineWidth = 1.5 + v * 7;
    c.beginPath();
    c.moveTo(cx + Math.cos(ang) * baseR, cy + Math.sin(ang) * baseR);
    c.lineTo(cx + Math.cos(ang) * (baseR + len),
             cy + Math.sin(ang) * (baseR + len));
    c.stroke();
  }
  // two counter-rotating waveform rings
  for (let ring = 0; ring < 2; ring++) {
    const dir = ring ? -1 : 1, R = baseR * (1.6 + ring * 0.55);
    c.strokeStyle = `hsla(${(vizHue + ring * 120) % 360},85%,60%,0.5)`;
    c.lineWidth = 2;
    c.beginPath();
    const M = 160, step = Math.max(1, Math.floor(waveData.length / M));
    for (let i = 0; i <= M; i++) {
      const s = (waveData[(i * step) % waveData.length] - 128) / 128;
      const ang = dir * vizPhase * 1.7 + (i / M) * TAU;
      const r = R + s * Math.min(W, H) * 0.09;
      const x = cx + Math.cos(ang) * r, y = cy + Math.sin(ang) * r;
      i ? c.lineTo(x, y) : c.moveTo(x, y);
    }
    c.stroke();
  }
  // breathing core
  const core = c.createRadialGradient(cx, cy, 0, cx, cy, baseR);
  core.addColorStop(0, `hsla(${vizHue},90%,62%,${0.35 + vizPulse * 0.5})`);
  core.addColorStop(1, "hsla(0,0%,0%,0)");
  c.fillStyle = core;
  c.beginPath();
  c.arc(cx, cy, baseR, 0, TAU);
  c.fill();
  c.globalCompositeOperation = "source-over";
}

let vizWanted = false;   // server says the file has no real video track
video.addEventListener("loadedmetadata", () => {
  // mp3s with embedded cover art report a video stream — trust the
  // server's stream probe over the element's metadata
  setVisuals(vizWanted ||
             (video.videoWidth === 0 && video.videoHeight === 0));
});
window.addEventListener("resize", () => {
  if (vizOn) { setVisuals(false); setVisuals(true); }
});

socket.on("now_playing", (payload) => {
  stopClock();
  clearInterval(countdownTimer);
  clearTimeout(neverStartTimer);
  video.pause();
  video.removeAttribute("src");
  vizWanted = false;
  setVisuals(false);

  if (!payload) {
    currentItemId = null;
    waitingEntryId = null;
    show("idle");
    setLaneData(null);
    sungTrails = {}; liveScores = {}; sparks = [];
    lane.classList.add("hidden");
    $("#final-score").classList.add("hidden");
    return;
  }
  if (payload.item.id === currentItemId) {
    // same song re-pushed (e.g. a chart was fetched mid-play): hot-swap
    // overlay data only — touching video.src would restart playback.
    // Checked before the playback-less wait states so a mid-song
    // "processing" push can't pause a song already playing.
    if (payload.lyrics) setLaneData(payload.lyrics);
    micLive = micLive || !!payload.mic;
    return;
  }
  if (!payload.playback) {
    if (payload.waiting === "download" || payload.waiting === "processing") {
      // media is still being fetched, or its lyric chart is still being
      // generated — hold the player on a waiting screen; the server
      // re-pushes now_playing when the artifact lands. currentItemId
      // stays unset so the real push still takes the fresh-play path.
      show("player");
      $("#pre-song-title").textContent = `${payload.item.title} — ${payload.item.artist}`;
      $("#pre-song-singers").innerHTML = singerChip(payload.item.singer, payload.item.singer_color);
      const mode = $("#pre-song-mode");
      mode.className = "badge video-only";
      mode.textContent = payload.waiting === "download"
        ? "Downloading video…" : "Preparing song…";
      waitingEntryId = payload.item.entry_id;
      $("#pre-stage").textContent = payload.stage || "";
      $("#pre-count").textContent = "";
      $("#pre-song").classList.remove("hidden");
      return;
    }
    show("idle");
    toast(`"${payload.item.title}" has no playable media — skipped`, "error");
    socket.emit("player:error", { reason: "unavailable" });
    return;
  }
  currentItemId = payload.item.id;
  waitingEntryId = null;

  show("player");
  video.volume = payload.music_volume ?? 1;
  // charted song -> timed lyrics + pitch lanes keyed to the video clock
  setLaneData(payload.lyrics);
  sungTrails = {};
  sparks = [];
  liveScores = {};
  micLive = !!payload.mic;
  $("#final-score").classList.add("hidden");
  $("#song-progress-fill").style.width = "0%";
  trackColors = {
    P1: payload.item.singer_color || "#ff5a5a",
    P2: payload.item.singer2_color || "#4da3ff",
  };
  trackNames = {
    P1: payload.item.singer || "P1",
    P2: payload.item.singer2 || "P2",
  };
  vizWanted = !!payload.playback.audio_only;
  video.src = payload.playback.uri;
  video.load();
  preSong(payload);
  startClock();
});

socket.on("state", renderQueue);

// worker progress while the player is held on "Preparing song…" —
// turns a silent hold into a readable stage list
socket.on("pipeline:stage", (ev) => {
  if (ev && ev.entry_id === waitingEntryId && ev.label)
    $("#pre-stage").textContent = ev.label + "…";
});

socket.on("lyrics", (lyr) => {
  // host nudged chart sync mid-song — swap in retimed lines/lanes without
  // touching the video; the next clock tick renders with the new offset.
  // hitMarks reset because note keys are timing-derived.
  if (lyr) setLaneData(lyr);
});

socket.on("item:update", (it) => {
  // e.g. someone claimed the open P2 slot while the pre-song timer runs
  if ($("#pre-song").classList.contains("hidden")) return;
  $("#pre-song-singers").innerHTML =
    singerChip(it.singer, it.singer_color) +
    (it.singer2 ? " &amp; " + singerChip(it.singer2, it.singer2_color) : "");
  $(".pre-label").textContent = it.singer2
    ? `Mics to ${it.singer} & ${it.singer2}!`
    : `Mic to ${it.singer}!`;
});

socket.on("session:ended", () => {
  stopClock();
  clearInterval(countdownTimer);
  video.pause();
  video.removeAttribute("src");
  setLaneData(null);
  sungTrails = {}; liveScores = {}; sparks = [];
  lane.classList.add("hidden");
  $("#final-score").classList.add("hidden");
  show("idle");
  toast("Session ended — new singers can join now");
});

/* host transport controls (phone host) */
socket.on("player:pause", () => {
  video.pause();
  clearTimeout(neverStartTimer);       // intentional pause — not stuck
});
socket.on("player:resume", () => video.play().catch(() => {}));
// server recovered the audio path — try again without a page reload
socket.on("player:retry", () => {
  video.load();
  video.play().catch(() => {});
  armNeverStart();
});
video.addEventListener("timeupdate", () => {
  if (video.currentTime > 0.05) clearTimeout(neverStartTimer);
});
// host/singer music-volume slider — live without touching playback state
socket.on("player:volume", (v) => {
  video.volume = Math.max(0, Math.min(1, v));
});

video.addEventListener("ended", () => socket.emit("player:ended"));
video.addEventListener("error", () => {
  if (video.src) socket.emit("player:error", { src: video.src });
});

// re-announce readiness on every (re)connect so a service restart — e.g.
// mid-party deploys — re-pushes now_playing instead of leaving a stale screen
socket.on("connect", () => socket.emit("tv:ready"));

/* host code floats until a host claims the room, then it's gone */
socket.on("host:state", ({ claimed, key }) => {
  document.getElementById("host-badge").classList.toggle("hidden", !!claimed);
  // key is pushed live so a long-lived kiosk page never shows a stale code
  if (key) document.querySelectorAll(".host-code code, #host-badge code")
    .forEach((c) => { c.textContent = key; });
});

// if the socket can't recover for 20s the cleanest recovery is a fresh
// page — the service's watchdog also restarts us when even this can't run
let socketDownSince = null;
setInterval(() => {
  if (socket.connected) { socketDownSince = null; return; }
  socketDownSince = socketDownSince || Date.now();
  if (Date.now() - socketDownSince > 20000) location.reload();
}, 5000);

show("idle");
