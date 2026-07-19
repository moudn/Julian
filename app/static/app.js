/* Julian dashboard — vanilla JS SPA over the JSON API */
"use strict";

const API = "";
let ORG = null;

/* ---------- api helper ---------- */
async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const key = localStorage.getItem("julian_key");
  if (key) headers["Authorization"] = "Bearer " + key;
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.json);
  }
  const response = await fetch(API + path, { ...options, headers });
  if (response.status === 401) { ui.logout(); throw new Error("Signed out"); }
  if (response.status === 402) { showBanner(true); }
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

/* ---------- tiny helpers ---------- */
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtDate = (iso) => iso ? new Date(iso).toLocaleString(undefined,
  { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast" + (isError ? " err" : "");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 3500);
}
const oops = (e) => toast(e.message || String(e), true);
function showBanner(show) { $("#billing-banner").classList.toggle("hidden", !show); }

const STATE_DOTS = {
  NEW: "--s-blue", SCORED: "--s-aqua", OUTREACH_PENDING: "--s-yellow",
  SEQUENCE_ACTIVE: "--s-violet", ENGAGED: "--s-orange",
  MEETING_PROPOSED: "--s-magenta", AWAITING_APPROVAL: "--s-red",
  MEETING_CONFIRMED: "--s-green", NOT_INTERESTED: "--s-gray", UNSUBSCRIBED: "--s-gray",
};
const STATE_LABELS = {
  NEW: "New", SCORED: "Scored", OUTREACH_PENDING: "Drafts ready",
  SEQUENCE_ACTIVE: "In sequence", ENGAGED: "Human engaged",
  MEETING_PROPOSED: "Times proposed", AWAITING_APPROVAL: "Awaiting approval",
  MEETING_CONFIRMED: "Meeting booked", NOT_INTERESTED: "Not interested",
  UNSUBSCRIBED: "Unsubscribed",
};
const badge = (state) =>
  `<span class="badge"><span class="dot" style="background:var(${STATE_DOTS[state] || "--s-gray"})"></span>${esc(STATE_LABELS[state] || state)}</span>`;

/* ---------- router ---------- */
const routes = {};
function route() {
  const hash = location.hash || "#/dashboard";
  const [, name, arg] = hash.split("/");
  document.querySelectorAll("[data-nav]").forEach((a) =>
    a.classList.toggle("active", a.dataset.nav === name));
  (routes[name] || routes.dashboard)(arg).catch(oops);
}
window.addEventListener("hashchange", route);

/* ---------- ui actions ---------- */
const ui = {
  authTab(which) {
    $("#tab-login").classList.toggle("active", which === "login");
    $("#tab-signup").classList.toggle("active", which === "signup");
    $("#login-form").classList.toggle("hidden", which !== "login");
    $("#signup-form").classList.toggle("hidden", which !== "signup");
    $("#auth-error").classList.add("hidden");
  },
  async login(event) {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    try {
      const result = await api("/auth/login", { method: "POST", json: data });
      localStorage.setItem("julian_key", result.api_key);
      boot();
    } catch (e) { $("#auth-error").textContent = e.message; $("#auth-error").classList.remove("hidden"); }
    return false;
  },
  async signup(event) {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    try {
      const result = await api("/auth/signup", { method: "POST", json: data });
      localStorage.setItem("julian_key", result.api_key);
      toast("Account created — your API key is stored in this browser.");
      boot();
    } catch (e) { $("#auth-error").textContent = e.message; $("#auth-error").classList.remove("hidden"); }
    return false;
  },
  logout() {
    localStorage.removeItem("julian_key");
    ORG = null;
    $("#app-view").classList.add("hidden");
    $("#auth-view").classList.remove("hidden");
  },
  async checkout() {
    try {
      const result = await api("/billing/checkout", { method: "POST" });
      window.open(result.checkout_url, "_blank");
    } catch (e) { oops(e); }
  },

  async uploadCsv(input) {
    if (!input.files.length) return;
    const body = new FormData();
    body.append("file", input.files[0]);
    try {
      const result = await api("/leads/import", { method: "POST", body });
      toast(`Imported ${result.imported}, skipped ${result.skipped}.`);
      route();
    } catch (e) { oops(e); }
    input.value = "";
  },
  async scoreAll() {
    try {
      const results = await api("/leads/score_all", { method: "POST" });
      toast(`Scored ${results.length} lead(s).`);
      route();
    } catch (e) { oops(e); }
  },
  async leadAction(id, action, confirmText) {
    if (confirmText && !confirm(confirmText)) return;
    try {
      await api(`/leads/${id}/${action}`, { method: "POST" });
      toast("Done.");
      route();
    } catch (e) { oops(e); }
  },
  async booking(id, action) {
    try {
      await api(action === "approve" ? `/approve_booking/${id}` : `/bookings/${id}/reject`,
                { method: "POST" });
      toast(action === "approve" ? "Meeting booked — calendar event created." : "Rejected — lead can pick another time.");
      route(); refreshApprovalsBadge();
    } catch (e) { oops(e); }
  },
  async simulateReply(event, leadId) {
    event.preventDefault();
    const body = new FormData(event.target).get("body");
    if (!body.trim()) return false;
    try {
      const result = await api("/replies/ingest", {
        method: "POST", json: { lead_id: Number(leadId), body },
      });
      toast(`Julian triaged it as ${result.category}.`);
      route();
    } catch (e) { oops(e); }
    return false;
  },
  async saveSettings(event) {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    data.auto_reply_enabled = event.target.querySelector('[name="auto_reply_enabled"]')?.checked ?? undefined;
    Object.keys(data).forEach((k) => { if (data[k] === "" || data[k] === undefined) delete data[k]; });
    if (data.score_threshold) data.score_threshold = Number(data.score_threshold);
    try {
      ORG = await api("/auth/org", { method: "PATCH", json: data });
      toast("Settings saved.");
    } catch (e) { oops(e); }
    return false;
  },
  async addRule(event) {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    data.weight = Number(data.weight);
    if (data.operator === "in") data.value = data.value.split(",").map((s) => s.trim());
    else if (["gte", "lte"].includes(data.operator) && !isNaN(Number(data.value)))
      data.value = Number(data.value);
    try {
      await api("/icp/rules", { method: "POST", json: data });
      toast("Rule added."); route();
    } catch (e) { oops(e); }
    return false;
  },
  async deleteRule(id) {
    try { await api(`/icp/rules/${id}`, { method: "DELETE" }); route(); }
    catch (e) { oops(e); }
  },
  async googleConnect() {
    try {
      const result = await api("/integrations/google/connect");
      window.open(result.authorize_url, "_blank");
      toast("Approve access in the new tab, then refresh this page.");
    } catch (e) { oops(e); }
  },
  async googleDisconnect() {
    if (!confirm("Disconnect Google Calendar & Gmail?")) return;
    try { await api("/integrations/google", { method: "DELETE" }); route(); }
    catch (e) { oops(e); }
  },
};

/* ---------- pages ---------- */
routes.dashboard = async () => {
  const stats = await api("/leads/stats");
  const counts = stats.by_state;
  const kpi = (n, label) => `<div class="kpi"><div class="num">${n}</div><div class="lbl">${label}</div></div>`;
  const funnelRow = (state) => (counts[state] ? `
    <tr><td>${badge(state)}</td><td style="text-align:right">${counts[state]}</td></tr>` : "");
  $("#page").innerHTML = `
    <div class="page-head"><div><h1>Dashboard</h1>
      <span class="muted">Julian is ${counts.SEQUENCE_ACTIVE ? "working " + counts.SEQUENCE_ACTIVE + " lead(s) on autopilot" : "idle — activate a sequence to put him to work"}.</span>
    </div></div>
    <div class="grid kpis">
      ${kpi(stats.total, "Total leads")}
      ${kpi(counts.SEQUENCE_ACTIVE || 0, "On autopilot")}
      ${kpi((counts.ENGAGED || 0) + (counts.MEETING_PROPOSED || 0), "In conversation")}
      ${kpi(counts.AWAITING_APPROVAL || 0, "Need your approval")}
      ${kpi(counts.MEETING_CONFIRMED || 0, "Meetings booked")}
    </div>
    <div class="card"><h2>Pipeline</h2>
      ${stats.total ? `<table>${Object.keys(STATE_LABELS).map(funnelRow).join("")}</table>`
        : `<div class="empty">No leads yet — import a CSV from the Leads page.</div>`}
    </div>`;
  refreshApprovalsBadge();
};

routes.leads = async (id) => {
  if (id) return leadDetail(id);
  const leads = await api("/leads");
  $("#page").innerHTML = `
    <div class="page-head"><h1>Leads</h1>
      <div class="actions">
        <button class="btn" onclick="ui.scoreAll()">Score all new</button>
        <label class="btn primary" style="margin:0">Import CSV
          <input type="file" accept=".csv" class="hidden" onchange="ui.uploadCsv(this)">
        </label>
      </div></div>
    <div class="card">
      ${leads.length ? `<table>
        <tr><th>Name</th><th>Company</th><th>Title</th><th>Score</th><th>Status</th></tr>
        ${leads.map((l) => `
          <tr class="click" onclick="location.hash='#/leads/${l.id}'">
            <td><strong>${esc(l.name)}</strong><div class="muted small">${esc(l.email || "")}</div></td>
            <td>${esc(l.company || "—")}</td>
            <td>${esc(l.title || "—")}</td>
            <td>${l.score ?? "—"}</td>
            <td>${badge(l.state)}</td>
          </tr>`).join("")}
      </table>` : `<div class="empty">No leads yet. Import a CSV with columns like
        <code>name,email,company,title,company_size</code>.</div>`}
    </div>`;
};

async function leadDetail(id) {
  const [lead, sequence, conversation] = await Promise.all([
    api(`/leads/${id}`), api(`/leads/${id}/sequence`),
    api(`/leads/${id}/conversation`),
  ]);
  const act = (label, action, primary = false, confirmText = "") => `
    <button class="btn ${primary ? "primary" : ""}"
      onclick="ui.leadAction(${id}, '${action}', '${confirmText}')">${label}</button>`;
  const actions = [];
  if (lead.state === "NEW") actions.push(act("Score against ICP", "score", true));
  if (lead.state === "SCORED") actions.push(act("Generate sequence", "generate_sequence", true));
  if (lead.state === "OUTREACH_PENDING") {
    actions.push(act("Activate autopilot", "activate_sequence", true,
      "Julian will start emailing this lead on schedule. Activate?"));
    actions.push(act("Regenerate drafts", "generate_sequence"));
    actions.push(act("Propose meeting now", "propose_meeting"));
  }
  if (lead.state === "ENGAGED") actions.push(act("Propose meeting times", "propose_meeting", true));

  $("#page").innerHTML = `
    <div class="page-head">
      <div><a href="#/leads">&larr; Leads</a>
        <h1>${esc(lead.name)}</h1>${badge(lead.state)}</div>
    </div>
    <div class="cols">
      <div>
        <div class="card"><h2>Details</h2>
          <dl class="kv">
            <dt>Email</dt><dd>${esc(lead.email || "—")}</dd>
            <dt>Company</dt><dd>${esc(lead.company || "—")}</dd>
            <dt>Title</dt><dd>${esc(lead.title || "—")}</dd>
            <dt>Size</dt><dd>${lead.company_size ?? "—"}</dd>
            <dt>Location</dt><dd>${esc(lead.location || "—")}</dd>
            <dt>Score</dt><dd>${lead.score ?? "not scored"}</dd>
            <dt>Source</dt><dd>${esc(lead.source)}</dd>
            ${lead.proposed_slots ? `<dt>Offered</dt><dd>${lead.proposed_slots.map(fmtDate).map(esc).join("<br>")}</dd>` : ""}
          </dl>
          <div class="actions">${actions.join("")}</div>
        </div>
        <div class="card"><h2>Sequence</h2>
          ${sequence.messages.length ? sequence.messages.map((m) => `
            <details class="step"><summary>
              <span><strong>Step ${m.step}</strong>
                <span class="muted small">day ${m.send_after_days} — ${esc(m.subject)}</span></span>
              <span class="chip ${m.status}">${m.status}</span></summary>
              <pre>${esc(m.body)}</pre>
              ${m.spam_flags ? `<p class="error small">Spam flags: ${esc(m.spam_flags.join(", "))}</p>` : ""}
            </details>`).join("")
          : `<div class="empty">No sequence yet.</div>`}
        </div>
      </div>
      <div>
        <div class="card"><h2>Conversation</h2>
          <div class="thread">
            ${conversation.length ? conversation.map((m) => `
              <div class="msg ${m.direction}">
                <div class="meta">${m.direction === "INBOUND" ? esc(lead.name) : "Julian"}
                  · ${fmtDate(m.created_at)}${m.category ? " · " + esc(m.category) : ""}</div>
                ${esc(m.body)}
                ${m.suggested_reply ? `<details><summary class="small muted">Julian's suggested reply</summary><pre>${esc(m.suggested_reply)}</pre></details>` : ""}
              </div>`).join("")
            : `<div class="empty">No messages yet.</div>`}
          </div>
          <form onsubmit="return ui.simulateReply(event, ${id})" style="margin-top:14px">
            <label>Simulate an inbound reply (demo/testing)
              <textarea name="body" placeholder="e.g. Sounds interesting — how does pricing work?"></textarea>
            </label>
            <button class="btn" type="submit">Feed to Julian</button>
          </form>
        </div>
      </div>
    </div>`;
}

routes.approvals = async () => {
  const bookings = await api("/bookings/pending");
  const leads = await api("/leads");
  const byId = Object.fromEntries(leads.map((l) => [l.id, l]));
  $("#page").innerHTML = `
    <div class="page-head"><div><h1>Approvals</h1>
      <span class="muted">Nothing lands on a calendar without your click.</span></div></div>
    ${bookings.length ? bookings.map((b) => {
      const lead = byId[b.lead_id] || { name: "Lead #" + b.lead_id };
      return `<div class="card booking">
        <div><strong>${esc(lead.name)}</strong>
          <span class="muted">${esc(lead.company || "")}</span>
          <div class="muted small">wants ${fmtDate(b.slot_start)} – ${fmtDate(b.slot_end).split(", ").pop()}</div></div>
        <div class="actions">
          <button class="btn danger" onclick="ui.booking(${b.id}, 'reject')">Reject</button>
          <button class="btn primary" onclick="ui.booking(${b.id}, 'approve')">Approve &amp; book</button>
        </div></div>`;
    }).join("") : `<div class="card"><div class="empty">No bookings waiting for approval.</div></div>`}`;
  refreshApprovalsBadge();
};

routes.settings = async () => {
  ORG = ORG || await api("/auth/me");
  const [google, billing, rules] = await Promise.all([
    api("/integrations/google/status"), api("/billing/status"), api("/icp/rules"),
  ]);
  $("#page").innerHTML = `
    <div class="page-head"><h1>Settings</h1></div>
    <div class="cols">
      <div>
        <div class="card"><h2>Julian's briefing</h2>
          <form onsubmit="return ui.saveSettings(event)">
            <label>Sales rep email (approvals go here)
              <input type="email" name="sales_rep_email" value="${esc(ORG.sales_rep_email || "")}"></label>
            <label>Score threshold
              <input type="number" name="score_threshold" value="${ORG.score_threshold}"></label>
            <label>What you sell (fed into every email Julian writes)
              <textarea name="product_description" placeholder="e.g. We build payroll software for restaurants that cuts admin time 80%">${esc(ORG.product_description || "")}</textarea></label>
            <label>Knowledge base (the ONLY facts Julian may use to answer questions)
              <textarea name="knowledge_base" placeholder="Pricing: ... Integrations: ... Onboarding: ...">${esc(ORG.knowledge_base || "")}</textarea></label>
            <label>Email footer — must include an opt-out line and your postal address
              (required by anti-spam law before Julian can send)
              <textarea name="email_footer" placeholder='--\nAcme Inc, 1 Main St, Springfield.\nIf you&#39;d rather not hear from me, just reply "no thanks".'>${esc(ORG.email_footer || "")}</textarea></label>
            <label>Timezone (IANA name — sending hours &amp; meeting slots use this)
              <input name="timezone" value="${esc(ORG.timezone || "UTC")}" placeholder="Europe/London"></label>
            <label><input type="checkbox" name="auto_reply_enabled" style="width:auto"
              ${ORG.auto_reply_enabled ? "checked" : ""}>
              Let Julian auto-send knowledge-base answers (off = he drafts, you approve)</label>
            <button class="btn primary" type="submit">Save</button>
          </form>
        </div>
        <div class="card"><h2>ICP scoring rules</h2>
          ${rules.length ? `<table>
            <tr><th>Rule</th><th>Condition</th><th>Weight</th><th></th></tr>
            ${rules.map((r) => `<tr><td>${esc(r.name)}</td>
              <td class="muted small">${esc(r.field)} ${esc(r.operator)} ${esc(JSON.stringify(r.value))}</td>
              <td>${r.weight}</td>
              <td><button class="btn ghost small" onclick="ui.deleteRule(${r.id})">✕</button></td></tr>`).join("")}
          </table>` : `<p class="muted small">No rules yet — leads can't reach SCORED without at least one.</p>`}
          <form onsubmit="return ui.addRule(event)" style="margin-top:12px">
            <div class="grid" style="grid-template-columns: 1fr 1fr">
              <label>Name<input name="name" required placeholder="Senior titles"></label>
              <label>Field<select name="field">
                <option value="title">title</option><option value="company_size">company_size</option>
                <option value="location">location</option><option value="company">company</option>
              </select></label>
              <label>Operator<select name="operator">
                <option value="contains">contains</option><option value="in">in (comma-sep)</option>
                <option value="equals">equals</option><option value="gte">≥</option><option value="lte">≤</option>
              </select></label>
              <label>Value<input name="value" required placeholder="VP, Director"></label>
            </div>
            <label>Weight<input type="number" name="weight" value="30"></label>
            <button class="btn" type="submit">Add rule</button>
          </form>
        </div>
      </div>
      <div>
        <div class="card"><h2>Google (Calendar + Gmail)</h2>
          <p class="muted small">One connection lets Julian read your availability, send from
            your address, and see replies.</p>
          ${google.connected
            ? `<p>✅ Connected${google.account_email ? " as " + esc(google.account_email) : ""}</p>
               <button class="btn danger" onclick="ui.googleDisconnect()">Disconnect</button>`
            : `<p>⚠️ Not connected — Julian falls back to a simulated calendar and console email.</p>
               <button class="btn primary" onclick="ui.googleConnect()">Connect Google</button>`}
        </div>
        <div class="card"><h2>Billing</h2>
          ${!billing.billing_enabled
            ? `<p class="muted small">Billing is not configured on this server (development mode — everything is unlocked).</p>`
            : billing.subscription_status === "active" || billing.subscription_status === "trialing"
              ? `<p>✅ Subscription ${esc(billing.subscription_status)}${billing.current_period_end ? " · renews " + fmtDate(billing.current_period_end) : ""}</p>`
              : `<p>⚠️ No active subscription (${esc(billing.subscription_status)}).</p>
                 <button class="btn primary" onclick="ui.checkout()">Subscribe</button>`}
        </div>
      </div>
    </div>`;
};

/* ---------- boot ---------- */
async function refreshApprovalsBadge() {
  try {
    const bookings = await api("/bookings/pending");
    const el = $("#approvals-badge");
    el.textContent = bookings.length;
    el.classList.toggle("hidden", bookings.length === 0);
  } catch (e) { /* banner/401 already handled */ }
}

async function boot() {
  if (!localStorage.getItem("julian_key")) return ui.logout();
  try {
    ORG = await api("/auth/me");
  } catch (e) { return; }
  $("#org-name").textContent = ORG.name;
  $("#auth-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  showBanner(false);
  try { await api("/leads"); } catch (e) { /* 402 shows banner */ }
  route();
}

boot();
