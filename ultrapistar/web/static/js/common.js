/* Shared client helpers */
const socket = io();
const $ = (s) => document.querySelector(s);

function toast(msg, level = "info") {
  const el = $("#toast");
  if (!el) return;
  el.textContent = msg;
  el.className = "toast " + level;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 3500);
}
socket.on("toast", (d) => toast(d.message, d.level));
socket.on("connect", () => { const d = $("#conn-dot"); if (d) d.classList.add("on"); });
socket.on("disconnect", () => { const d = $("#conn-dot"); if (d) d.classList.remove("on"); });
// server restarted under newer code — reload to pick it up (socket.io
// reconnects without reloading, so the page would run stale JS forever)
socket.on("hello", (d) => {
  if (window.PAGE_V && d && d.v && d.v !== window.PAGE_V) location.reload();
});

function badges(entry) {
  const out = [];
  if (entry.scoring_available || entry.chart_verified) out.push('<span class="badge scoring">scorable</span>');
  else if (entry.possible_chart) out.push('<span class="badge video-only">chart unverified</span>');
  else out.push('<span class="badge video-only">video only</span>');
  if (entry.is_duet || entry.chart_duet) out.push('<span class="badge duet">duet</span>');
  return out.join(" ");
}

function singerChip(name, color) {
  return `<span class="singer-chip" style="background:${color || "#666"}">${name || "?"}</span>`;
}

async function api(path, method = "GET", body = null) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : null,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
