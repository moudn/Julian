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
  // Only force a logout on 401 if we actually had a session to invalidate
  // (a stored key that got revoked/expired). Without one — e.g. a failed
  // login/signup attempt — fall through so the real error (e.g. "Invalid
  // email or password") reaches the caller instead of a generic message.
  if (response.status === 401 && key) { ui.logout(); throw new Error("Signed out"); }
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
// esc() blocks HTML injection but not a javascript: scheme in an href — only
// ever render a URL as a link after this passes (data comes from research
// sources: the fetched site's own URL and third-party search results).
const safeHref = (url) => {
  try { return ["http:", "https:"].includes(new URL(url).protocol) ? url : "#"; }
  catch (e) { return "#"; }
};
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
  (routes[name] || routes.dashboard)(arg).catch((e) => {
    oops(e);
    // A toast alone left a blank or stale page with no explanation of why
    // (e.g. an inactive subscription 402ing the analytics endpoint).
    if (!$("#page").innerHTML.trim() || $("#page").dataset.page !== name) {
      $("#page").innerHTML = `<div class="card"><div class="empty">
        Couldn't load this page: ${esc(e.message || String(e))}</div></div>`;
    }
  }).then(() => { $("#page").dataset.page = name; });
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
  async resendVerification() {
    try {
      await api("/auth/resend_verification", { method: "POST" });
      toast("Verification email sent — check your inbox.");
    } catch (e) { oops(e); }
  },
  async verifyEmail(event) {
    event.preventDefault();
    const token = new FormData(event.target).get("token")?.trim();
    if (!token) return false;
    try {
      await api("/auth/verify_email", { method: "POST", json: { token } });
      toast("Email verified — you're all set.");
      ORG = await api("/auth/me");
      $("#verify-banner").classList.add("hidden");
    } catch (e) { oops(e); }
    return false;
  },

  async uploadCsv(input) {
    if (!input.files.length) return;
    const body = new FormData();
    body.append("file", input.files[0]);
    try {
      const result = await api("/leads/import", { method: "POST", body });
      // The API says exactly why each row was skipped; showing only the
      // counts left "0 imported" looking like an unexplained failure.
      const why = (result.errors || []).slice(0, 3).join(" · ");
      const more = (result.errors || []).length > 3
        ? ` (+${result.errors.length - 3} more)` : "";
      toast(`Imported ${result.imported}, skipped ${result.skipped}.`
            + (why ? ` — ${why}${more}` : ""),
            result.imported === 0 && result.skipped > 0);
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
    data.research_enabled = event.target.querySelector('[name="research_enabled"]')?.checked ?? undefined;
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
  async deleteLead(id, name) {
    if (!confirm(
      `Permanently erase ${name} — their details, drafts, conversation and `
      + `bookings.\n\nTheir email address is also added to your `
      + `do-not-contact list, so Julian can't mail it again. You can undo `
      + `that from Settings if you need the address back.\n\nDelete?`)) return;
    try {
      await api(`/leads/${id}`, { method: "DELETE" });
      toast(`${name} deleted.`);
      location.hash = "#/leads";
    } catch (e) { oops(e); }
  },
  async removeSuppression(id, email, reason) {
    // Re-contacting someone who explicitly opted out is the risky case, so
    // that confirmation says plainly what it means.
    const warning = reason === "unsubscribed" || reason === "erased"
      ? `${email} asked not to be contacted again.\n\nRemoving them lets `
        + `Julian email this address. Only do this if you have a genuine `
        + `reason — re-contacting someone after an opt-out can breach `
        + `anti-spam law.\n\nRemove anyway?`
      : `Allow Julian to contact ${email} again?`;
    if (!confirm(warning)) return;
    try {
      await api(`/suppressions/${id}`, { method: "DELETE" });
      toast(`${email} can be contacted again.`);
      route();
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
  if (lead.state === "SCORED") {
    actions.push(act("Generate sequence", "generate_sequence", true));
    actions.push(act("Research now", "research"));
  }
  if (lead.state === "OUTREACH_PENDING") {
    actions.push(act("Activate autopilot", "activate_sequence", true,
      "Julian will start emailing this lead on schedule. Activate?"));
    actions.push(act("Regenerate drafts", "generate_sequence"));
    actions.push(act("Propose meeting now", "propose_meeting"));
  }
  if (lead.state === "ENGAGED") actions.push(act("Propose meeting times", "propose_meeting", true));
  actions.push(`<button class="btn danger" onclick="ui.deleteLead(${id}, '${esc(lead.name)}')"
    >Delete lead</button>`);

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
        <div class="card"><h2>Research</h2>
          ${lead.research_notes ? `
            <pre style="white-space:pre-wrap;font:13px/1.5 inherit;margin:0">${esc(lead.research_notes)}</pre>
            ${lead.research_sources ? `<div class="muted small" style="margin-top:8px">Sources:
              ${lead.research_sources.map((s) => `<a href="${esc(safeHref(s))}" target="_blank" rel="noopener">${esc(safeHref(s) === "#" ? s : new URL(s).hostname)}</a>`).join(", ")}</div>` : ""}`
          : lead.researched_at ? `<div class="muted small">Researched, but nothing citable was found.</div>`
          : `<div class="muted small">Not yet researched. Julian researches automatically when you generate a sequence (if enabled in Settings).</div>`}
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

routes.analytics = async () => {
  const a = await api("/analytics");
  const f = a.funnel;
  const kpi = (n, label, suffix = "") =>
    `<div class="kpi"><div class="num">${n}${suffix}</div><div class="lbl">${label}</div></div>`;

  // per-step and per-variant bars scale to the largest reply_rate present
  const maxStepRate = Math.max(1, ...a.per_step.map((s) => s.reply_rate));
  const stepRows = a.per_step.map((s) => `
    <tr><td>Step ${s.step}</td><td>${s.sent}</td><td>${s.replies}</td>
      <td style="min-width:160px">
        <div style="background:var(--surface);border-radius:5px;overflow:hidden">
          <div style="width:${(s.reply_rate / maxStepRate) * 100}%;background:var(--s-blue);
            height:16px;border-radius:5px"></div></div></td>
      <td>${s.reply_rate}%</td></tr>`).join("");

  const variantRows = a.per_variant.map((v) => `
    <tr><td>${esc(v.variant)}</td><td>${v.contacted}</td><td>${v.replied}</td>
      <td>${v.reply_rate}%</td></tr>`).join("");

  // weekly trend: paired sent/reply bars
  const maxWeek = Math.max(1, ...a.weekly.map((w) => w.sent));
  const weekBars = a.weekly.map((w) => `
    <div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:4px">
      <div style="display:flex;align-items:flex-end;gap:2px;height:90px">
        <div title="${w.sent} sent" style="width:9px;background:var(--s-violet);
          height:${(w.sent / maxWeek) * 100}%;min-height:2px;border-radius:3px 3px 0 0"></div>
        <div title="${w.replies} replies" style="width:9px;background:var(--s-green);
          height:${(w.replies / maxWeek) * 100}%;min-height:2px;border-radius:3px 3px 0 0"></div>
      </div>
      <div class="muted" style="font-size:10px">${w.week.slice(5)}</div>
    </div>`).join("");

  $("#page").innerHTML = `
    <div class="page-head"><div><h1>Analytics</h1>
      <span class="muted">How your outreach is landing. Reply rate is
      replies ÷ leads contacted.</span></div></div>
    ${f.contacted === 0 ? `<div class="card"><div class="empty">
      ${f.failed ? `${f.failed} message(s) failed to send, so there's nothing
        to measure yet. Check your email settings — open a lead's sequence to
        see the exact send error.`
      : f.queued ? `${f.queued} message(s) are queued but none have sent yet.
        Sends only go out in business hours, up to your daily cap.`
      : `No sent outreach yet — activate a sequence and check back once Julian
        has emailed some leads.`}</div></div>` : `
    <div class="grid kpis">
      ${kpi(f.contacted, "Leads contacted")}
      ${kpi(f.reply_rate, "Reply rate", "%")}
      ${kpi(f.interested, "Interested replies")}
      ${kpi(f.meetings_booked, "Meetings booked")}
      ${kpi(f.meeting_rate, "Contacted → meeting", "%")}
    </div>
    <div class="cols">
      <div class="card"><h2>Which touch earns replies</h2>
        <table>
          <tr><th>Step</th><th>Sent</th><th>Replies</th><th>Reply rate</th><th></th></tr>
          ${stepRows || `<tr><td colspan="5" class="muted">No sends yet.</td></tr>`}
        </table>
        <p class="muted small" style="margin-top:10px">Replies are attributed
          to the most recent touch when the lead responded — showing which
          message in the sequence is doing the work.</p>
      </div>
      <div>
        <div class="card"><h2>By template</h2>
          <table>
            <tr><th>Template</th><th>Contacted</th><th>Replied</th><th>Reply rate</th></tr>
            ${variantRows || `<tr><td colspan="4" class="muted">No data.</td></tr>`}
          </table>
          <p class="muted small" style="margin-top:10px">One template today.
            When you A/B test new methods, each appears here to compare.</p>
        </div>
        <div class="card"><h2>Last 8 weeks</h2>
          <div style="display:flex;gap:4px;align-items:flex-end">${weekBars}</div>
          <div class="muted small" style="margin-top:8px">
            <span style="color:var(--s-violet)">■</span> sent
            &nbsp; <span style="color:var(--s-green)">■</span> replies</div>
        </div>
      </div>
    </div>`}`;
};

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
  const [google, billing, rules, suppressions] = await Promise.all([
    api("/integrations/google/status"), api("/billing/status"), api("/icp/rules"),
    api("/suppressions"),
  ]);
  $("#page").innerHTML = `
    <div class="page-head"><h1>Settings</h1></div>
    <div class="cols">
      <div>
        <div class="card"><h2>Julian's briefing</h2>
          <form onsubmit="return ui.saveSettings(event)">
            <label>Sender name (the name Julian signs every email with)
              <input name="sender_name" value="${esc(ORG.sender_name || "")}" placeholder="Alex Rivera"></label>
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
            <label><input type="checkbox" name="research_enabled" style="width:auto"
              ${ORG.research_enabled ? "checked" : ""}>
              Research each lead (company site + news) before writing</label>
            <label><input type="checkbox" name="auto_reply_enabled" style="width:auto"
              ${ORG.auto_reply_enabled ? "checked" : ""}>
              Let Julian reply on his own — knowledge-base answers, and a short
              "here's what we do, worth a call?" brief when a lead asks what this
              is (off = he drafts it and you send)</label>
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
          ${google.connected && google.broken
            ? `<p>⚠️ Connection needs attention: ${esc(google.broken_reason || "access was revoked")}. Reconnect to resume sending.</p>
               <button class="btn primary" onclick="ui.googleConnect()">Reconnect Google</button>`
            : google.connected
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
        <div class="card"><h2>Do-not-contact list</h2>
          <p class="muted small">Julian will never email these addresses, and
            they can't be re-imported. Opt-outs land here automatically.</p>
          ${suppressions.length ? `<table>
            <tr><th>Address</th><th>Why</th><th>Added</th><th></th></tr>
            ${suppressions.map((s) => `<tr>
              <td>${esc(s.email)}</td>
              <td class="muted small">${esc(s.reason_label)}</td>
              <td class="muted small">${fmtDate(s.created_at)}</td>
              <td><button class="btn danger small"
                onclick="ui.removeSuppression(${s.id}, '${esc(s.email)}', '${esc(s.reason)}')"
                >Remove</button></td></tr>`).join("")}
          </table>
          <p class="muted small" style="margin-top:10px">Removing an address
            means Julian can contact it again. If they asked you to stop,
            that's your call and your liability — check you have a genuine
            reason first.</p>`
          : `<div class="empty">Nobody has opted out yet.</div>`}
        </div>
      </div>
    </div>`;
};

/* ---------- boot ---------- */
async function refreshGoogleBanner() {
  // A revoked Google connection silently stops all outreach, so it needs to
  // be visible from every page — not only from Settings.
  try {
    const google = await api("/integrations/google/status");
    const broken = google.connected && google.broken;
    $("#google-banner").classList.toggle("hidden", !broken);
    if (broken) {
      $("#google-banner-text").textContent =
        `Julian can't reach your Google account (${google.broken_reason
          || "access was revoked or expired"}) — sending and scheduling are paused.`;
    }
  } catch (e) { /* auth/billing handled elsewhere */ }
}

async function refreshApprovalsBadge() {
  try {
    const bookings = await api("/bookings/pending");
    const el = $("#approvals-badge");
    el.textContent = bookings.length;
    el.classList.toggle("hidden", bookings.length === 0);
  } catch (e) { /* banner/401 already handled */ }
}

/* Julian keeps working in the background (sending steps, polling replies,
   triaging), so a view rendered a minute ago is already stale — a lead can
   go NOT_INTERESTED without the open page ever knowing. Re-render on a
   timer and whenever the tab regains focus.

   Skipped while the user is typing into a field or has text selected, so a
   refresh can never wipe half-written input or fight the user. */
const AUTO_REFRESH_MS = 20000;

function userIsBusy() {
  const el = document.activeElement;
  if (el && ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)) return true;
  return !window.getSelection().isCollapsed;
}

function autoRefresh() {
  if (document.hidden || userIsBusy()) return;
  if (!localStorage.getItem("julian_key")) return;
  refreshGoogleBanner();
  route();
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
  $("#verify-banner").classList.toggle("hidden", ORG.email_verified !== false);
  try { await api("/leads"); } catch (e) { /* 402 shows banner */ }
  refreshGoogleBanner();
  route();

  if (!boot._refreshing) {
    boot._refreshing = true;
    setInterval(autoRefresh, AUTO_REFRESH_MS);
    // Coming back to the tab is the moment stale data is most obvious.
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) autoRefresh();
    });
  }
}

boot();
