"use strict";
// friday chat: long-polls /api/chat, renders messages and live cards.

const $ = (id) => document.getElementById(id);
const thread = $("thread"), scroller = $("scroller"), input = $("input"), sendBtn = $("send");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const state = { messages: [], cards: {}, lastId: 0, sig: "", hasMore: false, loaded: false, unread: 0, cardAt: {} };

// ---- http ---------------------------------------------------------------

async function req(method, url, body) {
  const r = await fetch(url, {
    method, credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) { location.href = "/login?next=/chat"; throw new Error("login required"); }
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return r.status === 204 ? null : r.json().catch(() => null);
}

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 2600);
}

// ---- formatting ---------------------------------------------------------

const ICONS = {
  otp: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.7 12.3 9.3-9.3"/><path d="m17 6 3 3"/><path d="m14 9 2 2"/></svg>',
  approval: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10"/><path d="m9 12 2 2 4-4"/></svg>',
  handoff: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="14" rx="2"/><path d="M8 22h8"/><path d="M12 18v4"/></svg>',
  run: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5z"/><path d="M14 2v6h6"/><path d="M9 15l2 2 4-4"/></svg>',
};

const time = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
function dayLabel(iso) {
  const d = new Date(iso), today = new Date(), y = new Date(); y.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === y.toDateString()) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric", year: d.getFullYear() === today.getFullYear() ? undefined : "numeric" });
}
const mmss = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
function linkify(text) {
  return esc(text).replace(/\bhttps?:\/\/[^\s<]+[^\s<.,;:!?)]/g, (u) => `<a href="${u}" target="_blank" rel="noopener">${u.length > 60 ? u.slice(0, 57) + "…" : u}</a>`);
}

const STATUS = {
  pending: ["Waiting on you", "pending"], resolved: ["Code delivered", "good"], approved: ["Approved", "good"],
  denied: ["Denied", "bad"], expired: ["Expired", ""], cancelled: ["Cancelled", ""], done: ["Done", "good"],
  succeeded: ["Succeeded", "good"], failed: ["Failed", "bad"], blocked: ["Blocked", "bad"],
  needs_attention: ["Needs attention", "pending"], running: ["Running", "pending"], queued: ["Queued", ""],
};
function pill(status) {
  const [label, cls] = STATUS[status] || [String(status || "").replace(/_/g, " "), ""];
  return `<span class="status ${cls}">${esc(label)}</span>`;
}

// ---- cards ----------------------------------------------------------------

function countdown(ref) {
  const c = state.cards[ref];
  if (!c || c.status !== "pending" || !c.seconds_left) return "";
  const left = Math.max(0, c.seconds_left - Math.floor((Date.now() - (state.cardAt[ref] || Date.now())) / 1000));
  return `<span class="countdown" data-cd="${esc(ref)}">${left ? mmss(left) + " left" : "expiring"}</span>`;
}

function cardHTML(m) {
  const c = state.cards[m.ref];
  if (!c) return "";
  const acct = m.profile_id ? `<span class="account">${esc(m.profile_id)}</span>` : "";
  const head = (title, sub) => `<div class="card-head"><div class="card-icon">${ICONS[m.kind]}</div>
    <div class="card-title">${esc(title)}${sub ? `<div class="card-sub">${sub}</div>` : ""}</div>${pill(c.status)}</div>`;
  const live = c.status === "pending";

  if (m.kind === "otp") {
    const what = { sms: "SMS code", email: "Email code", totp: "Authenticator code", app: "App code" }[c.channel] || "One-time code";
    return head(what + " needed", acct) + `<div class="card-main">
      ${live ? `<form class="row" data-otp="${esc(m.ref)}">
        <input class="code" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="12" placeholder="••••••" aria-label="Code" required>
        <button class="btn primary" type="submit">Send code</button></form>
        <div class="row" style="margin-top:8px">${countdown(m.ref)}</div>`
        : `<p class="card-sub" style="margin:0">${c.status === "resolved" ? "The code went to the sign-in. It is not stored." : "This request is closed."}</p>`}
    </div>`;
  }
  if (m.kind === "approval") {
    return head("Approval needed", acct) + `<div class="card-main">
      <p class="action">${esc(c.action)}</p>
      ${c.url ? `<div class="url">${esc(c.url)}</div>` : ""}
      ${c.screenshot_file_id && live ? `<img class="shot" loading="lazy" src="/api/files/${encodeURIComponent(c.screenshot_file_id)}" alt="The page when approval was requested (sensitive fields masked)">` : ""}
      ${live ? `<div class="row">${countdown(m.ref)}<span class="grow"></span>
        <button class="btn" data-ap="${esc(m.ref)}" data-v="0">Deny</button>
        <button class="btn primary" data-ap="${esc(m.ref)}" data-v="1">Approve</button></div>`
        : `<p class="card-sub" style="margin:0">Only this one action was ${c.status === "approved" ? "allowed" : "affected"}.</p>`}
    </div>`;
  }
  if (m.kind === "handoff") {
    return head("Your turn in the browser", acct) + `<div class="card-main">
      <p>${esc(c.reason)}</p>
      ${live ? `<div class="row"><a class="btn primary" href="${esc(c.link)}" target="_blank" rel="noopener">${c.remote_view ? "Open remote view" : "Open instructions"}</a>
        <button class="btn" data-done="${esc(m.ref)}">I'm done</button></div>` : ""}
    </div>`;
  }
  if (m.kind === "run") {
    const title = { fetch_statement: "Statement fetch", login: "Sign-in" }[c.kind] || "Run";
    return head(title, acct) + `<div class="card-main">
      ${c.summary ? `<p>${esc(c.summary)}</p>` : ""}
      <a class="btn" href="${esc(c.link)}">View run</a></div>`;
  }
  return "";
}

// ---- rendering ----------------------------------------------------------------

function msgHTML(m, prev, next) {
  const same = (a, b) => a && b && a.sender === b.sender && new Date(b.created_at) - new Date(a.created_at) < 5 * 60e3 && !a.kind && dayLabel(a.created_at) === dayLabel(b.created_at);
  const first = !same(prev, m), last = !same(m, next) || !!m.kind;
  const day = !prev || dayLabel(prev.created_at) !== dayLabel(m.created_at) ? `<div class="day">${dayLabel(m.created_at)}</div>` : "";
  const cls = `msg ${m.sender}${first ? " first" : ""}${last ? " last" : ""}`;
  const meta = last ? `<div class="meta">${time(m.created_at)}${m.url && !m.kind ? ` · <a href="${esc(m.url)}">open</a>` : ""}</div>` : "";
  // Card messages repeat what the card says; show the card only.
  const text = m.kind && state.cards[m.ref] ? "" : `<div class="text">${linkify(m.text)}</div>`;
  const card = m.kind ? `<div class="card" data-card="${esc(m.ref)}" data-id="${m.id}">${cardHTML(m)}</div>` : "";
  const gutter = m.sender === "friday" ? `<div class="gutter">${first ? '<div class="mark sm">f</div>' : ""}</div>` : "";
  return `${day}<div class="${cls}" data-id="${m.id}">${gutter}<div class="body">${text}${card}${meta}</div></div>`;
}

function render() {
  const nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120;
  if (!state.messages.length) {
    thread.innerHTML = state.loaded ? `<div class="empty"><div class="mark">f</div><h2>All quiet</h2>
      <p>When a run needs a code, an approval or your hand in the browser, it shows up here.</p></div>` : "";
    return;
  }
  const ms = state.messages;
  const typed = {}, focused = document.activeElement?.closest?.("[data-otp]")?.dataset.otp;
  thread.querySelectorAll("[data-otp]").forEach((f) => { if (f.code.value) typed[f.dataset.otp] = f.code.value; });
  thread.innerHTML = (state.hasMore ? `<div class="older"><button class="pill-btn" id="older">Show earlier messages</button></div>` : "")
    + ms.map((m, i) => msgHTML(m, ms[i - 1], ms[i + 1])).join("");
  thread.querySelectorAll("[data-otp]").forEach((f) => {
    if (typed[f.dataset.otp]) f.code.value = typed[f.dataset.otp];
    if (f.dataset.otp === focused) f.code.focus({ preventScroll: true });
  });
  if (nearBottom || render.stick) stickToBottom();
  render.stick = false;
}

// Images load after layout; keep the newest message in view while they do.
function stickToBottom() {
  scroller.scrollTop = scroller.scrollHeight;
  thread.querySelectorAll("img").forEach((img) => {
    if (!img.complete) img.addEventListener("load", () => { scroller.scrollTop = scroller.scrollHeight; }, { once: true });
  });
}

// Update cards in place, without touching a code someone is typing.
function refreshCards() {
  for (const el of thread.querySelectorAll("[data-card]")) {
    const ref = el.dataset.card, m = state.messages.find((x) => String(x.id) === el.dataset.id);
    if (!m) continue;
    const key = JSON.stringify(state.cards[ref] && { ...state.cards[ref], seconds_left: 0 });
    if (el._key === key) continue;
    const typing = el.contains(document.activeElement) && state.cards[ref]?.status === "pending";
    el._key = key;
    if (!typing) el.innerHTML = cardHTML(m);
  }
}

function tick() {
  for (const el of thread.querySelectorAll("[data-cd]")) {
    const ref = el.dataset.cd, c = state.cards[ref];
    if (!c) continue;
    const left = Math.max(0, c.seconds_left - Math.floor((Date.now() - state.cardAt[ref]) / 1000));
    el.textContent = left ? mmss(left) + " left" : "expiring";
  }
}
setInterval(tick, 1000);

function mergeCards(cards) {
  const now = Date.now();
  for (const [ref, c] of Object.entries(cards || {})) { state.cards[ref] = c; state.cardAt[ref] = now; }
}

// ---- connection -------------------------------------------------------------------

function setConn(ok) {
  $("dot").className = "dot " + (ok ? "on" : "off");
  $("conn").textContent = ok ? "Online" : "Reconnecting…";
}

function announce(msgs) {
  const fromFriday = msgs.filter((m) => m.sender === "friday");
  if (!fromFriday.length || !document.hidden) return;
  state.unread += fromFriday.length;
  document.title = `(${state.unread}) friday`;
  if (window.Notification && Notification.permission === "granted" && localGet("notify") === "1") {
    const m = fromFriday[fromFriday.length - 1];
    try { new Notification("friday", { body: m.text.slice(0, 180), tag: "friday-" + m.id }); } catch {}
  }
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) { state.unread = 0; document.title = "friday"; } });

function absorb(data, { initial = false } = {}) {
  mergeCards(data.cards);
  state.sig = data.sig;
  const fresh = (data.messages || []).filter((m) => m.id > state.lastId);
  if (initial) { state.messages = data.messages; state.hasMore = data.has_more; }
  else if (fresh.length) state.messages.push(...fresh);
  state.lastId = Math.max(state.lastId, data.last_id || 0, ...state.messages.map((m) => m.id));
  if (initial || fresh.length) render();
  refreshCards();
  if (!initial) announce(fresh);
}

async function refresh() {
  try { absorb(await req("GET", `/api/chat?after=${state.lastId}`)); } catch {}
}

async function loop() {
  let delay = 1000;
  for (;;) {
    try {
      const data = await req("GET", `/api/chat?after=${state.lastId}&wait=25&sig=${encodeURIComponent(state.sig)}`);
      setConn(true);
      absorb(data);
      delay = 1000;
    } catch (e) {
      setConn(false);
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(delay * 2, 15000);
    }
  }
}

async function start() {
  try {
    const data = await req("GET", "/api/chat?limit=80");
    state.loaded = true;
    render.stick = true;
    absorb(data, { initial: true });
    setConn(true);
  } catch {
    state.loaded = true;
    setConn(false);
    render();
  }
  loop();
}

// ---- actions ----------------------------------------------------------------------

async function busy(btn, fn) {
  if (btn) btn.disabled = true;
  try { await fn(); } catch (e) { toast(e.message || "Something went wrong"); } finally { if (btn) btn.disabled = false; }
}

thread.addEventListener("submit", (e) => {
  const f = e.target.closest("[data-otp]");
  if (!f) return;
  e.preventDefault();
  const code = f.code.value.replace(/\s+/g, "");
  if (!code) return;
  busy(f.querySelector("button"), async () => {
    await req("POST", `/api/otp/${encodeURIComponent(f.dataset.otp)}`, { code });
    f.code.value = "";
    document.activeElement?.blur();
    toast("Code sent");
    await refresh();
  });
});

thread.addEventListener("click", (e) => {
  const ap = e.target.closest("[data-ap]"), done = e.target.closest("[data-done]"), shot = e.target.closest(".shot");
  if (e.target.id === "older") return loadOlder(e.target);
  if (ap) {
    const yes = ap.dataset.v === "1";
    busy(ap, async () => {
      await req("POST", `/api/approvals/${encodeURIComponent(ap.dataset.ap)}`, { approve: yes });
      toast(yes ? "Approved" : "Denied");
      await refresh();
    });
  } else if (done) {
    busy(done, async () => {
      await req("POST", `/handoff/${encodeURIComponent(done.dataset.done)}/done`);
      toast("Marked done");
      await refresh();
    });
  } else if (shot) {
    const lb = $("lightbox");
    lb.querySelector("img").src = shot.src;
    lb.classList.add("open");
  }
});
$("lightbox").onclick = () => $("lightbox").classList.remove("open");
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("lightbox").classList.remove("open"); });

async function loadOlder(btn) {
  const first = state.messages[0];
  if (!first) return;
  await busy(btn, async () => {
    const h = scroller.scrollHeight;
    const data = await req("GET", `/api/chat?before=${first.id}&limit=80`);
    mergeCards(data.cards);
    state.messages = [...data.messages, ...state.messages];
    state.hasMore = data.has_more;
    render();
    scroller.scrollTop = scroller.scrollHeight - h;
  });
}

// ---- composer -----------------------------------------------------------------------

function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
  sendBtn.disabled = !input.value.trim();
}
input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("composer").requestSubmit(); }
});

async function say(text) {
  text = text.trim();
  if (!text) return;
  input.value = "";
  autosize();
  render.stick = true;
  await busy(sendBtn, async () => {
    await req("POST", "/api/chat", { text });
    await refresh();
  });
  sendBtn.disabled = !input.value.trim();
}
$("composer").addEventListener("submit", (e) => { e.preventDefault(); say(input.value); });
document.querySelectorAll("[data-say]").forEach((b) => b.onclick = () => say(b.dataset.say));

// ---- header -------------------------------------------------------------------------

function localGet(k) { try { return localStorage.getItem("friday." + k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem("friday." + k, v); } catch {} }

function paintBell() {
  const on = window.Notification && Notification.permission === "granted" && localGet("notify") === "1";
  $("bell").classList.toggle("on", !!on);
  $("bell").title = on ? "Desktop notifications are on" : "Turn on desktop notifications";
}
$("bell").onclick = async () => {
  if (!window.Notification) return toast("This browser has no desktop notifications");
  if (localGet("notify") === "1" && Notification.permission === "granted") { localSet("notify", "0"); paintBell(); return toast("Desktop notifications off"); }
  const p = await Notification.requestPermission();
  if (p === "granted") { localSet("notify", "1"); toast("You'll be notified while this tab is in the background"); }
  else toast("Notifications are blocked for this site");
  paintBell();
};
paintBell();

$("more").onclick = (e) => { e.stopPropagation(); $("menu").classList.toggle("open"); };
document.addEventListener("click", () => $("menu").classList.remove("open"));
$("clear").onclick = () => busy(null, async () => {
  if (!confirm("Clear the whole conversation? Pending requests stay pending.")) return;
  await req("DELETE", "/api/chat");
  state.messages = []; state.lastId = 0; state.hasMore = false;
  render();
  await refresh();
});

start();
input.focus({ preventScroll: true });
