"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtS = (v) => (v == null ? "—" : `${v.toFixed(2)} s`);
const fmtUsd = (v) => (v == null ? "—" : v < 0.01 ? `$${v.toFixed(6)}` : `$${v.toFixed(4)}`);
const fmtPct = (v) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const fmtInt = (v) => (v == null ? "—" : Number(v).toLocaleString());
const shortModel = (m) => m.split("/").pop();

let CFG = null;
let SEV_RANKS = []; // from severity.yaml, most severe first
let URGENCY = [];   // urgency levels, most urgent first
let lastBatch = null;

const EXAMPLES = [
  { subject: "Charged twice this month", tier: "pro", body: "Hi, I was charged $49 twice on October 3rd for the same Pro subscription. Please refund the duplicate charge." },
  { subject: "Production API returning 500", tier: "enterprise", body: "Since 09:40 UTC every call to /v2/orders returns HTTP 500. Our checkout is down and we are losing sales every minute." },
  { subject: "Something wrong with my account and bill", tier: "free", body: "My account looks weird and I think the bill is wrong too. Please help." },
  { subject: "No puedo iniciar sesión", tier: "pro", body: "Me aparece 'usuario bloqueado' cuando intento entrar. No recuerdo haber fallado la contraseña muchas veces." },
  { subject: "Suspicious login from another country", tier: "pro", body: "I got an alert about a login from another country that wasn't me. I changed my password but I'm worried data was accessed." },
];
let exampleIdx = 0;

/* ---------- init ---------- */
async function init() {
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
  $("#ticket-form").addEventListener("submit", onTriage);
  $("#btn-example").addEventListener("click", loadExample);
  document.querySelectorAll("button.demo").forEach((b) => b.addEventListener("click", loadDemo));
  $("#btn-preview").addEventListener("click", onPreview);
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-copy]");
    if (!btn) return;
    const text = document.getElementById(btn.dataset.copy).textContent;
    navigator.clipboard.writeText(text).then(() => { btn.textContent = "Copied"; setTimeout(() => (btn.textContent = "Copy"), 1200); });
  });
  $("#btn-batch").addEventListener("click", () => runBatchFromFile());
  $("#btn-sample").addEventListener("click", runSample);
  $("#btn-download").addEventListener("click", downloadCsv);

  try {
    const [models, teams, skills, sev, tiers, urg] = await Promise.all(["/api/models", "/api/teams", "/api/skills", "/api/severity", "/api/tiers", "/api/urgency"].map((u) => fetch(u).then((r) => r.json())));
    SEV_RANKS = sev;
    URGENCY = urg;
    $("#f-tier").innerHTML = '<option value="">(none)</option>' +
      tiers.map((t) => `<option value="${esc(t.name)}" title="${esc(t.description)}">${esc(t.name)}</option>`).join("");
    CFG = models;
    document.querySelectorAll(".review-mode").forEach((s) => (s.value = models.human_review_mode || "default"));
    const others = models.models.filter((m) => !m.is_jev).map((m) => esc(m.id) + (m.known === false ? " ⚠ not found on OpenRouter" : ""));
    $("#model-info").innerHTML = `jev = <b>${esc(models.jev_model)}</b>${others.length ? " · compared with " + others.join(", ") : ""}`;
    renderTeams(teams, skills);
    $("#teams-list").insertAdjacentHTML("beforeend", `<h2 style="margin-top:18px">Customer tiers (${tiers.length})</h2>
      <p class="muted small">Loaded from <code>tiers.yaml</code>. The tier and its description are sent to every model to weigh urgency and escalation.</p>
      ${tiers.map((t) => `<div class="team-item"><b>${esc(t.name)}</b><div class="muted">${esc(t.description) || "—"}</div></div>`).join("")}`);
    $("#teams-list").insertAdjacentHTML("beforeend", `<h2 style="margin-top:18px">Urgency levels (${URGENCY.length})</h2>
      <p class="muted small">How fast a ticket needs a response (severity is how serious the impact is). Critical urgency always goes to human review.</p>
      ${urgencyLegend(null)}`);
    $("#teams-list").insertAdjacentHTML("beforeend", `<h2 style="margin-top:18px">Severity ranks (${SEV_RANKS.length})</h2>
      <p class="muted small">Loaded from <code>severity.yaml</code>. They build the chat models' rubric, jev's severity scale and the SEV level.
      Severity is informative: it does not change the automatic / human decision.</p>${severityLegend(null)}`);
  } catch (e) {
    $("#model-info").textContent = "Could not load configuration: " + e;
  }
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => (p.hidden = p.id !== `tab-${name}`));
}

function fillTicket(ex) {
  $("#f-subject").value = ex.subject;
  $("#f-body").value = ex.body;
  $("#f-tier").value = ex.tier;
}

function loadExample() {
  fillTicket(EXAMPLES[exampleIdx++ % EXAMPLES.length]);
}

// Demo buttons load the first three examples (automatic / critical / ambiguous) without running the triage.
function loadDemo(ev) {
  const i = Number(ev.currentTarget.dataset.demo);
  fillTicket(EXAMPLES[i]);
  exampleIdx = i + 1;
  $("#single-result").innerHTML = "";
  $("#single-status").textContent = "";
}

function renderTeams(teams, skills) {
  const t = teams.map((x) => `<div class="team-item"><b>${esc(x.name)}</b><div class="muted">${esc(x.description)}</div>
    ${x.examples.length ? `<div class="small muted">Examples: ${x.examples.map((e) => `"${esc(e)}"`).join(", ")}</div>` : ""}</div>`).join("");
  const s = skills.length
    ? skills.map((x) => `<div class="team-item"><b>${esc(x.name)}</b>${x.always ? ' <span class="badge">always</span>' : ""}
        <div class="muted">${esc(x.description)}</div>
        <div class="small muted">Triggers: ${x.triggers.map(esc).join(", ") || "—"}</div>
        <details><summary class="small">Instructions</summary><div class="reply small">${esc(x.body)}</div></details></div>`).join("")
    : '<p class="muted">No skills loaded. Add <code>skills/&lt;name&gt;/SKILL.md</code> files and restart.</p>';
  $("#teams-list").innerHTML = `<h2>Teams (${teams.length})</h2>${t}<h2 style="margin-top:18px">Agent Skills (${skills.length})</h2>
    <p class="muted small">Loaded from <code>skills/*/SKILL.md</code>. A skill is added to the prompt for every model when one of its triggers appears in the ticket.</p>${s}`;
}

/* ---------- prompt / request inspector ---------- */
let codeSeq = 0;
function codeBlock(title, text) {
  const id = `code-${++codeSeq}`;
  return `<div class="code-head"><h3>${esc(title)}</h3><button type="button" data-copy="${id}">Copy</button></div>
    <pre class="code" id="${id}">${esc(text)}</pre>`;
}

function promptSection(messages, skills) {
  const sys = messages.find((m) => m.role === "system");
  const user = messages.find((m) => m.role === "user");
  const sk = skills && skills.length ? skills.map((s) => `<span class="badge">${esc(s)}</span>`).join(" ") : '<span class="muted">none</span>';
  return `<p class="small">Skills added to the user message: ${sk}</p>
    ${sys ? codeBlock("System prompt", sys.content) : ""}
    ${user ? codeBlock("User prompt", user.content) : ""}`;
}

const DECISIONS_NOTE = `jev is a <b>decision model</b>: it is called through <code>/api/alpha/decisions</code>, not chat. The ticket (and any
  matching skills, as <code>guidelines</code>) goes in <code>state</code>, and the triage is asked as typed <code>questions</code>
  (choice / score / noul). It answers with probabilities and does not generate text.`;

function apiLabel(r) {
  if (r.api === "decisions") return "Decisions API";
  return `chat · response_format: ${r.request && r.request.response_format ? r.request.response_format.type : "none (plain text)"}`;
}

function requestCard(runs, skills) {
  const withReq = runs.filter((r) => r.request);
  if (!withReq.length) return "";
  const jev = withReq.find((r) => r.is_jev) || withReq[0];
  const chat = withReq.find((r) => r.request.messages);
  const others = withReq.filter((r) => r !== jev).map((r) => `<details class="req"><summary><b>${esc(r.model)}</b>
      <span class="muted small">· ${esc(apiLabel(r))}</span></summary>${codeBlock(`JSON sent to ${r.model}`, JSON.stringify(r.request, null, 2))}</details>`).join("");
  return `<div class="card"><h2>Prompt &amp; request</h2>
    ${jev.api === "decisions" ? `<p class="small">${DECISIONS_NOTE}</p>` : ""}
    ${codeBlock(`JSON sent to jev (${jev.model}) · ${apiLabel(jev)}`, JSON.stringify(jev.request, null, 2))}
    ${chat ? `<h3 style="margin-top:14px">Prompt for the chat models</h3>
      <p class="small muted">Every chat model receives the same messages. Only <code>model</code> changes, and <code>response_format</code> changes when a
      model doesn't support JSON schema. The API key travels in an HTTP header and is not shown.</p>
      ${promptSection(chat.request.messages, skills)}` : ""}
    ${others ? `<h3 style="margin-top:12px">JSON sent to the other models</h3>${others}` : ""}
  </div>`;
}

function ticketPayload() {
  return { subject: $("#f-subject").value.trim(), body: $("#f-body").value.trim(), customer_tier: $("#f-tier").value || null };
}

async function onPreview() {
  const t = ticketPayload();
  if (!t.body) { $("#single-status").textContent = "Write the ticket body first."; return; }
  try {
    const r = await fetch("/api/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(t) });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    const p = await r.json();
    $("#single-status").textContent = "";
    $("#single-result").innerHTML = `<div class="card"><h2>Preview · nothing was sent</h2>
      ${p.jev_api === "decisions" ? `<p class="small">${DECISIONS_NOTE}</p>` : ""}
      ${codeBlock(`JSON that would be sent to jev (${p.request.model}) · ${p.jev_api === "decisions" ? "Decisions API" : "chat"}`, JSON.stringify(p.request, null, 2))}
      <h3 style="margin-top:14px">Prompt for the chat models</h3>
      <p class="small muted">Every chat model gets these messages with its own <code>model</code> id.</p>
      ${promptSection(p.messages, p.skills)}</div>`;
  } catch (e) {
    $("#single-status").innerHTML = `<span class="err">${esc(e.message)}</span>`;
  }
}

/* ---------- single ticket ---------- */
// Reads an NDJSON response line by line, calling onMsg(obj) for each line.
async function readNdjson(response, onMsg) {
  const reader = response.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) onMsg(JSON.parse(line));
    }
  }
}

function progressPanel(models, jevModel) {
  const rows = models.map((m, i) => `<div class="mprog-row" id="mprog-${i}">
      <span class="mprog-name">${esc(m)}${m === jevModel ? " <b>(jev)</b>" : ""}</span>
      <div class="mprog-track"><div class="mprog-bar running"></div></div>
      <span class="mprog-status small muted">running…</span></div>`).join("");
  return `<div class="card" id="mprog"><h2>Running triage · <span id="mprog-count">0</span> / ${models.length} models done</h2>
    <div class="progress"><div class="progress-bar" id="mprog-total"></div></div>
    <div class="mprog-list">${rows}</div></div>`;
}

async function onTriage(ev) {
  ev.preventDefault();
  const body = $("#f-body").value.trim();
  if (!body) return;
  const btn = $("#btn-triage");
  btn.disabled = true;
  $("#single-status").textContent = "";
  $("#single-result").innerHTML = "";
  let timer = null;
  try {
    const r = await fetch(`/api/triage/stream?review_mode=${encodeURIComponent($("#f-review").value)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(ticketPayload()),
    });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    const started = performance.now();
    let total = 0, done = 0, result = null;
    const finished = new Set();
    await readNdjson(r, (msg) => {
      if (msg.type === "start") {
        total = msg.models.length;
        $("#single-result").innerHTML = progressPanel(msg.models, msg.jev_model);
        timer = setInterval(() => {
          const secs = ((performance.now() - started) / 1000).toFixed(1);
          for (let i = 0; i < total; i++) {
            if (!finished.has(i)) { const s = document.querySelector(`#mprog-${i} .mprog-status`); if (s) s.textContent = `running… ${secs} s`; }
          }
        }, 100);
      } else if (msg.type === "model") {
        finished.add(msg.index);
        done++;
        const row = $(`#mprog-${msg.index}`);
        if (row) {
          const bar = row.querySelector(".mprog-bar");
          bar.classList.remove("running");
          bar.classList.add(msg.ok ? "ok" : "fail");
          const m = msg.metrics;
          row.querySelector(".mprog-status").innerHTML = msg.ok
            ? `✓ ${fmtS(m.latency_s)} · ${fmtInt(m.total_tokens)} tok · ${fmtUsd(m.cost_usd)}`
            : `<span class="err" title="${esc(msg.error || "")}">✗ ${esc((msg.error || "error").slice(0, 60))}</span>`;
        }
        $("#mprog-count").textContent = done;
        $("#mprog-total").style.width = `${(done / total) * 100}%`;
      } else if (msg.type === "result") {
        result = msg;
      } else if (msg.type === "error") {
        throw new Error(msg.message);
      }
    });
    clearInterval(timer);
    if (!result) throw new Error("The triage stream ended before the result arrived.");
    renderSingle(result);
  } catch (e) {
    $("#single-status").innerHTML = `<span class="err">${esc(e.message)}</span>`;
  } finally {
    clearInterval(timer);
    btn.disabled = false;
  }
}

// Two zones split at the auto-assign threshold: below = needs human review, at/above = automatic.
function gauge(label, value, threshold) {
  const pos = value == null ? null : Math.max(0, Math.min(1, value));
  const human = threshold * 100, auto = 100 - human;
  return `<div class="small"><b>${label}</b>: ${value == null ? "—" : value.toFixed(2)} <span class="muted">(auto ≥ ${threshold.toFixed(2)})</span></div>
    <div class="gauge">
      <div class="z" style="width:${human}%;background:var(--warn-soft)"></div>
      <div class="z" style="width:${auto}%;background:var(--ok-soft)"></div>
      ${pos == null ? "" : `<div class="marker" style="left:calc(${pos * 100}% - 1px)"></div>`}
    </div>
    <div class="gauge-labels small muted gauge-2">
      <span style="width:${human}%">0 · needs human</span><span style="width:${auto}%">automatic · 1</span></div>`;
}

const REVIEW_MODE_LABEL = {
  default: "default (team + confidence + critical urgency)",
  team: "team only (team probability, tie, team confidence)",
  urgency: "urgency only (critical, urgency probability, urgency confidence)",
};

function verdictHtml(run) {
  if (!run) return "";
  const d = run.decision;
  if (run.error && !run.result) {
    return `<div class="verdict error"><div class="title">REQUIRES HUMAN INTERVENTION</div><ul><li>${esc(run.error)}</li></ul></div>`;
  }
  const cls = d && d.automatic ? "auto" : "human";
  const title = d && d.automatic ? `AUTOMATIC ASSIGNMENT → ${esc(run.result.team)}` : "REQUIRES HUMAN INTERVENTION";
  const reasons = d && d.reasons.length ? `<ul>${d.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`
    : `<ul><li>High team probability and high confidence.</li></ul>`;
  const modeNote = d && d.mode ? `<div class="small" style="margin-top:6px;color:var(--muted)">Human review mode: <b>${esc(REVIEW_MODE_LABEL[d.mode] || d.mode)}</b></div>` : "";
  return `<div class="verdict ${cls}"><div class="title">${title}</div>${reasons}${modeNote}</div>`;
}

// colour by position: most severe rank = critical style, then high, medium, and low for the rest
const SEV_STYLES = ["u-critical", "u-high", "u-medium", "u-low"];
const SEV_FILLS = ["var(--bad-soft)", "var(--warn-soft)", "var(--accent-soft)", "var(--bg)"];
const sevIdx = (name) => Math.min(Math.max(SEV_RANKS.findIndex((r) => r.name === name), 0), SEV_STYLES.length - 1);
const sevText = (x) => (x && x.severity_score != null ? `${x.severity_level} · ${x.severity_score}` : "—");
const sevBadge = (x) => (x && x.severity_score != null
  ? `<span class="badge ${SEV_STYLES[sevIdx(x.severity_level)]}" title="${esc(x.severity_label || "")}">${esc(sevText(x))}</span>` : "");
const sevFull = (x) => (x && x.severity_score != null
  ? `${x.severity_level}${x.severity_label ? " — " + x.severity_label : ""} (score ${x.severity_score})` : "—");

// Big-number summary of jev's conclusions, each with the text that explains the number.
function kpiTiles(run, thrP, thrC) {
  const x = run && run.result;
  if (!x) return "";
  const d = run.decision || {};
  const pct = (v) => `${Math.round(v * 100)}%`;
  const zone = (v, thr) => (v >= thr ? "ok" : "warn"); // same two zones as the gauges: automatic / needs human

  const probs = Object.entries(x.team_probabilities || {}).sort((a, b) => b[1] - a[1]);
  const runnerUp = probs.find(([t]) => t !== x.team);
  const topProb = d.top_prob != null ? d.top_prob : (x.team_probabilities || {})[x.team];
  const teamText = topProb == null ? "No team probabilities returned."
    : `${pct(topProb)} probability for this team${runnerUp ? `; next: ${runnerUp[0]} (${pct(runnerUp[1])})` : ""}. `
      + (topProb >= thrP ? `Meets the ${pct(thrP)} auto-assign threshold.` : `Below the ${pct(thrP)} auto-assign threshold.`)
      + (d.prob_source === "decision model" ? " Probabilities computed by the decision model." : " Probabilities self-reported by the model.");

  let confText = (x.confidence >= thrC ? `Meets the ${pct(thrC)} auto-assign threshold. ` : `Below the ${pct(thrC)} auto-assign threshold. `)
    + (run.api === "decisions" ? "The lower of jev's confidence in the team and in the urgency." : "The model's confidence in the overall triage (team + urgency).");

  const urgWhy = x.urgency_reason && x.urgency_reason !== x.urgency_meaning ? ` <b>Why:</b> ${esc(x.urgency_reason)}` : "";
  const urgText = `${esc(x.urgency_meaning || "")}${urgWhy}${x.urgency === "critical" ? " Critical tickets always go to human review." : ""}`;

  const rank = SEV_RANKS.find((r) => r.name === x.severity_level);
  const sevText = x.severity_score == null ? "No severity score returned."
    : `${esc(x.severity_label || "")}${rank ? ` (score ${rank.min_score}–${rank.max_score}): ${esc(rank.description)}` : ""}`
      + (x.severity_reason ? ` <b>Why:</b> ${esc(x.severity_reason)}` : "") + " Informative only.";

  const pEsc = x.escalate_probability;
  const escText = (pEsc != null ? `${pct(pEsc)} probability that it should be escalated (yes from 50%). ` : "")
    + (x.escalate ? "Should go beyond the normal support queue: on-call engineering, a senior agent or management. "
                  : "Can be handled in the normal support queue. ")
    + "Doesn't change the automatic / human-review decision.";

  // Decision-model question type behind each tile (choice / score / noul), read from the request actually sent.
  const questions = run.api === "decisions" && run.request ? run.request.questions || {} : {};
  const TYPE_HELP = {
    choice: "choice: picks one option and returns a probability for each option, plus a confidence",
    score: "score: places the ticket on an ordered scale and returns a probability for each level, plus a confidence",
    noul: "noul: yes/no question; returns the probability of yes",
  };
  const qtype = (name) => {
    const t = questions[name] && questions[name].type;
    return t ? `<span class="qtype qtype-${esc(t)}" title="${esc(TYPE_HELP[t] || t)}">${esc(t)}</span>` : "";
  };
  const derived = run.api === "decisions"
    ? `<span class="qtype" title="Not a question: the lower of the confidences jev returned for the team and urgency choices">from choice</span>` : "";

  // jev's own confidence for the question behind a tile (choice/score answers; noul answers have none)
  const jevConf = (v, note = "") => (v == null ? (note ? `<div class="kpi-conf muted">${note}</div>` : "")
    : `<div class="kpi-conf" title="Confidence jev returned for this answer"><span>jev confidence <b>${pct(v)}</b></span>
        <div class="kpi-conf-track"><div class="kpi-conf-fill kpi-conf-${zone(v, thrC)}" style="width:${v * 100}%"></div></div></div>`);
  const isDec = run.api === "decisions";
  if (isDec && x.team_confidence != null && x.urgency_confidence != null) {
    confText = `Lower of jev's confidence in the team (${pct(x.team_confidence)}) and in the urgency (${pct(x.urgency_confidence)}). `
      + (x.confidence >= thrC ? `Meets the ${pct(thrC)} auto-assign threshold.` : `Below the ${pct(thrC)} auto-assign threshold.`);
  }

  const tile = (label, valueHtml, cls, text, tag = "", conf = "") => `<div class="kpi ${cls}">
      <div class="kpi-label">${label}${tag}</div><div class="kpi-value">${valueHtml}</div>${conf}<div class="kpi-text">${text}</div></div>`;

  return `<div class="kpis">
    ${tile("Team", esc(x.team), "kpi-team", esc(teamText), qtype("team"), jevConf(x.team_confidence))}
    ${tile("Confidence", pct(x.confidence), `kpi-${zone(x.confidence, thrC)}`, esc(confText), derived)}
    ${tile("Urgency", esc(x.urgency.toUpperCase()), `kpi-u-${esc(x.urgency)}`, urgText, qtype("urgency"), jevConf(x.urgency_confidence))}
    ${tile("Severity", x.severity_score == null ? "—" : `${esc(x.severity_level)} <span class="kpi-sub">${x.severity_score}/100</span>`,
      x.severity_score == null ? "" : `kpi-sev-${sevIdx(x.severity_level)}`, sevText, qtype("severity"), jevConf(x.severity_confidence))}
    ${tile("Escalate", `${x.escalate ? "YES" : "NO"}${pEsc != null ? ` <span class="kpi-sub">${pct(pEsc)}</span>` : ""}`,
      x.escalate ? "kpi-bad" : "kpi-ok", esc(escText), qtype("escalate"),
      jevConf(null, isDec ? "noul answers have no confidence: the % is P(yes)" : ""))}
  </div>`;
}

function urgencyLegend(current) {
  if (!URGENCY.length) return "";
  const rows = URGENCY.map((u) => `<tr class="${u.name === current ? "sev-current" : ""}">
      <td><span class="badge u-${esc(u.name)}">${esc(u.name)}</span></td><td class="wrap">${esc(u.description)}</td></tr>`).join("");
  return `<div class="table-wrap"><table class="sev-legend"><thead><tr><th>Urgency</th><th>Meaning</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function urgencySection(x) {
  if (!x) return "";
  const probs = x.urgency_probabilities || {};
  const bars = Object.keys(probs).length
    ? `<div class="probs small" style="margin-top:6px">${URGENCY.map((u) => u.name).filter((n) => n in probs).map((n) => `<span>${esc(n)}</span>
        <div class="track"><div class="fill ${n === x.urgency ? "top" : ""}" style="width:${probs[n] * 100}%"></div></div>
        <span class="num">${fmtPct(probs[n])}</span>`).join("")}</div>` : "";
  return `<div class="small"><b>Urgency</b>: <span class="badge u-${esc(x.urgency)}">${esc(x.urgency)}</span>
      ${x.urgency_meaning ? `<span class="muted">${esc(x.urgency_meaning)}</span>` : ""}</div>
    ${x.urgency_reason && x.urgency_reason !== x.urgency_meaning ? `<div class="small" style="margin-top:4px"><b>Why:</b> ${esc(x.urgency_reason)}</div>` : ""}
    ${bars}
    <details class="small" style="margin-top:6px"><summary>Urgency levels</summary>${urgencyLegend(x.urgency)}</details>`;
}

function severityLegend(currentName) {
  if (!SEV_RANKS.length) return "";
  const rows = SEV_RANKS.map((r) => `<tr class="${r.name === currentName ? "sev-current" : ""}">
      <td><span class="badge ${SEV_STYLES[sevIdx(r.name)]}">${esc(r.name)}</span></td>
      <td class="num">${r.min_score}–${r.max_score}</td><td><b>${esc(r.label)}</b></td><td class="wrap">${esc(r.description)}</td></tr>`).join("");
  return `<div class="table-wrap"><table class="sev-legend"><thead><tr><th>Rank</th><th class="num">Score</th><th>Label</th><th>Meaning</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function severityGauge(x) {
  const v = x ? x.severity_score : null;
  const asc = [...SEV_RANKS].reverse(); // least severe first, left to right
  const zones = asc.map((r) => `<div class="z" style="width:${((r.max_score + 1 - r.min_score) / 101) * 100}%;background:${SEV_FILLS[sevIdx(r.name)]}"></div>`).join("");
  const labels = asc.map((r) => `<span style="width:${((r.max_score + 1 - r.min_score) / 101) * 100}%">${esc(r.name)}</span>`).join("");
  return `<div class="small"><b>Severity</b>: ${esc(sevFull(x))} <span class="muted">(informative, does not affect assignment)</span></div>
    <div class="gauge">${zones}${v == null ? "" : `<div class="marker" style="left:calc(${v}% - 1px)"></div>`}</div>
    <div class="gauge-labels sev-labels small muted">${labels}</div>
    ${x && x.severity_reason ? `<div class="small muted">${esc(x.severity_reason)}</div>` : ""}
    <details class="small" style="margin-top:6px"><summary>Severity ranks</summary>${severityLegend(x ? x.severity_level : null)}</details>`;
}

function probBars(probs, chosen) {
  const entries = Object.entries(probs || {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return '<p class="muted small">No team probabilities returned.</p>';
  return `<div class="probs small">${entries.map(([t, p]) => `<span>${esc(t)}</span>
    <div class="track"><div class="fill ${t === chosen ? "top" : ""}" style="width:${p * 100}%"></div></div>
    <span class="num">${fmtPct(p)}</span>`).join("")}</div>`;
}

function bestIdx(runs, getter) {
  let best = null, idx = -1;
  runs.forEach((r, i) => {
    const v = getter(r);
    if (v != null && v > 0 && (best == null || v < best)) { best = v; idx = i; }
  });
  return idx;
}

function renderSingle(data) {
  const runs = data.runs;
  const jev = runs.find((r) => r.is_jev) || runs[0];
  const thrP = CFG ? CFG.auto_min_prob : 0.8, thrC = CFG ? CFG.auto_min_confidence : 0.8;

  const bLat = bestIdx(runs, (r) => r.metrics.latency_s);
  const bTtft = bestIdx(runs, (r) => r.metrics.ttft_s);
  const bTok = bestIdx(runs, (r) => r.metrics.total_tokens);
  const bCost = bestIdx(runs, (r) => r.metrics.cost_usd);
  const mark = (i, b, txt) => (i === b ? `<span class="best">${txt}</span>` : txt);
  const agree = (v) => (v == null ? "—" : v ? "✓" : "✗");

  const rows = runs.map((r, i) => {
    const m = r.metrics;
    const dec = r.decision ? (r.decision.automatic ? '<span class="badge auto">auto</span>' : '<span class="badge human">human</span>') : "—";
    return `<tr class="${r.is_jev ? "jev" : ""}">
      <td>${esc(r.model)}${r.is_jev ? " <b>(jev)</b>" : ""}${r.api === "decisions" ? ' <span class="badge">decisions</span>' : ""}</td>
      <td>${r.result ? esc(r.result.team) : `<span class="err">error</span>`}</td>
      <td>${r.result ? esc(r.result.urgency) : "—"}</td>
      <td>${r.result ? sevBadge(r.result) || "—" : "—"}</td>
      <td>${dec}</td>
      <td class="num">${mark(i, bLat, fmtS(m.latency_s))}</td>
      <td class="num">${mark(i, bTtft, fmtS(m.ttft_s))}</td>
      <td class="num">${fmtInt(m.prompt_tokens)}</td>
      <td class="num">${fmtInt(m.completion_tokens)}</td>
      <td class="num">${mark(i, bTok, fmtInt(m.total_tokens))}</td>
      <td class="num">${mark(i, bCost, fmtUsd(m.cost_usd))}${m.cost_source === "pricing" ? "*" : ""}</td>
      <td>${r.is_jev ? "—" : agree(r.agrees_team) + " / " + agree(r.agrees_urgency)}</td></tr>`;
  }).join("");

  const skills = data.skills.length ? data.skills.map((s) => `<span class="badge">${esc(s)}</span>`).join(" ") : '<span class="muted">none matched</span>';

  $("#single-result").innerHTML = `
    <div class="card jev-decision">
      <h2>jev decision</h2>
      ${kpiTiles(jev, thrP, thrC)}
      <div class="decision">
        <div>${verdictHtml(jev)}<p class="small" style="margin-top:10px">Skills applied: ${skills}</p></div>
        <div>
          ${gauge("Team probability", jev.decision ? jev.decision.top_prob : null, thrP)}
          ${jev.decision && jev.decision.prob_source === "logprobs+self-reported" ? '<div class="small muted">Probability combines logprobs and self-reported values (lower of the two).</div>' : ""}
          ${jev.decision && jev.decision.prob_source === "decision model" ? '<div class="small muted">Probabilities and confidence come directly from the decision model (not self-reported).</div>' : ""}
          <div style="height:10px"></div>
          ${gauge("Confidence", jev.result ? jev.result.confidence : null, thrC)}
          <div style="height:10px"></div>
          ${urgencySection(jev.result)}
          <div style="height:10px"></div>
          ${severityGauge(jev.result)}
          <div style="height:12px"></div>
          ${jev.result ? probBars(jev.result.team_probabilities, jev.result.team) : ""}
        </div>
      </div>
    </div>
    <div class="card">
      <h2>Comparison</h2>
      <div class="table-wrap"><table>
        <thead><tr><th>Model</th><th>Team</th><th>Urgency</th><th>Severity</th><th>Decision</th><th class="num">Latency</th><th class="num">TTFT</th>
          <th class="num">In tok</th><th class="num">Out tok</th><th class="num">Total tok</th><th class="num">Cost</th><th>Agrees w/ jev (team / urgency)</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      <p class="small muted">★ = best in column. * = cost calculated from the model's list price (OpenRouter didn't return a cost).</p>
    </div>
    <div class="cards">${runs.map(modelCard).join("")}</div>
    ${requestCard(runs, data.skills)}`;
}

function modelCard(r) {
  const head = `<div class="head"><span class="model">${esc(r.model)}</span>${r.is_jev ? '<span class="badge team">jev</span>' : ""}</div>`;
  if (!r.result) return `<div class="mcard ${r.is_jev ? "jev" : ""}">${head}<p class="err">${esc(r.error || "No result")}</p></div>`;
  const x = r.result;
  const d = r.decision;
  return `<div class="mcard ${r.is_jev ? "jev" : ""}">${head}
    <div class="badges">
      <span class="badge team">${esc(x.team)}</span>
      <span class="badge u-${esc(x.urgency)}">${esc(x.urgency)}</span>
      ${sevBadge(x)}
      ${d ? `<span class="badge ${d.automatic ? "auto" : "human"}">${d.automatic ? "automatic" : "human review"}</span>` : ""}
      ${x.escalate ? '<span class="badge u-critical">escalate</span>' : ""}
      ${x.category ? `<span class="badge">${esc(x.category)}</span>` : ""}
      ${x.sentiment ? `<span class="badge">${esc(x.sentiment)}</span>` : ""}
      <span class="badge">conf ${x.confidence.toFixed(2)}</span>
      ${x.escalate_probability != null ? `<span class="badge">P(escalate) ${fmtPct(x.escalate_probability)}</span>` : ""}
    </div>
    ${x.summary ? `<p>${esc(x.summary)}</p>` : ""}
    <p class="small muted"><b>Urgency: ${esc(x.urgency)}</b>${x.urgency_meaning ? ` (${esc(x.urgency_meaning)})` : ""}${
      x.urgency_reason && x.urgency_reason !== x.urgency_meaning ? `<br><b>Why:</b> ${esc(x.urgency_reason)}` : ""}</p>
    ${x.severity_score != null ? `<p class="small muted"><b>Severity:</b> ${esc(sevFull(x))}${x.severity_reason ? ". " + esc(x.severity_reason) : ""}</p>` : ""}
    ${r.api === "decisions" ? '<p class="small muted">Decision model: answers with probabilities only, so there is no summary, suggested reply or next steps.</p>' : ""}
    ${x.suggested_reply ? `<h3>Suggested reply</h3><div class="reply small">${esc(x.suggested_reply)}</div>` : ""}
    ${x.next_steps.length ? `<h3>Next steps</h3><ul class="small">${x.next_steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>` : ""}
  </div>`;
}

/* ---------- batch ---------- */
async function runSample() {
  const text = await fetch("/api/sample").then((r) => r.text());
  await runBatch(new Blob([text], { type: "text/csv" }), "tickets.csv");
}

async function runBatchFromFile() {
  const f = $("#f-csv").files[0];
  if (!f) { $("#progress-text").textContent = "Choose a CSV file first."; $("#batch-progress").hidden = false; return; }
  await runBatch(f, f.name);
}

async function runBatch(blob, name) {
  const buttons = ["#btn-batch", "#btn-sample", "#btn-download"].map((s) => $(s));
  buttons.forEach((b) => (b.disabled = true));
  $("#batch-result").innerHTML = "";
  $("#batch-progress").hidden = false;
  $("#progress-bar").style.width = "0";
  $("#progress-text").textContent = "Uploading…";
  const state = { total: 0, done: 0, models: [], tickets: [], summary: null };

  try {
    const fd = new FormData();
    fd.append("file", blob, name);
    const r = await fetch(`/api/batch?review_mode=${encodeURIComponent($("#f-review-batch").value)}`, { method: "POST", body: fd });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    const started = performance.now();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        const msg = JSON.parse(line);
        if (msg.type === "start") { state.total = msg.total; state.models = msg.models; }
        else if (msg.type === "ticket") { state.tickets[msg.index] = msg; state.done++; }
        else if (msg.type === "summary") { state.summary = msg; }
        const pct = state.total ? state.done / state.total : 0;
        $("#progress-bar").style.width = `${pct * 100}%`;
        $("#progress-text").textContent = `${state.done} / ${state.total} tickets · ${state.models.length} models · ${((performance.now() - started) / 1000).toFixed(0)} s`;
      }
    }
    if (!state.summary) throw new Error("The batch stream ended before the summary arrived.");
    lastBatch = state;
    renderBatch(state);
    $("#btn-download").disabled = false;
  } catch (e) {
    $("#progress-text").innerHTML = `<span class="err">${esc(e.message)}</span>`;
  } finally {
    $("#btn-batch").disabled = false;
    $("#btn-sample").disabled = false;
  }
}

function barChart(title, items, fmt) {
  const vals = items.filter((i) => i.value != null);
  if (!vals.length) return "";
  const max = Math.max(...vals.map((i) => i.value)) || 1;
  const rowH = 24, labelW = 130, w = 380, barW = w - labelW - 80;
  const h = items.length * rowH + 6;
  const rows = items.map((it, i) => {
    const y = i * rowH + 4;
    const bw = it.value == null ? 0 : Math.max(2, (it.value / max) * barW);
    return `<text class="lbl" x="${labelW - 6}" y="${y + 14}" text-anchor="end">${esc(shortModel(it.label).slice(0, 20))}</text>
      <rect x="${labelW}" y="${y + 3}" width="${bw}" height="15" rx="3" fill="${it.jev ? "var(--accent)" : "var(--bar)"}"><title>${esc(it.label)}</title></rect>
      <text x="${labelW + bw + 6}" y="${y + 14}">${it.value == null ? "—" : esc(fmt(it.value))}</text>`;
  }).join("");
  return `<div class="card chart"><h3>${esc(title)}</h3><svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(title)}">${rows}</svg></div>`;
}

function renderBatch(state) {
  const s = state.summary;
  const ms = s.models;
  const best = (key, higher = false) => {
    const vals = ms.map((m) => m[key]).filter((v) => v != null);
    if (!vals.length) return null;
    return higher ? Math.max(...vals) : Math.min(...vals);
  };
  const cell = (m, key, fmt, higher = false) => {
    const b = best(key, higher);
    const txt = fmt(m[key]);
    return `<td class="num">${m[key] != null && m[key] === b && ms.length > 1 ? `<span class="best">${txt}</span>` : txt}</td>`;
  };

  const accHead = s.labeled_team ? '<th class="num">Team acc.</th><th class="num">Acc. when auto</th><th class="num">Cost / correct</th>' : "";
  const urgHead = s.labeled_urgency ? '<th class="num">Urgency acc.</th><th class="num">Urgency ±1</th>' : "";
  const sevHead = s.labeled_severity ? '<th class="num">Severity MAE</th><th class="num">SEV level acc.</th>' : "";
  const fmtSev = (v) => (v == null ? "—" : v.toFixed(1));
  const rows = ms.map((m) => `<tr class="${m.is_jev ? "jev" : ""}">
      <td>${esc(m.model)}${m.is_jev ? " <b>(jev)</b>" : ""}</td>
      ${cell(m, "avg_latency_s", fmtS)}${cell(m, "p95_latency_s", fmtS)}${cell(m, "avg_ttft_s", fmtS)}${cell(m, "p95_ttft_s", fmtS)}
      ${cell(m, "total_tokens", fmtInt)}${cell(m, "total_cost_usd", fmtUsd)}${cell(m, "cost_per_1k_usd", fmtUsd)}
      ${cell(m, "auto_rate", fmtPct, true)}
      <td class="num">${m.errors}</td>
      <td class="num">${m.is_jev ? "—" : fmtPct(m.agree_team_with_jev) + " / " + fmtPct(m.agree_urgency_with_jev)}</td>
      <td class="num">${fmtSev(m.avg_severity)}</td>
      <td class="num">${m.is_jev ? "—" : fmtSev(m.severity_diff_vs_jev)}</td>
      ${s.labeled_team ? cell(m, "team_accuracy", fmtPct, true) + `<td class="num">${fmtPct(m.auto_accuracy)} <span class="muted">(n=${m.auto_count})</span></td>` + cell(m, "cost_per_correct_usd", fmtUsd) : ""}
      ${s.labeled_urgency ? cell(m, "urgency_accuracy", fmtPct, true) + cell(m, "urgency_within_one", fmtPct, true) : ""}
      ${s.labeled_severity ? cell(m, "severity_mae", fmtSev) + cell(m, "severity_level_accuracy", fmtPct, true) : ""}
    </tr>`).join("");

  const items = (key) => ms.map((m) => ({ label: m.model, value: m[key], jev: m.is_jev }));
  const charts = [
    barChart("Avg latency (s)", items("avg_latency_s"), (v) => v.toFixed(2)),
    barChart("Avg time to first token (s)", items("avg_ttft_s"), (v) => v.toFixed(2)),
    barChart("Cost per 1,000 tickets (USD)", items("cost_per_1k_usd"), (v) => "$" + v.toFixed(3)),
    barChart("Total tokens", items("total_tokens"), (v) => Math.round(v).toLocaleString()),
    s.labeled_team ? barChart("Team accuracy", items("team_accuracy"), (v) => Math.round(v * 100) + "%") : "",
    barChart("Auto-assigned", items("auto_rate"), (v) => Math.round(v * 100) + "%"),
    barChart("Avg severity (0–100)", items("avg_severity"), (v) => v.toFixed(1)),
  ].join("");

  let confusion = "";
  if (s.jev_confusion) {
    const { labels, matrix } = s.jev_confusion;
    const head = labels.map((l) => `<th class="num">${esc(l)}</th>`).join("");
    const body = Object.entries(matrix).map(([exp, row]) => `<tr><th>${esc(exp)}</th>${labels.map((l) => {
      const v = row[l];
      const cls = v ? (l === exp ? "hit" : "miss") : "";
      return `<td class="num ${cls}">${v || ""}</td>`;
    }).join("")}</tr>`).join("");
    confusion = `<div class="card"><h2>jev confusion matrix</h2><p class="small muted">Rows = expected team, columns = jev's predicted team.</p>
      <div class="table-wrap"><table class="matrix"><thead><tr><th></th>${head}</tr></thead><tbody>${body}</tbody></table></div></div>`;
  }

  const ticketRows = state.tickets.map((t, i) => {
    const jev = t.runs.find((r) => r.is_jev) || t.runs[0];
    const exp = t.ticket.expected_team;
    const ok = exp && jev.result ? (jev.result.team.toLowerCase() === exp.toLowerCase() ? "✓" : "✗") : "";
    const others = t.runs.filter((r) => !r.is_jev).map((r) => `<span title="${esc(r.model)}">${r.result ? esc(r.result.team) : '<span class="err">error</span>'}</span>`).join("<br>");
    return `<tr><td class="num">${i + 1}</td><td>${esc(t.ticket.subject)}</td>
      <td>${jev.result ? esc(jev.result.team) : `<span class="err" title="${esc(jev.error)}">error</span>`} ${ok}</td>
      <td>${jev.result ? esc(jev.result.urgency) : "—"}${t.ticket.expected_urgency ? ` <span class="muted">(exp ${esc(t.ticket.expected_urgency)})</span>` : ""}</td>
      <td>${jev.result ? sevBadge(jev.result) || "—" : "—"}${t.ticket.expected_severity != null ? ` <span class="muted">(exp ${t.ticket.expected_severity})</span>` : ""}</td>
      <td>${jev.decision ? (jev.decision.automatic ? '<span class="badge auto">auto</span>' : `<span class="badge human" title="${esc(jev.decision.reasons.join("; "))}">human</span>`) : "—"}</td>
      <td class="small">${esc(t.skills.join(", "))}</td>
      <td class="small">${others}</td></tr>`;
  }).join("");

  $("#batch-result").innerHTML = `
    <div class="card"><h2>Summary · ${state.total} tickets</h2>
      <div class="table-wrap"><table><thead><tr><th>Model</th>
        <th class="num help" title="Average time per request, from sending it to receiving the full answer.">Avg latency</th>
        <th class="num help" title="95% of requests finished within this time; only the slowest 5% took longer. Shows the worst case users actually hit, which the average hides.">p95 latency</th>
        <th class="num help" title="Average time until the model starts answering (time to first token). Not available for jev: the Decisions API doesn't stream.">Avg TTFT</th>
        <th class="num help" title="95% of requests started answering within this time; only the slowest 5% took longer.">p95 TTFT</th><th class="num">Tokens</th><th class="num">Total cost</th>
        <th class="num">Cost / 1k</th><th class="num">Auto</th><th class="num">Errors</th><th class="num">Agree w/ jev (team / urg.)</th><th class="num">Avg severity</th><th class="num">|Δ| severity vs jev</th>${accHead}${urgHead}${sevHead}</tr></thead>
        <tbody>${rows}</tbody></table></div>
      <p class="small muted">★ = best in column. <b>p95</b> = 95% of tickets were at least this fast; the slowest 5% took longer.
        A p95 far above the average means the model is occasionally very slow. "Acc. when auto" = team accuracy on the tickets the model would have auto-assigned; this is the number to watch when tuning the thresholds.</p>
    </div>
    <div class="charts">${charts}</div>
    ${confusion}
    <div class="card"><details><summary><b>Per-ticket results</b></summary>
      <div class="table-wrap"><table><thead><tr><th class="num">#</th><th>Subject</th><th>jev team</th><th>jev urgency</th><th>jev severity</th><th>Decision</th><th>Skills</th><th>Other models' team</th></tr></thead>
      <tbody>${ticketRows}</tbody></table></div></details></div>`;
}

function downloadCsv() {
  if (!lastBatch) return;
  const cols = ["index", "subject", "customer_tier", "expected_team", "expected_urgency", "skills", "model", "api", "is_jev", "team", "urgency",
    "severity_score", "severity_level", "severity_reason", "expected_severity", "confidence", "top_prob", "automatic", "review_reasons", "category", "sentiment", "escalate", "summary", "suggested_reply",
    "latency_s", "ttft_s", "prompt_tokens", "completion_tokens", "total_tokens", "cost_usd", "error"];
  const q = (v) => {
    const s = v == null ? "" : String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [cols.join(",")];
  lastBatch.tickets.forEach((t, i) => t.runs.forEach((r) => {
    const x = r.result || {}, m = r.metrics, d = r.decision || {};
    lines.push([i + 1, t.ticket.subject, t.ticket.customer_tier, t.ticket.expected_team, t.ticket.expected_urgency, t.skills.join("; "),
      r.model, r.api, r.is_jev, x.team, x.urgency, x.severity_score, x.severity_level, x.severity_reason, t.ticket.expected_severity, x.confidence, d.top_prob, d.automatic, (d.reasons || []).join("; "), x.category, x.sentiment,
      x.escalate, x.summary, x.suggested_reply, m.latency_s, m.ttft_s, m.prompt_tokens, m.completion_tokens, m.total_tokens, m.cost_usd, r.error].map(q).join(","));
  }));
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `jev-batch-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

init();
