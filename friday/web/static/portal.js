// friday admin portal: plain JS, polls the JSON API. Everything shown is already redacted server-side.
"use strict";

const $view = document.getElementById("view");
const $nav = document.getElementById("nav");
const TABS = [
  ["accounts", "Accounts"], ["runs", "Runs"], ["approvals", "Approvals"], ["otp", "Codes"],
  ["handoffs", "Handoffs"], ["diagnosis", "Diagnosis"], ["data", "Statements"], ["secrets", "Secrets"],
  ["sites", "Sites"], ["audit", "Audit"],
];
let state = null;
let timer = null;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ago = (iso) => { if (!iso) return "-"; const s = (Date.now() - Date.parse(iso)) / 1000; return s < 60 ? `${s | 0}s ago` : s < 3600 ? `${(s / 60) | 0}m ago` : s < 86400 ? `${(s / 3600) | 0}h ago` : iso.slice(0, 10); };
const statusClass = (s) => ({ succeeded: "ok", logged_in: "ok", seeded: "ok", running: "warn", queued: "muted", needs_attention: "bad", failed: "bad", aborted: "muted" }[s] || "");

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts });
  if (r.status === 401) { location.href = "/login?next=/portal"; throw new Error("login"); }
  const body = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(body?.detail || body || r.status);
  return body;
}
const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data || {}) });

function renderNav() {
  const route = location.hash.slice(1).split("/")[0] || "accounts";
  const counts = state ? { approvals: state.approvals.length, otp: state.otp.length, handoffs: state.handoffs.length } : {};
  $nav.innerHTML = TABS.map(([id, label]) =>
    `<a href="#${id}" class="${route === id ? "on" : ""}">${label}${counts[id] ? `<span class="badge">${counts[id]}</span>` : ""}</a>`).join("");
  if (state) document.getElementById("meta").textContent = `friday ${state.version ? state.version.short : "?"}${state.version && state.version.built_at ? " (" + state.version.built_at.slice(0, 16).replace("T", " ") + ")" : ""} · ${state.model} · notify: ${state.notify.join(", ")} · Gmail ${state.gmail_connected ? "connected" : "not connected"}`;
}

async function refreshState() {
  try { state = await api("/api/state"); renderNav(); } catch (e) { /* retry next tick */ }
}

function card(title, inner, extra = "") { return `<div class="card"><div class="row"><h2 class="grow">${title}</h2>${extra}</div>${inner}</div>`; }
function empty(text) { return `<div class="empty">${esc(text)}</div>`; }
async function act(fn) { try { await fn(); await refreshState(); route(); } catch (e) { alert(e.message); } }

// ---- views ----------------------------------------------------------------

const views = {
  async accounts() {
    const rows = state.profiles.map((p) => `
      <tr><td><b>${esc(p.id)}</b><div class="muted">${esc(p.locale)} · ${esc(p.timezone || "host tz")}${p.proxy ? " · proxy " + esc(p.proxy) : ""}</div>
      <div class="muted">secrets: ${p.secrets.map(esc).join(", ") || "none"}</div></td>
      <td><span class="chip ${statusClass(p.status)}">${esc(p.status)}</span>${p.running ? ' <span class="chip warn">running</span>' : ""}</td>
      <td>${p.last_run ? `<a href="#runs/${p.last_run.id}">${esc(p.last_run.kind)}</a> <span class="${statusClass(p.last_run.status)}">${esc(p.last_run.status)}</span><div class="muted">${ago(p.last_run.created_at)}</div>` : '<span class="muted">never</span>'}</td>
      <td class="row">
        <button data-act="login" data-id="${esc(p.id)}">Log in</button>
        <button data-act="statement" data-id="${esc(p.id)}" class="primary">Fetch statement</button>
        <button data-act="seed" data-id="${esc(p.id)}">Manual login</button>
        <button data-act="wipe" data-id="${esc(p.id)}" class="danger">Revoke everything</button>
      </td></tr>`).join("");
    $view.innerHTML = card("Accounts", rows ? `<table><tr><th>Account</th><th>Session</th><th>Last run</th><th></th></tr>${rows}</table>` : empty("No profiles. Add a site and a profile on the Sites tab.")) +
      card("Fetch statement", `<div class="row">From <input type="date" id="st-from"> to <input type="date" id="st-to"> <span class="muted">Applies to the "Fetch statement" buttons above.</span></div>`);
    const now = new Date(), first = new Date(now.getFullYear(), now.getMonth() - 1, 1), last = new Date(now.getFullYear(), now.getMonth(), 0);
    const ymd = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; // local, not UTC
    document.getElementById("st-from").value = ymd(first);
    document.getElementById("st-to").value = ymd(last);
    $view.querySelectorAll("button[data-act]").forEach((b) => b.onclick = () => act(async () => {
      const id = b.dataset.id;
      if (b.dataset.act === "login") { const r = await post(`/api/profiles/${id}/runs`, { kind: "login" }); location.hash = `runs/${r.run_id}`; }
      if (b.dataset.act === "statement") {
        const r = await post(`/api/profiles/${id}/runs`, { kind: "fetch_statement", params: { start: document.getElementById("st-from").value, end: document.getElementById("st-to").value } });
        location.hash = `runs/${r.run_id}`;
      }
      if (b.dataset.act === "seed") { const r = await post(`/api/profiles/${id}/seed`); window.open(r.url, "_blank"); }
      if (b.dataset.act === "wipe" && prompt(`Type ${id} to wipe its Chrome profile and all its secrets. This cannot be undone.`) === id) {
        await api(`/api/profiles/${id}`, { method: "DELETE" });
      }
    }));
  },

  async runs(id) {
    if (id) return runDetail(id);
    const runs = await api("/api/runs?limit=100");
    const rows = runs.map((r) => `<tr><td><a href="#runs/${r.id}">${esc(r.kind)}</a><div class="muted mono">${r.id}</div></td><td>${esc(r.profile_id)}</td>
      <td class="${statusClass(r.status)}">${esc(r.status)}</td><td>${r.steps}</td><td>$${(r.cost_usd || 0).toFixed(3)}</td><td>${ago(r.created_at)}</td><td>${esc((r.summary || "").slice(0, 140))}</td></tr>`).join("");
    $view.innerHTML = card("Runs", rows ? `<table><tr><th>Run</th><th>Profile</th><th>Status</th><th>Steps</th><th>Cost</th><th>When</th><th>Summary</th></tr>${rows}</table>` : empty("No runs yet."));
  },

  async approvals() {
    const data = await api("/api/approvals");
    const pend = data.pending.map((a) => `<div class="card"><div class="row"><b class="grow">${esc(a.profile_id)}</b><span class="muted">expires ${esc(a.expires_at.slice(11, 19))}</span></div>
      <p><b>${esc(a.action)}</b></p><p class="muted">${esc(a.details.why || a.details.details || "")}</p>
      ${a.details.form_text ? `<pre>${esc(a.details.form_text)}</pre>` : ""}
      ${a.details.screenshot_file_id ? `<div class="step"><img src="/api/files/${a.details.screenshot_file_id}" alt="page at approval time"></div>` : ""}
      <div class="row"><button class="primary" data-ap="${a.id}" data-v="1">Approve this one action</button><button class="danger" data-ap="${a.id}" data-v="0">Deny</button></div></div>`).join("");
    const hist = data.history.map((h) => `<tr><td>${ago(h.created_at)}</td><td>${esc(h.profile_id)}</td><td>${esc(h.action)}</td><td class="${h.status === "approved" ? "ok" : "bad"}">${esc(h.status)}</td></tr>`).join("");
    $view.innerHTML = (pend || card("Pending approvals", empty("Nothing waiting."))) + card("History", hist ? `<table>${hist}</table>` : empty("None yet."));
    $view.querySelectorAll("[data-ap]").forEach((b) => b.onclick = () => act(() => post(`/api/approvals/${b.dataset.ap}`, { approve: b.dataset.v === "1" })));
  },

  async otp() {
    const reqs = state.otp;
    $view.innerHTML = card("One-time codes", reqs.length ? reqs.map((r) => `<div class="card"><div class="row"><b class="grow">${esc(r.prompt)}</b>
      <span class="${r.seconds_left < 60 ? "bad" : "muted"}" data-countdown="${Date.now() + r.seconds_left * 1000}"></span></div>
      <div class="row"><input id="code-${r.id}" inputmode="numeric" autocomplete="one-time-code" placeholder="code"><button class="primary" data-otp="${r.id}">Submit</button></div></div>`).join("")
      : empty("No code requests. When a site texts you a code, it shows up here (and in your chat app)."));
    $view.querySelectorAll("[data-otp]").forEach((b) => b.onclick = () => act(() => post(`/api/otp/${b.dataset.otp}`, { code: document.getElementById("code-" + b.dataset.otp).value })));
  },

  async handoffs() {
    $view.innerHTML = card("Open handoffs", state.handoffs.length ? `<table>${state.handoffs.map((h) =>
      `<tr><td>${esc(h.profile_id)}</td><td>${esc(h.reason)}</td><td><a href="${esc(h.url)}" target="_blank">Open remote view</a></td></tr>`).join("")}</table>` : empty("Nothing needs you right now."));
  },

  async diagnosis(id) {
    if (id) return diagDetail(id);
    const list = await api("/api/diagnosis");
    $view.innerHTML = card("Diagnosis reports", list.length ? `<table><tr><th>When</th><th>Profile</th><th>Trigger</th><th>Top cause</th><th>Next experiment</th></tr>${list.map((r) =>
      `<tr><td><a href="#diagnosis/${r.id}">${ago(r.created_at)}</a></td><td>${esc(r.profile_id)}</td><td>${esc(r.trigger_state)}</td><td>${esc(r.top_cause)}</td><td class="muted">${esc(r.next_experiment)}</td></tr>`).join("")}</table>` : empty("No reports. They are created when a sign-in hits a silent bounce or a block."));
  },

  async data() {
    const [files, tx] = await Promise.all([api("/api/files?kind=download"), api("/api/transactions")]);
    $view.innerHTML = card("Downloaded statements", files.length ? `<table>${files.map((f) => `<tr><td><a href="/api/files/${f.id}">${esc(f.name)}</a></td><td>${esc(f.profile_id)}</td><td>${(f.size / 1024).toFixed(1)} KB</td><td>${ago(f.created_at)}</td></tr>`).join("")}</table>` : empty("None yet."))
      + card(`Transactions (${tx.length})`, tx.length ? `<table><tr><th>Date</th><th>Description</th><th>Amount</th><th>Balance</th><th>Account</th></tr>${tx.slice(0, 300).map((t) =>
        `<tr><td>${esc(t.date)}</td><td>${esc(t.description)}</td><td class="${t.direction === "credit" ? "ok" : ""}">${t.direction === "debit" ? "-" : "+"}${esc(t.amount)} ${esc(t.currency)}</td><td>${esc(t.balance || "")}</td><td class="muted">${esc(t.source)}</td></tr>`).join("")}</table>` : empty("None yet."),
        '<a href="/api/transactions?format=csv"><button>Export CSV</button></a>');
  },

  async secrets() {
    const list = await api("/api/secrets");
    $view.innerHTML = card("Add or replace a secret", `<div class="row"><input id="s-name" placeholder="site/account/password" size="32">
      <select id="s-kind"><option>password</option><option>username</option><option>totp_seed</option><option>pin</option><option>proxy</option><option>other</option></select>
      <input id="s-val" type="password" placeholder="value" autocomplete="off"><button class="primary" id="s-save">Save</button></div>
      <p class="muted">Values are encrypted immediately and are never shown again. Conventional names: &lt;site&gt;/&lt;account&gt;/username, /password, /totp-seed, /security-answer-*.</p>`)
      + card("Stored secrets (names only)", list.length ? `<table><tr><th>Name</th><th>Kind</th><th>Updated</th><th>Last used</th><th></th></tr>${list.map((m) =>
        `<tr><td class="mono">${esc(m.name)}</td><td>${esc(m.kind)}</td><td>${ago(m.updated_at)}</td><td>${ago(m.last_used_at)}</td><td><button class="danger" data-del="${esc(m.name)}">Delete</button></td></tr>`).join("")}</table>` : empty("None."));
    document.getElementById("s-save").onclick = () => act(async () => {
      await api(`/api/secrets/${document.getElementById("s-name").value}`, { method: "PUT", body: JSON.stringify({ value: document.getElementById("s-val").value, kind: document.getElementById("s-kind").value }) });
    });
    $view.querySelectorAll("[data-del]").forEach((b) => b.onclick = () => confirm(`Delete ${b.dataset.del}?`) && act(() => api(`/api/secrets/${b.dataset.del}`, { method: "DELETE" })));
  },

  async sites() {
    const sites = await api("/api/sites");
    const notes = await Promise.all(sites.map((s) => api(`/api/sites/${s.id}/notes`)));
    $view.innerHTML = card("Add a site", `<div class="row"><input id="si-id" placeholder="id e.g. hdfc"><input id="si-name" placeholder="name">
      <input id="si-login" placeholder="login URL" size="30"><input id="si-domains" placeholder="domains, comma separated" size="30">
      <input id="si-otp" placeholder="auth/OTP domains (optional)"><input id="si-senders" placeholder="OTP email senders (optional)"><button class="primary" id="si-save">Save</button></div>`)
      + card("Add a profile", `<div class="row"><select id="p-site">${sites.map((s) => `<option>${esc(s.id)}</option>`).join("")}</select><input id="p-acct" placeholder="account label e.g. me">
      <input id="p-tz" placeholder="timezone e.g. Asia/Kolkata"><input id="p-locale" placeholder="locale e.g. en-IN"><button class="primary" id="p-save">Create</button></div>`)
      + sites.map((s, i) => card(esc(s.name) + ` <span class="muted mono">${esc(s.id)}</span>`, `<div class="muted">${esc(s.login_url || "")} · ${s.domains.concat(s.otp_domains).map(esc).join(", ")}${s.otp_senders.length ? " · OTP from " + s.otp_senders.map(esc).join(", ") : ""}</div>
        ${notes[i].backoff && notes[i].backoff.failures ? `<p class="bad">Backoff: ${notes[i].backoff.failures} failure(s), next allowed ${esc(notes[i].backoff.next_allowed_at)} <button data-clear="${esc(s.id)}">Clear</button></p>` : ""}
        <h3>Site notes (hints for the agent)</h3>${notes[i].notes.length ? notes[i].notes.map((n) => `<div class="row"><span class="chip">${esc(n.kind)}</span><span class="grow ${n.stale ? "muted" : ""}">${n.stale ? "[stale] " : ""}${esc(n.text)}</span><button data-note="${esc(s.id)}/${n.id}">×</button></div>`).join("") : empty("None yet.")}`)).join("");
    const split = (v) => v.split(",").map((x) => x.trim()).filter(Boolean);
    document.getElementById("si-save").onclick = () => act(async () => {
      const id = document.getElementById("si-id").value.trim();
      await api(`/api/sites/${id}`, { method: "PUT", body: JSON.stringify({ id, name: document.getElementById("si-name").value || id, login_url: document.getElementById("si-login").value || null,
        domains: split(document.getElementById("si-domains").value), otp_domains: split(document.getElementById("si-otp").value), otp_senders: split(document.getElementById("si-senders").value) }) });
    });
    document.getElementById("p-save").onclick = () => act(async () => {
      const config = { locale: document.getElementById("p-locale").value || "en-US", timezone: document.getElementById("p-tz").value || null };
      await post("/api/profiles", { site_id: document.getElementById("p-site").value, account: document.getElementById("p-acct").value.trim(), config });
    });
    $view.querySelectorAll("[data-clear]").forEach((b) => b.onclick = () => act(() => post(`/api/sites/${b.dataset.clear}/backoff/clear`)));
    $view.querySelectorAll("[data-note]").forEach((b) => b.onclick = () => act(() => api(`/api/sites/${b.dataset.note.split("/")[0]}/notes/${b.dataset.note.split("/")[1]}`, { method: "DELETE" })));
  },

  async audit() {
    const data = await api("/api/audit?limit=300");
    $view.innerHTML = card(`Audit log <span class="${data.chain_ok ? "ok" : "bad"}">${data.chain_ok ? "hash chain intact" : "HASH CHAIN BROKEN"}</span>`,
      `<table><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Details</th></tr>${data.entries.map((e) =>
        `<tr><td class="mono">${esc(e.ts.slice(0, 19))}</td><td>${esc(e.actor)}</td><td>${esc(e.action)}</td><td class="mono">${esc(e.target || "")}</td><td class="mono muted">${esc(JSON.stringify(e.details)).slice(0, 220)}</td></tr>`).join("")}</table>`);
  },
};

async function runDetail(id) {
  const data = await api(`/api/runs/${id}`);
  const r = data.run;
  const steps = data.steps.map((s) => `<div class="step ${esc(s.kind)}"><span class="muted mono">${esc(s.ts.slice(11, 19))}</span>
    <b>${esc(s.kind === "tool" ? s.tool : s.kind)}</b>${s.state ? ` <span class="chip">${esc(s.state)}</span>` : ""}
    ${s.args && Object.keys(s.args).length ? `<span class="mono muted">${esc(JSON.stringify(s.args))}</span>` : ""}
    ${s.result ? `<pre>${esc(s.result)}</pre>` : ""}${s.screenshot_file_id ? `<img loading="lazy" src="/api/files/${s.screenshot_file_id}" alt="masked screenshot">` : ""}</div>`).join("");
  $view.innerHTML = card(`${esc(r.kind)} · ${esc(r.profile_id)} <span class="${statusClass(r.status)}">${esc(r.status)}</span>`,
    `<p>${esc(r.summary || "")}</p><div class="muted">${r.steps} steps · $${(r.cost_usd || 0).toFixed(4)} · started ${ago(r.started_at)}</div>
     ${r.result ? `<details><summary>Result</summary><pre>${esc(JSON.stringify(r.result, null, 2))}</pre></details>` : ""}
     ${r.result?.diagnosis_report ? `<p><a href="#diagnosis/${r.result.diagnosis_report}">Open diagnosis report</a></p>` : ""}`,
    data.active ? `<button class="danger" id="cancel">Stop run</button>` : "") + card("Step log", steps || empty("No steps yet."));
  const c = document.getElementById("cancel");
  if (c) c.onclick = () => act(() => post(`/api/runs/${id}/cancel`));
  $view.querySelectorAll(".step img").forEach((img) => img.onclick = () => window.open(img.src, "_blank"));
  if (data.active) window.scrollTo(0, document.body.scrollHeight);
}

async function diagDetail(id) {
  const r = await api(`/api/diagnosis/${id}`);
  const causes = r.causes.map((c) => `<tr><td><b>${esc(c.cause.replace(/_/g, " "))}</b></td><td>${esc(c.likelihood)} (${c.score})</td>
    <td>${c.confirmed ? '<span class="ok">confirmed by experiment</span>' : '<span class="warn">unconfirmed</span>'}</td><td><ul>${c.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></td></tr>`).join("");
  const exps = (r.experiments || []).map((e) => `<tr><td>${esc(e.name)}</td><td>${esc(e.mode)}</td><td>${esc(e.variable || "")}</td><td>${esc(e.status)}</td><td>${esc(e.result_state || "")} ${e.http_status ? "HTTP " + e.http_status : ""}${e.verdict ? ` <span class="warn">${esc(e.verdict)}</span>` : ""}</td><td class="muted">${esc(e.reason || e.notes || "")}</td></tr>`).join("");
  const cap = r.capture || {};
  const env = r.environment || {}, eg = env.egress || {};
  const envLine = r.environment ? `<p class="muted">Driver ${esc(env.driver)} · pinned navigator.webdriver ${esc(String(env.pinned_webdriver))} · proxy ${esc(env.proxy || "none")} · egress ${esc(eg.ip || "?")} ${esc(eg.org || "")}${eg.datacenter ? ' <span class="bad">datacenter</span>' : ""}</p>` : "";
  $view.innerHTML = card(`Diagnosis: ${esc(r.profile_id)} · ${esc(r.trigger_state)}`, `<p><b>Cheapest next experiment:</b> ${esc(r.next_experiment)}</p>${envLine}
      <div class="row"><button class="primary" id="d-manual">Run manual-login test (remote view)</button><button id="d-probes">Re-run page-load probes</button></div>`)
    + card("Likely causes", `<table><tr><th>Cause</th><th>Likelihood</th><th>Status</th><th>Evidence</th></tr>${causes}</table>`)
    + card("Experiments (one variable each)", exps ? `<table><tr><th>Name</th><th>Mode</th><th>Variable</th><th>Status</th><th>Result</th><th>Notes</th></tr>${exps}</table>` : empty("None yet."))
    + card("Captured evidence", `<p>Final URL: <span class="mono">${esc(cap.final_url)}</span> · cookies: ${esc(cap.cookie_count)}</p>
      ${cap.screenshot_file_id ? `<img src="/api/files/${cap.screenshot_file_id}" style="max-width:100%;border:1px solid var(--line)">` : ""}
      <h3>Navigations</h3><pre>${esc(JSON.stringify(cap.navigations, null, 1))}</pre><h3>Console errors</h3><pre>${esc((cap.console_errors || []).join("\n") || "none")}</pre>
      <h3>Failed requests</h3><pre>${esc(JSON.stringify(cap.failed_requests || [], null, 1))}</pre>
      <h3>Fingerprint findings</h3><ul>${(r.fingerprint_findings || []).map((f) => `<li class="${f.severity === "high" ? "bad" : f.severity === "medium" ? "warn" : "muted"}">${esc(f.finding)}</li>`).join("") || "<li>none</li>"}</ul>
      <details><summary>Raw fingerprint and IP geolocation</summary><pre>${esc(JSON.stringify({ fingerprint: r.fingerprint, geo: r.geo }, null, 2))}</pre></details>
      <h3>Visible text</h3><pre>${esc(cap.visible_text)}</pre>`);
  document.getElementById("d-manual").onclick = () => act(async () => { const x = await post(`/api/diagnosis/${id}/manual`); location.hash = `runs/${x.run_id}`; });
  document.getElementById("d-probes").onclick = () => act(async () => { const x = await post(`/api/diagnosis/${id}/probes`); location.hash = `runs/${x.run_id}`; });
}

async function route() {
  const [name, id] = location.hash.slice(1).split("/");
  renderNav();
  if (!state) await refreshState();
  try { await (views[name || "accounts"] || views.accounts)(id); } catch (e) { $view.innerHTML = card("Error", `<pre>${esc(e.message)}</pre>`); }
}

function tickCountdowns() {
  document.querySelectorAll("[data-countdown]").forEach((el) => {
    const s = Math.max(0, ((+el.dataset.countdown - Date.now()) / 1000) | 0);
    el.textContent = s ? `expires in ${(s / 60) | 0}:${String(s % 60).padStart(2, "0")}` : "expired";
  });
}

window.addEventListener("hashchange", route);
refreshState().then(route);
setInterval(async () => {
  const before = JSON.stringify([state?.approvals.length, state?.otp.length, state?.handoffs.length, state?.active_runs]);
  await refreshState();
  const after = JSON.stringify([state?.approvals.length, state?.otp.length, state?.handoffs.length, state?.active_runs]);
  const [name, id] = location.hash.slice(1).split("/");
  const typing = document.activeElement && ["INPUT", "SELECT"].includes(document.activeElement.tagName);
  if (!typing && (before !== after || (name === "runs" && id) || name === "accounts" && before !== after)) route();
}, 3000);
setInterval(tickCountdowns, 1000);
