// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/* Council Chamber — UI driver.
 *
 * This script is purely presentational:
 *   1. Calls our own /api/* endpoints, which in turn call the live
 *      council_authorize MCP tool and the live trusted executor contract.
 *   2. Renders whatever the council returns. The only "animation"
 *      applied to council output is a typewriter reveal of the
 *      reasoning text — the text itself is the real response.
 *
 * No outcomes are hardcoded. If the council changes its mind about a
 * scripted scenario, the UI will show the new verdict faithfully.
 */

// ── Constants / state ────────────────────────────────────────────────

const MEMBER_DISPLAY = {
  "command-integrity": { label: "Command Integrity", sub: "WHAT is being executed" },
  "environment-isolation": { label: "Environment Isolation", sub: "WHERE it runs" },
  "blast-containment": { label: "Blast Containment", sub: "WHAT happens if it fails" },
};

const CODE_REVIEW_DISPLAY = {
  label: "Code Review",
  sub: "WHAT the packaged file contains",
};

const PIPELINE_STAGES = [
  "idle",
  "authenticate",
  "categorize",
  "chairman",
  "vote",
  "mint",
  "executor",
];

let currentSilos = []; // [{name, scenarios: [...]}]
let activeScenarioId = null;
let lastAuthResult = null;
let lastAuthAgentId = null; // agent that submitted the last successful authorize
let certifiedEvidence = []; // executor-certified results attached to the next council request
let pendingCertifiedFollowupScenario = null;
let availableAgents = []; // [{id, has_token}] from /api/config
let defaultAgentId = "";
let loadedIntent = null; // the template last loaded into the intent card (used for Reset)
let loadedScenario = null; // raw scenario record (retained across rounds so the retry can be built from .followup)
let currentTurn = 1; // 1 = first attempt; 2 = follow-up after a denial in a multi-turn scenario
let sessionId = ""; // short opaque id the server issued us; shown in the header pill

// Blank template for the Custom card — same shape as a Scenario. `agent_id`
// is left empty so renderIntent picks up whichever agent the user selected
// (defaults to server-configured default).
const CUSTOM_BLANK = {
  id: "__custom__",
  title: "Custom intent",
  subtitle: "Compose your own intent; press Run to submit it to the live council.",
  expected: "your call",
  command: "",
  description: "",
  target_resource: "",
  justification: "",
  blast_radius: "",
  target_environment: "development",
  packaged_files: [],
  agent_id: "",
};

function clearCertifiedEvidenceState() {
  certifiedEvidence = [];
  pendingCertifiedFollowupScenario = null;
}

// ── DOM refs ─────────────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);

// ── Boot ─────────────────────────────────────────────────────────────

(async function boot() {
  await Promise.all([loadConfig(), loadHealth(), loadScenarios()]);
  wireCustomCard();
  wireRunButton();
  wireResetButton();
  wireExecuteButton();
  wireIntroButton();
  wireEnvSelect();
  wireAgentSelect();
  resetStage();
})();

function wireEnvSelect() {
  // Keep the environment pill color in sync with the selected value.
  const el = $("intent-env");
  if (!el) return;
  el.addEventListener("change", () => el.setAttribute("data-env", el.value));
}

function syncAgentEyebrow() {
  const sel = $("intent-agent-select");
  const eyebrow = $("intent-agent");
  if (sel && eyebrow) eyebrow.textContent = sel.value || "—";
}

function wireAgentSelect() {
  const el = $("intent-agent-select");
  if (!el) return;
  el.addEventListener("change", () => {
    syncAgentEyebrow();
    // Persist the choice for this session so a reload keeps the same agent.
    // Fire-and-forget: the server call isn't on the critical path for anything.
    fetchJson("/api/session/agent", { method: "POST", body: { agent_id: el.value || "" } })
      .catch((err) => console.warn("session agent persist failed", err));
  });
}

async function wireIntroButton() {
  const btn = document.getElementById("intro-btn");
  if (!btn) return;
  // Lazy-load the tour module — nothing happens unless the user clicks.
  // Cache-bust with a timestamp: ES module dynamic imports are cached
  // once-resolved per URL, so any edits to tour.js would otherwise be
  // invisible to an already-loaded page.
  btn.addEventListener("click", async () => {
    const mod = await import(`/static/tour.js?v=${Date.now()}`);
    mod.startTour();
  });
}

async function loadConfig() {
  try {
    const cfg = await fetchJson("/api/config");
    $("council-url").textContent = cfg.council_url;
    $("executor-url").textContent = cfg.executor_url;
    availableAgents = Array.isArray(cfg.agents) ? cfg.agents : [];
    defaultAgentId = cfg.default_agent_id || "";
    sessionId = cfg.session?.id || "";
    // Banner pill: show available agents summary.
    const ready = availableAgents.filter((a) => a.has_token).map((a) => a.id);
    const pill = $("agent-pill");
    if (pill) {
      pill.textContent = ready.length
        ? `agents: ${ready.length} configured (default: ${defaultAgentId || "—"})`
        : `agents: none configured`;
    }
    const sPill = $("session-pill");
    if (sPill && sessionId) sPill.textContent = `session: ${sessionId}`;
    populateAgentSelect();
  } catch (err) {
    console.error("config load failed", err);
  }
}

function populateAgentSelect() {
  const sel = $("intent-agent-select");
  if (!sel) return;
  sel.innerHTML = "";
  availableAgents.forEach((a) => {
    const opt = document.createElement("option");
    opt.value = a.id;
    opt.textContent = a.has_token ? a.id : `${a.id} (no token)`;
    if (!a.has_token) opt.disabled = true;
    sel.appendChild(opt);
  });
  if (defaultAgentId) sel.value = defaultAgentId;
}

async function loadHealth() {
  try {
    const h = await fetchJson("/api/health");
    setPill("council-pill", h.council.ok, h.council.ok
      ? `council · live (pk ${h.council.public_key_prefix}…)`
      : `council · unreachable`);
    setPill("executor-pill", h.executor.ok, h.executor.ok
      ? `executor · live (key ${h.executor.key_fingerprint}…)`
      : `executor · unreachable`);
  } catch (err) {
    setPill("council-pill", false, "council · error");
    setPill("executor-pill", false, "executor · error");
  }
}

function setPill(id, ok, label) {
  const el = $(id);
  el.className = "pill " + (ok ? "pill-ok" : "pill-bad");
  el.textContent = label;
}

async function loadScenarios() {
  const { silos } = await fetchJson("/api/scenarios");
  currentSilos = Array.isArray(silos) ? silos : [];
  renderScenarioRail();
}

function renderScenarioRail() {
  const container = $("silo-list");
  container.innerHTML = "";
  currentSilos.forEach((silo) => {
    const block = document.createElement("div");
    // Collapsed by default — viewer clicks the silo header to expand.
    block.className = "silo collapsed";
    block.dataset.silo = silo.name;

    const heading = document.createElement("button");
    heading.type = "button";
    heading.className = "silo-heading";
    heading.setAttribute("aria-expanded", "false");
    heading.innerHTML = `
      <span class="silo-chevron" aria-hidden="true">▸</span>
      <span class="silo-title">${escapeHtml(silo.name)}</span>
      <span class="silo-rule"></span>
      <span class="silo-count">${silo.scenarios.length}</span>
    `;
    heading.addEventListener("click", () => toggleSilo(block, heading));
    block.appendChild(heading);

    const list = document.createElement("ul");
    list.className = "scenario-list";
    silo.scenarios.forEach((s) => {
      const li = document.createElement("li");
      li.className = "scenario";
      li.dataset.id = s.id;
      li.innerHTML = `
        <p class="scenario-title">${escapeHtml(s.title)}</p>
        <p class="scenario-subtitle">${escapeHtml(s.subtitle)}</p>
        <p class="scenario-expected">expected: ${escapeHtml(s.expected)} · as ${escapeHtml(s.agent_id || "default")}</p>
      `;
      li.addEventListener("click", () => loadScenario(s.id));
      list.appendChild(li);
    });
    block.appendChild(list);
    container.appendChild(block);
  });
}

function toggleSilo(block, heading) {
  const willExpand = block.classList.contains("collapsed");
  block.classList.toggle("collapsed", !willExpand);
  heading.setAttribute("aria-expanded", String(willExpand));
}

function findScenario(id) {
  for (const silo of currentSilos) {
    for (const s of silo.scenarios) if (s.id === id) return s;
  }
  return null;
}

// ── Stage reset / pipeline ──────────────────────────────────────────

export function resetStage() {
  softResetResults();
  // Full reset — also clears the Intent editor back to the empty prompt.
  $("intent-title").textContent = "Pick a scenario or click Custom";
  $("intent-subtitle").innerHTML =
    'Clicking a card loads it here. Edit any field, then press <b>Run</b> to submit to the live council.';
  $("scenario-rationale").hidden = true;
  $("intent-meta").hidden = true;
  $("intent-fields").innerHTML =
    '<div class="intent-fields-placeholder" id="intent-fields-placeholder">' +
    "Pick a scenario on the left, or click <b>Custom</b>, to populate this card." +
    "</div>";
  $("intent-actions").hidden = true;
  loadedIntent = null;
  loadedScenario = null;
  currentTurn = 1;
  clearCertifiedEvidenceState();
}

function setPipeline(stage) {
  PIPELINE_STAGES.forEach((s) => {
    const el = document.querySelector(`.stage-node[data-stage="${s}"]`);
    if (!el) return;
    el.classList.remove("active", "done", "failed");
  });
  const idx = PIPELINE_STAGES.indexOf(stage);
  if (idx < 0) return;
  PIPELINE_STAGES.slice(0, idx).forEach((s) => {
    const el = document.querySelector(`.stage-node[data-stage="${s}"]`);
    if (el) el.classList.add("done");
  });
  const current = document.querySelector(`.stage-node[data-stage="${stage}"]`);
  if (current) current.classList.add("active");
}

function markPipelineFailed(stage) {
  const el = document.querySelector(`.stage-node[data-stage="${stage}"]`);
  if (el) {
    el.classList.remove("active", "done");
    el.classList.add("failed");
  }
}

// ── Loading an intent into the editor ───────────────────────────────
//
// Clicking a scenario card — or the Custom card — loads the intent into
// the editable Intent panel. It does NOT submit to the council. Running
// is an explicit second step (the Run button in the Intent card).

function loadScenario(id) {
  const sc = findScenario(id);
  if (!sc) return;
  activeScenarioId = id;
  loadedScenario = sc;
  currentTurn = 1;
  clearCertifiedEvidenceState();
  loadedIntent = buildTurnView(sc, 1);
  highlightActiveScenario();
  // Wipe prior run state (council row, verdict, executor) when the user
  // picks a new intent — otherwise stale results from the previous run
  // would hover above the newly-loaded intent card.
  softResetResults();
  renderIntent(loadedIntent);
}

function loadCustom() {
  activeScenarioId = "__custom__";
  loadedScenario = null;
  currentTurn = 1;
  clearCertifiedEvidenceState();
  loadedIntent = CUSTOM_BLANK;
  highlightActiveScenario();
  softResetResults();
  renderIntent(CUSTOM_BLANK);
}

// For multi-round scenarios (scenarios with a .followup block), build the
// intent-like object to render for the given round. Round 1 is the raw
// scenario. If an evidence probe exists, round 2 is the evidence command and
// round 3 overlays the followup's original-command retry fields. Without a
// probe, the followup is simply round 2.
function buildTurnView(scenario, turn) {
  if (turn === 1 || !scenario || !scenario.followup) return scenario;
  const f = scenario.followup;
  const retryRound = scenario.followup.evidence_probe ? 3 : 2;
  const certifiedNote = f.requires_certified_evidence
    ? " This round expects a signed ExecutionResult in certified_evidence[0]. The council renders that block to members as CERTIFIED EXECUTOR EVIDENCE; without it, the retry is still only agent-asserted prose."
    : "";
  return {
    ...scenario,
    command: f.command,
    description: f.description,
    target_resource: f.target_resource,
    justification: f.justification,
    blast_radius: f.blast_radius,
    target_environment: f.target_environment,
    packaged_files: Array.isArray(f.packaged_files)
      ? f.packaged_files
      : scenario.packaged_files,
    expected: f.expected || "approve",
    title: `${scenario.title} — round ${retryRound}`,
    subtitle:
      f.requires_certified_evidence
        ? "Original pool-increase command retried with executor-certified evidence."
        : "Agent came back with the evidence the council asked for on round 1.",
    rationale:
      `Round ${retryRound}. The agent returns to the original command after ` +
      "gathering the evidence the council flagged as missing. " +
      (f.revision_note || "") + certifiedNote,
    certified_evidence_note: f.certified_evidence_note || "",
  };
}

function buildEvidenceProbeView(scenario) {
  const probe = scenario?.followup?.evidence_probe;
  if (!probe) return null;
  const hasFinalRetry = !!scenario?.followup;
  return {
    ...probe,
    title: `${probe.title || "Collect certified evidence"} — round 2`,
    subtitle: hasFinalRetry
      ? "Evidence collection round. This is not the production pool increase."
      : probe.subtitle,
    rationale:
      "Round 2. The agent is not retrying the production mutation yet. " +
      "They are asking the council for a writ to run a read-only evidence " +
      "command in the trusted executor. The signed result from this round " +
      "will be attached to the next round's original pool-increase request. " +
      (probe.rationale || ""),
    agent_id: scenario.agent_id,
    expected: "approve",
    packaged_files: Array.isArray(probe.packaged_files) ? probe.packaged_files : [],
  };
}

function handleCollectCertifiedEvidence() {
  if (!loadedScenario || !loadedScenario.followup?.evidence_probe) return;
  pendingCertifiedFollowupScenario = loadedScenario;
  currentTurn = "evidence-probe";
  loadedIntent = buildEvidenceProbeView(loadedScenario);
  softResetResults();
  renderIntent(loadedIntent);
  document.getElementById("intent-card").scrollIntoView({
    behavior: "smooth",
    block: "start",
  });
}

function handleRetryWithFollowup() {
  if (!loadedScenario || !loadedScenario.followup) return;
  currentTurn = loadedScenario.followup.evidence_probe ? 3 : 2;
  loadedIntent = buildTurnView(loadedScenario, 2);
  softResetResults();
  renderIntent(loadedIntent);
  // Pull the viewer's eye back up to the intent editor so they can see
  // what the agent added before pressing Run again.
  document.getElementById("intent-card").scrollIntoView({
    behavior: "smooth",
    block: "start",
  });
}

function handleContinueToCertifiedRetry() {
  const scenario = pendingCertifiedFollowupScenario || loadedScenario;
  if (!scenario || !scenario.followup) return;
  loadedScenario = scenario;
  pendingCertifiedFollowupScenario = null;
  handleRetryWithFollowup();
}

function softResetResults() {
  // Like resetStage but preserves the Intent card contents — used when
  // switching between scenarios without wiping the editor.
  setPipeline("idle");
  $("council-row").innerHTML = "";
  const sidecar = $("sidecar-row");
  const sidecarSlot = $("sidecar-slot");
  if (sidecarSlot) sidecarSlot.innerHTML = "";
  if (sidecar) sidecar.hidden = true;
  $("chairman-card").hidden = true;
  const chairmanRisk = $("chairman-risk");
  chairmanRisk.textContent = "—";
  chairmanRisk.className = "chairman-risk";
  $("chairman-threshold").textContent = "—";
  $("chairman-reason").textContent = "—";
  $("verdict-card").hidden = true;
  $("executor-card").hidden = true;
  $("executor-result").classList.remove("has-actions");
  $("executor-result").textContent = "";
  Array.from(document.querySelectorAll(".verify-steps li")).forEach((li) => {
    li.classList.remove("pass", "fail");
  });
  $("execute-btn").disabled = true;
  lastAuthResult = null;
}

function highlightActiveScenario() {
  Array.from(document.querySelectorAll(".scenario")).forEach((el) => {
    el.classList.toggle("active", el.dataset.id === activeScenarioId);
  });
}

function wireCustomCard() {
  const el = document.querySelector('.scenario[data-id="__custom__"]');
  if (el) el.addEventListener("click", loadCustom);
}

function wireRunButton() {
  $("run-btn").addEventListener("click", async () => {
    const intent = getCurrentIntent();
    if (!intent.command.trim()) {
      flashField("command");
      return;
    }
    if (!intent.description || intent.description.trim().length < 10) {
      flashField("description", "needs 10+ characters");
      return;
    }
    // Preserve the loaded scenario's metadata (title, subtitle, rationale,
    // expected, silo) so the rationale callout and scenario framing survive
    // the re-render that happens during submission. Edited field values
    // from `intent` override the template fields.
    const base = loadedIntent || {
      title: "Custom intent",
      subtitle: "submitted live to council_authorize",
    };
    const intentLike = { ...base, ...intent };
    const body = { ...intent, agent_id: intent.agent_id || undefined };
    const packagedFiles = Array.isArray(intentLike.packaged_files)
      ? intentLike.packaged_files
      : [];
    if (packagedFiles.length) body.packaged_files = packagedFiles;
    if (certifiedEvidence.length) body.certified_evidence = certifiedEvidence;
    $("run-btn").disabled = true;
    try {
      await callAuthorize(body, intentLike);
    } finally {
      $("run-btn").disabled = false;
    }
  });
}

function wireResetButton() {
  $("reset-btn").addEventListener("click", () => {
    if (loadedIntent) renderIntent(loadedIntent);
  });
}

function getCurrentIntent() {
  const intent = {
    target_environment: $("intent-env").value || "development",
    agent_id: $("intent-agent-select").value || "",
  };
  document.querySelectorAll("#intent-fields [data-field]").forEach((el) => {
    intent[el.dataset.field] = el.value;
  });
  return intent;
}

function flashField(fieldName, msg) {
  const el = document.querySelector(`#intent-fields [data-field="${fieldName}"]`);
  if (!el) return;
  el.focus();
  el.style.borderColor = "var(--bad)";
  if (msg) el.setAttribute("placeholder", msg);
  setTimeout(() => {
    el.style.borderColor = "";
  }, 1500);
}

export function renderIntent(intentLike) {
  $("intent-title").textContent = intentLike.title || "Custom intent";
  $("intent-subtitle").innerHTML = intentLike.subtitle
    ? escapeHtml(intentLike.subtitle)
    : 'submitted live to <code>council_authorize</code>';

  // Agent selector: pick the intent's agent if it's configured, otherwise
  // leave the current selection alone. Eyebrow mirrors the selection.
  const agentSel = $("intent-agent-select");
  const desiredAgent = intentLike.agent_id || defaultAgentId;
  if (
    desiredAgent &&
    Array.from(agentSel.options).some((o) => o.value === desiredAgent && !o.disabled)
  ) {
    agentSel.value = desiredAgent;
  }
  syncAgentEyebrow();

  // Rationale callout — show only for scripted scenarios that provide one.
  const rationaleCard = $("scenario-rationale");
  const rationaleBody = $("scenario-rationale-body");
  const rationaleExpected = $("scenario-rationale-expected");
  if (intentLike.rationale) {
    rationaleBody.textContent = intentLike.rationale;
    const expected = (intentLike.expected || "").toLowerCase();
    rationaleExpected.textContent = expected ? `expected: ${expected}` : "";
    // Compound expected strings (e.g. "deny → approve" for multi-turn
    // scenarios) would produce an invalid CSS class — collapse them to a
    // dedicated "multi-turn" token so styling still applies.
    const expectedSlug = expected.includes("→") ? "multi-turn" : expected;
    rationaleExpected.className = "scenario-rationale-expected " + expectedSlug;
    rationaleCard.hidden = false;
  } else {
    rationaleCard.hidden = true;
  }

  // Environment select (agent + environment are the only editable pills;
  // action_type was removed — the council derives required permissions
  // from the command, not an agent-declared label).
  const meta = $("intent-meta");
  meta.hidden = false;
  const envEl = $("intent-env");
  const env = (intentLike.target_environment || "development").toLowerCase();
  envEl.value = env;
  envEl.setAttribute("data-env", env);

  // Rebuild editable field inputs.
  const fields = $("intent-fields");
  fields.innerHTML = "";
  addIntentField(fields, "Command", "command", intentLike.command, {
    mono: true,
    multiline: true,
    rows: 2,
    placeholder: "/usr/bin/echo hello > /workspace/out.txt",
  });
  addIntentField(fields, "Target", "target_resource", intentLike.target_resource, {
    mono: true,
    placeholder: "/workspace/out.txt",
  });
  addIntentField(fields, "Description", "description", intentLike.description, {
    multiline: true,
    rows: 2,
    placeholder: "What this command does (10+ characters).",
  });
  addIntentField(fields, "Justification", "justification", intentLike.justification, {
    multiline: true,
    rows: 5,
    placeholder: "Why it is needed; rollback plan; evidence for blast-radius claims.",
  });
  addIntentField(fields, "Blast radius", "blast_radius", intentLike.blast_radius, {
    multiline: true,
    rows: 2,
    placeholder: "Scope of potential impact if things go wrong.",
  });
  if (Array.isArray(intentLike.packaged_files) && intentLike.packaged_files.length) {
    addStaticIntentField(
      fields,
      "Packaged files",
      renderPackagedFilesSummary(intentLike.packaged_files),
      { mono: true }
    );
  }
  const certifiedRequestSummary = renderCertifiedEvidenceRequestSummary(intentLike);
  if (certifiedRequestSummary) {
    addStaticIntentField(
      fields,
      "Council request",
      certifiedRequestSummary,
      { mono: true, className: "certified-request-summary" }
    );
  }
  if (certifiedEvidence.length) {
    addStaticIntentField(
      fields,
      "certified_evidence[]",
      renderCertifiedEvidenceSummary(certifiedEvidence),
      { mono: true, className: "certified-evidence-summary" }
    );
  }

  $("intent-actions").hidden = false;
}

function addIntentField(container, label, fieldName, value, opts = {}) {
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  if (opts.mono) dd.className = "mono";

  let input;
  if (opts.multiline) {
    input = document.createElement("textarea");
    input.rows = opts.rows || 2;
  } else {
    input = document.createElement("input");
    input.type = "text";
  }
  input.dataset.field = fieldName;
  input.value = value == null ? "" : value;
  if (opts.placeholder) input.placeholder = opts.placeholder;

  dd.appendChild(input);
  container.appendChild(dt);
  container.appendChild(dd);
}

function addStaticIntentField(container, label, value, opts = {}) {
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  if (opts.mono) dd.className = "mono";
  if (opts.className) dd.classList.add(opts.className);
  dd.textContent = value == null ? "" : value;
  container.appendChild(dt);
  container.appendChild(dd);
}

function renderPackagedFilesSummary(files) {
  return files.map((file, idx) => {
    const parts = [
      `${idx + 1}. ${file.path || "(unnamed attachment)"}`,
      `mode: ${file.mode || "0600"}`,
      `sha256: ${file.sha256 || "(computed by council)"}`,
    ];
    if (file.description) parts.push(`description: ${file.description}`);
    parts.push("", decodeBase64Preview(file.content_base64 || ""));
    return parts.join("\n");
  }).join("\n\n---\n\n");
}

function renderCertifiedEvidenceRequestSummary(intentLike) {
  const note = intentLike.certified_evidence_note || "";
  if (!note && !certifiedEvidence.length) return "";

  const count = certifiedEvidence.length;
  const lines = [];
  if (count) {
    lines.push(
      `certified_evidence: present (${count} signed ExecutionResult${count === 1 ? "" : "s"})`
    );
    lines.push("sent to: council_authorize");
    lines.push("member label: CERTIFIED EXECUTOR EVIDENCE");
    lines.push(
      "council verifies: executor signature, registered public key, writ binding, exit code, stdout/stderr hashes"
    );
  } else {
    lines.push("certified_evidence: missing");
    lines.push("member label: unavailable until a signed ExecutionResult is attached");
  }
  if (note) lines.push(`scenario note: ${note}`);
  return lines.join("\n");
}

function renderCertifiedEvidenceSummary(results) {
  return results.map((result, idx) => {
    const evidence = result.evidence || {};
    const payload = evidence.payload || {};
    const command = payload.command || {};
    const resultEvidence = payload.result || {};
    return [
      `${idx + 1}. payload field: certified_evidence[${idx}]`,
      `member label: CERTIFIED EXECUTOR EVIDENCE`,
      `demo data: simulated DB-pool values; executor signature path is live`,
      `executor: ${payload.executor_id || result.executor_id || "(unknown)"}`,
      `executor key: ${(evidence.key_fingerprint || "").slice(0, 16) || "(missing)"}`,
      `writ: ${payload.writ_id || result.writ_id || "(missing)"}`,
      `command: ${command.command || "(missing)"}`,
      `exit_code: ${result.exit_code}`,
      `stdout_sha256: ${resultEvidence.stdout_sha256 || "(missing)"}`,
      `stderr_sha256: ${resultEvidence.stderr_sha256 || "(missing)"}`,
      `stdout:\n${truncateText(result.stdout || "", 1200) || "(empty)"}`,
    ].join("\n");
  }).join("\n\n---\n\n");
}

function truncateText(value, maxChars) {
  if (!value || value.length <= maxChars) return value || "";
  return value.slice(0, maxChars) + `\n... truncated ${value.length - maxChars} chars ...`;
}

function decodeBase64Preview(contentBase64) {
  try {
    const raw = atob(contentBase64);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
    const text = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
    return text.length > 1400 ? text.slice(0, 1400) + "\n... truncated ..." : text;
  } catch (err) {
    return "(unable to preview packaged file)";
  }
}

async function callAuthorize(body, intentLike) {
  // Keep the Intent editor and the loaded scenario reference intact —
  // the user just submitted it. We only clear prior-run results (members,
  // chairman, verdict, executor). Re-render the intent to freeze it at
  // the submitted values (any subsequent edits won't reflect backward
  // onto what was just sent).
  softResetResults();
  renderIntent(intentLike);
  // Start with pending cards so the audience sees the reviewers waiting.
  renderPendingMembers(Array.isArray(body.packaged_files) && body.packaged_files.length > 0);

  // Run the pipeline animation while the real call is in flight. The stages
  // advance on a timer; the real work (authenticate → categorize → chairman
  // → vote → mint) is happening on the server in the same order.
  const stageTimers = animatePipelineWhilePending();

  try {
    const t0 = performance.now();
    const result = await fetchJson("/api/authorize", { method: "POST", body });
    const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
    stopPipelineTimers(stageTimers);

    lastAuthResult = result;
    // Server echoes the resolved agent_id back so we send the right
    // X-Agent-ID header when submitting the writ to the executor.
    lastAuthAgentId = result._agent_id || null;
    await renderAuthResult(result, elapsed);
  } catch (err) {
    stopPipelineTimers(stageTimers);
    renderError(err);
  }
}

export function renderPendingMembers(includeCodeReview = false) {
  const row = $("council-row");
  const sidecar = $("sidecar-row");
  const sidecarSlot = $("sidecar-slot");
  row.innerHTML = "";
  if (sidecarSlot) sidecarSlot.innerHTML = "";
  if (sidecar) sidecar.hidden = !includeCodeReview;
  if (includeCodeReview) {
    const el = document.createElement("div");
    el.className = "member thinking code-review-member";
    el.dataset.role = "code-review-card";
    el.innerHTML = `
      <div class="member-head">
        <div class="member-head-left">
          <span class="member-eyebrow">Advisory Council Member</span>
          <div class="member-name">${escapeHtml(CODE_REVIEW_DISPLAY.label)}</div>
          <div class="member-focus">Reviews ${escapeHtml(CODE_REVIEW_DISPLAY.sub)}</div>
        </div>
        <span class="member-vote pending">reviewing…</span>
      </div>
      <div class="member-section">
        <span class="member-section-label">JSON result</span>
        <div class="member-reasoning" data-role="reasoning"></div>
      </div>
      <div class="member-section" data-role="concerns-section" hidden>
        <span class="member-section-label">Risk concerns</span>
        <div class="member-concerns" data-role="concerns"></div>
      </div>
    `;
    if (sidecarSlot) sidecarSlot.appendChild(el);
  }
  Object.entries(MEMBER_DISPLAY).forEach(([id, meta]) => {
    const el = document.createElement("div");
    el.className = "member thinking";
    el.dataset.memberId = id;
    el.innerHTML = `
      <div class="member-head">
        <div class="member-head-left">
          <span class="member-eyebrow">Council Member</span>
          <div class="member-name">${escapeHtml(meta.label)}</div>
          <div class="member-focus">Reviews ${escapeHtml(meta.sub)}</div>
        </div>
        <span class="member-vote pending">reviewing…</span>
      </div>
      <div class="member-section">
        <span class="member-section-label">Commentary</span>
        <div class="member-reasoning" data-role="reasoning"></div>
      </div>
      <div class="member-section" data-role="concerns-section" hidden>
        <span class="member-section-label">Risk concerns</span>
        <div class="member-concerns" data-role="concerns"></div>
      </div>
    `;
    row.appendChild(el);
  });
}

function animatePipelineWhilePending() {
  // Visually advance the pipeline while we wait for the live call.
  const stages = ["authenticate", "categorize", "chairman", "vote"];
  const timers = [];
  let i = 0;
  setPipeline(stages[0]);
  const tick = setInterval(() => {
    i += 1;
    if (i < stages.length) {
      setPipeline(stages[i]);
    } else {
      // Hold on "vote" until the response comes back.
      clearInterval(tick);
    }
  }, 1400);
  timers.push(tick);
  return timers;
}

function stopPipelineTimers(timers) {
  timers.forEach((t) => clearInterval(t));
}

// ── Render results ──────────────────────────────────────────────────

async function renderAuthResult(result, elapsed) {
  // Chairman card
  const ch = result.chairman || {};
  const risk = (ch.risk_level || "").toLowerCase();
  $("chairman-card").hidden = false;
  const riskEl = $("chairman-risk");
  riskEl.textContent = ch.risk_level || "—";
  riskEl.className = "chairman-risk " + risk;
  $("chairman-threshold").textContent = ch.threshold || "—";
  $("chairman-reason").textContent = ch.reasoning || "(no commentary provided)";

  // Members — typewriter reveal of each reasoning block.
  const members = Array.isArray(result.council_members) ? result.council_members : [];
  if (result.code_review || document.querySelector('[data-role="code-review-card"]')) {
    await renderCodeReviewCard(result.code_review);
  }
  await revealMembers(members);

  // Pipeline: advance to mint/deny
  setPipeline("mint");

  // Verdict
  renderVerdict(result, elapsed);

  // Executor card — only meaningful if approved and a writ is present
  if (result.status === "approved" && result.writ) {
    setupExecutorCard(result);
    setPipeline("executor");
  }
}

async function revealMembers(members) {
  // Map council return order onto our three member cards. The council
  // returns them by member_id; if a member is missing from the response
  // (early denial with no vote), mark it skipped.
  const cards = document.querySelectorAll(".member[data-member-id]");
  const byId = new Map(members.map((m) => [m.member_id, m]));

  // Reveal in parallel so all three feel like they're deliberating at once,
  // but with slightly staggered starts for readability.
  const tasks = Array.from(cards).map((card, idx) => {
    const memberId = card.dataset.memberId;
    const vote = byId.get(memberId);
    const delay = idx * 180;
    return new Promise((resolve) =>
      setTimeout(() => {
        renderMemberCard(card, vote).then(resolve);
      }, delay)
    );
  });
  await Promise.all(tasks);
}

async function renderCodeReviewCard(review) {
  const sidecar = $("sidecar-row");
  const sidecarSlot = $("sidecar-slot");
  if (!sidecar || !sidecarSlot) return;
  sidecar.hidden = false;
  const old = sidecarSlot.querySelector('[data-role="code-review-card"]');
  const card = document.createElement("div");
  card.className = "member code-review-member";
  card.dataset.role = "code-review-card";

  const decision = (review?.decision || "reviewed").toLowerCase();
  const decisionClass = ["approve", "deny", "pending"].includes(decision)
    ? decision
    : "advisory";
  card.innerHTML = `
    <div class="member-head">
      <div class="member-head-left">
        <span class="member-eyebrow">Advisory Council Member</span>
        <div class="member-name">${escapeHtml(review?.display_name || CODE_REVIEW_DISPLAY.label)}</div>
        <div class="member-focus">Reviews ${escapeHtml(CODE_REVIEW_DISPLAY.sub)}</div>
      </div>
      <span class="member-vote ${decisionClass}">${escapeHtml(decision)}</span>
    </div>
    <div class="member-section">
      <span class="member-section-label">JSON result</span>
      <div class="member-reasoning" data-role="reasoning"></div>
    </div>
    <div class="member-section" data-role="concerns-section">
      <span class="member-section-label">Risk concerns</span>
      <div class="member-concerns" data-role="concerns"></div>
    </div>
  `;
  if (old) old.replaceWith(card);
  else sidecarSlot.appendChild(card);

  const reasoningEl = card.querySelector('[data-role="reasoning"]');
  const concernsEl = card.querySelector('[data-role="concerns"]');
  const concerns = Array.isArray(review?.risk_concerns) ? review.risk_concerns : [];
  if (concerns.length) {
    concernsEl.innerHTML =
      "<ul>" + concerns.map((c) => `<li>${escapeHtml(c)}</li>`).join("") + "</ul>";
  } else {
    concernsEl.innerHTML = '<span class="member-empty">None noted.</span>';
  }
  const reasoning = review?.reasoning || "No Code Review analysis was returned.";
  await typeWriter(reasoningEl, reasoning);
}

async function renderMemberCard(card, vote) {
  const voteBadge = card.querySelector(".member-vote");
  const reasoningEl = card.querySelector('[data-role="reasoning"]');
  const concernsSection = card.querySelector('[data-role="concerns-section"]');
  const concernsEl = card.querySelector('[data-role="concerns"]');
  card.classList.remove("thinking");

  if (!vote) {
    voteBadge.textContent = "—";
    voteBadge.className = "member-vote pending";
    reasoningEl.textContent = "No vote recorded (early gate decision).";
    if (concernsSection) concernsSection.hidden = true;
    return;
  }

  const decision = (vote.decision || "").toLowerCase();
  voteBadge.textContent = decision || "—";
  voteBadge.className = "member-vote " + decision;

  const reasoning = vote.reasoning || "(no commentary provided)";
  await typeWriter(reasoningEl, reasoning);

  // Risk concerns section is always shown for a recorded vote, with a
  // "None noted" placeholder when the member flagged no concerns. This
  // makes it visible to the viewer that the member reviewed for concerns
  // and found none, versus the section simply being absent.
  if (concernsSection && concernsEl) {
    concernsSection.hidden = false;
    if (Array.isArray(vote.risk_concerns) && vote.risk_concerns.length) {
      concernsEl.innerHTML =
        "<ul>" +
        vote.risk_concerns.map((c) => `<li>${escapeHtml(c)}</li>`).join("") +
        "</ul>";
    } else {
      concernsEl.innerHTML = '<span class="member-empty">None noted.</span>';
    }
  }
}

function typeWriter(el, text) {
  // Animate the reveal of the council member's reasoning. The text
  // itself is real output; only the pacing of the reveal is cosmetic.
  return new Promise((resolve) => {
    el.textContent = "";
    const chars = text.split("");
    const perChar = chars.length > 600 ? 4 : 10; // speed up long blocks
    let i = 0;
    const tick = () => {
      const chunk = chars.slice(i, i + 6).join("");
      el.textContent += chunk;
      i += 6;
      el.scrollTop = el.scrollHeight;
      if (i < chars.length) {
        setTimeout(tick, perChar);
      } else {
        resolve();
      }
    };
    tick();
  });
}

function renderVerdict(result, elapsed) {
  const card = $("verdict-card");
  card.hidden = false;
  const badge = $("verdict-badge");
  const meta = $("verdict-meta");
  const body = $("verdict-body");
  body.innerHTML = "";

  const status = (result.status || "error").toLowerCase();
  badge.textContent = status;
  badge.className = "verdict-badge " + status;

  const writId = result.writ_id || result.decision_id || "";
  meta.textContent = `council latency: ${elapsed}s${writId ? "  ·  writ_id: " + writId : ""}`;

  if (status === "approved") {
    const certifiedCount =
      Number(result._certified_evidence_count || 0) || certifiedEvidence.length;
    const certifiedApproval = certifiedCount
      ? `<div class="certified-verdict-note">
           <b>certified_evidence accepted:</b>
           this council_authorize request included ${certifiedCount}
           signed ExecutionResult${certifiedCount === 1 ? "" : "s"}. Members received it under
           <code>CERTIFIED EXECUTOR EVIDENCE</code> after council-side signature,
           key-registry, writ-binding, exit-code, and stdout/stderr hash checks.
         </div>`
      : "";
    body.innerHTML = `
      <p>Council minted a signed Writ of Execution. Click <b>Submit writ to executor</b> to run trusted executor verification.</p>
      ${certifiedApproval}
      <div class="writ-detail"><b>writ (base64, truncated):</b> ${escapeHtml(
        (result.writ || "").slice(0, 96)
      )}…</div>
    `;
  } else if (status === "denied") {
    const guidance = Array.isArray(result.guidance) ? result.guidance : [];
    const missing = Array.isArray(result.missing_permissions) ? result.missing_permissions : [];
    const parts = [];
    if (missing.length) {
      parts.push(
        `<p><b>Missing permissions:</b> ${missing
          .map((m) => `<span class="missing-perm">${escapeHtml(m)}</span>`)
          .join(" ")}</p>`
      );
    }
    if (guidance.length) {
      parts.push(
        `<p><b>Guidance returned to the agent (informational decline):</b></p>
         <ol class="guidance-list">${guidance
           .map((g) => `<li>${escapeHtml(g)}</li>`)
           .join("")}</ol>`
      );
    }
    if (!parts.length) {
      parts.push(
        "<p>The council denied this request. Each member's reasoning is shown above.</p>"
      );
    }
    // Multi-round retry affordance: only surface it after the original
    // attempt of a scenario that defines a followup intent. Later denials
    // (or any custom intent) shouldn't get the button — there's no further
    // scripted round to load.
    const hasFollowup = loadedScenario && loadedScenario.followup && currentTurn === 1;
    const requiresCertifiedEvidence =
      hasFollowup && !!loadedScenario.followup.requires_certified_evidence;
    const hasCertifiedEvidence = certifiedEvidence.length > 0;
    const canRetry =
      hasFollowup && (!requiresCertifiedEvidence || hasCertifiedEvidence);
    const canCollectCertifiedEvidence =
      hasFollowup &&
      requiresCertifiedEvidence &&
      !hasCertifiedEvidence &&
      !!loadedScenario.followup.evidence_probe;
    if (canCollectCertifiedEvidence) {
      parts.push(
        `<div class="retry-cta-row">
           <button type="button" class="retry-cta" id="collect-certified-evidence-btn">
             Round 2 — collect certified evidence →
           </button>
           <span class="retry-cta-hint">
             Loads a separate evidence-gathering intent. Run it and submit its writ to the executor; the signed result becomes evidence for round 3.
           </span>
         </div>`
      );
    }
    if (canRetry) {
      parts.push(
        `<div class="retry-cta-row">
           <button type="button" class="retry-cta" id="retry-btn">
             ${requiresCertifiedEvidence ? "Round 3 — retry original command with certified evidence" : "Agent iterates — retry with evidence"} →
           </button>
           <span class="retry-cta-hint">
             Loads the original pool-increase command with updated justification and blast radius. Review it, then press <b>Run</b> again.
           </span>
         </div>`
      );
    }
    body.innerHTML = parts.join("");
    if (canCollectCertifiedEvidence) {
      const btn = document.getElementById("collect-certified-evidence-btn");
      if (btn) btn.addEventListener("click", handleCollectCertifiedEvidence);
    }
    if (canRetry) {
      const btn = document.getElementById("retry-btn");
      if (btn) btn.addEventListener("click", handleRetryWithFollowup);
    }
  } else {
    body.innerHTML = `<pre class="writ-detail">${escapeHtml(
      JSON.stringify(result, null, 2)
    )}</pre>`;
  }
}

// ── Executor submission ─────────────────────────────────────────────

function setupExecutorCard(authResult) {
  const card = $("executor-card");
  card.hidden = false;
  $("execute-btn").disabled = false;
  // Reset verification checklist
  Array.from(document.querySelectorAll(".verify-steps li")).forEach((li) => {
    li.classList.remove("pass", "fail");
  });
  $("executor-result").classList.remove("has-actions");
  $("executor-result").textContent = "";
}

function wireExecuteButton() {
  $("execute-btn").addEventListener("click", async () => {
    if (!lastAuthResult || !lastAuthResult.writ) return;
    $("execute-btn").disabled = true;
    // Run the checklist animation in parallel with the real POST. We wait
    // for both to finish before rendering the result, so the audience
    // always sees the full verification progression even on fast responses
    // (the executor can return in under 10ms for echo-class commands).
    const animationP = animateVerificationSteps();
    const fetchP = fetchJson("/api/execute", {
      method: "POST",
      body: { writ: lastAuthResult.writ, agent_id: lastAuthAgentId || undefined },
    });
    try {
      const [res] = await Promise.all([fetchP, animationP]);
      renderExecutorResult(res);
    } catch (err) {
      // If the fetch rejected, let the animation finish (it is visual
      // only) and then show the error so the layout doesn't jitter.
      await animationP.catch(() => {});
      $("executor-result").classList.remove("has-actions");
      $("executor-result").textContent = "Executor call failed: " + err.message;
    }
  });
}

function animateVerificationSteps() {
  // Tick through verification stages visually at a pace the audience can read. The
  // actual verification is atomic on the server; this is purely the UI
  // pacing the reveal. Returns a promise that resolves once all ten
  // steps have been marked pass (the final state is then overridden by
  // renderExecutorResult, which honors the real verdict).
  const steps = Array.from(document.querySelectorAll(".verify-steps li"));
  return new Promise((resolve) => {
    let i = 0;
    const tick = setInterval(() => {
      if (i >= steps.length) {
        clearInterval(tick);
        resolve();
        return;
      }
      steps[i].classList.add("pass");
      i += 1;
    }, 160);
  });
}

function renderExecutorResult(res) {
  const out = $("executor-result");
  if (res.success) {
    // Defensive: ensure all steps are marked pass regardless of
    // animation state. If the animation finished, these are already
    // set; if it didn't, we fix it here.
    Array.from(document.querySelectorAll(".verify-steps li")).forEach((li) => {
      li.classList.remove("fail");
      li.classList.add("pass");
    });
    const canContinueToRound3 = !!(res.evidence && pendingCertifiedFollowupScenario);
    if (res.evidence) {
      certifiedEvidence = [res, ...certifiedEvidence].slice(0, 5);
      if (!canContinueToRound3 && loadedIntent) renderIntent(loadedIntent);
    }
    const evidence = res.evidence || {};
    const payload = evidence.payload || {};
    const evidenceResult = payload.result || {};
    const certifiedText = res.evidence
      ? `\n--- certified evidence ---\n` +
        `signature: present\n` +
        `next_request_field: certified_evidence[0]\n` +
        `member_label: CERTIFIED EXECUTOR EVIDENCE\n` +
        `demo_data: simulated; signature_path: live\n` +
        `executor_key: ${(evidence.key_fingerprint || "").slice(0, 16)}\n` +
        `stdout_sha256: ${evidenceResult.stdout_sha256 || ""}\n` +
        `stderr_sha256: ${evidenceResult.stderr_sha256 || ""}\n` +
        `attached_to_next_request: yes\n`
      : "\n--- certified evidence ---\nsignature: missing\n";
    const resultText =
      `exit_code: ${res.exit_code}\n` +
      `duration:  ${res.duration_seconds?.toFixed(3) ?? "?"}s\n` +
      `writ_id:   ${res.writ_id}\n\n` +
      `--- stdout ---\n${res.stdout || ""}\n` +
      (res.stderr ? `--- stderr ---\n${res.stderr}\n` : "") +
      certifiedText;
    if (canContinueToRound3) {
      out.classList.add("has-actions");
      out.innerHTML = `
        <pre class="executor-result-text">${escapeHtml(resultText)}</pre>
        <div class="executor-next-round">
          <div>
            <b>Round 2 complete.</b>
            The signed ExecutionResult is queued as <code>certified_evidence[0]</code>.
            Round 3 returns to the original pool-increase command with updated
            justification and blast radius.
          </div>
          <button type="button" class="retry-cta" id="continue-round3-btn">
            Round 3 — load original command with certified evidence →
          </button>
        </div>
      `;
      const btn = document.getElementById("continue-round3-btn");
      if (btn) btn.addEventListener("click", handleContinueToCertifiedRetry);
    } else {
      out.classList.remove("has-actions");
      out.textContent = resultText;
    }
  } else {
    out.classList.remove("has-actions");
    // On failure, mark the step that actually fired as red; clear any
    // downstream steps that were optimistically lit.
    const stage = (res.verification_stage || "").toLowerCase();
    const stageToIndex = {
      deserialize: 1,
      agent_binding: 2, "agent-binding": 2,
      signature: 3,
      temporal: 4,
      nonce: 5,
      "working-directory": 6, directory: 6, "working_dir": 6,
      "blocked-patterns": 7, blocked_patterns: 7, blocked_pattern: 7, blocked: 7,
      "tool-whitelist": 8, tool_whitelist: 8, tool: 8, tools: 8,
      "denied-args": 9, denied_args: 9, denied_tool_args: 9,
      "denied-paths": 10, denied_paths: 10, denied_path: 10, paths: 10,
      "command-non-empty": 11, command: 11, empty_command: 11, empty: 11,
      attachment: 12, packaged_files: 12, "packaged-files": 12,
    };
    const idx = stageToIndex[stage];
    if (idx) {
      const steps = Array.from(document.querySelectorAll(".verify-steps li"));
      steps.forEach((li, i) => {
        li.classList.remove("pass", "fail");
        if (i < idx - 1) li.classList.add("pass");
      });
      const target = steps[idx - 1];
      if (target) target.classList.add("fail");
    }
    const stageExplanation = {
      deserialize:
        "The executor could not decode the writ payload, so no trust decision was made.",
      agent_binding:
        "The caller identity did not match the agent bound into the writ.",
      signature:
        "The executor rejected the writ before policy checks because its cryptographic signature did not verify.",
      temporal:
        "The writ signature verified, but the executor rejected it because its validity window was not current.",
      nonce:
        "The writ signature verified, but replay protection rejected the nonce.",
    };
    const explanation =
      stageExplanation[stage] ||
      `This is defense in depth in action: the council approved the intent,\n` +
        `but the executor's local policy caught it at step ${idx ?? "?"}.`;
    out.textContent =
      `success:            false\n` +
      `verification_stage: ${res.verification_stage || "?"}\n` +
      `error:              ${res.error || "?"}\n\n` +
      `${explanation}\n`;
  }
}

// ── HTTP helpers ────────────────────────────────────────────────────

async function fetchJson(path, opts = {}) {
  const method = opts.method || "GET";
  const headers = { "Content-Type": "application/json" };
  const init = { method, headers };
  if (opts.body) init.body = JSON.stringify(opts.body);
  // Retry ONLY transport-level failures — a rejected fetch ("Failed to fetch":
  // dropped connection, router/keep-alive blip, transient DNS). HTTP error
  // responses (401/403/5xx) are real server verdicts and are NOT retried. Up to
  // 3 attempts with short backoff. Note: the executor's nonce replay-defense
  // makes a retried /api/execute safe (a duplicate writ execution fails closed).
  const MAX_ATTEMPTS = 3;
  let lastErr;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    let resp;
    try {
      resp = await fetch(path, init);
    } catch (err) {
      lastErr = err; // network error (e.g. "Failed to fetch")
      if (attempt < MAX_ATTEMPTS) {
        await new Promise((r) => setTimeout(r, 300 * attempt));
        continue;
      }
      throw err;
    }
    if (!resp.ok) {
      let detail = "";
      try { detail = (await resp.json()).detail || ""; } catch (e) { /* ignore */ }
      throw new Error(`${resp.status} ${resp.statusText}${detail ? ": " + detail : ""}`);
    }
    return await resp.json();
  }
  throw lastErr;
}

function renderError(err) {
  $("verdict-card").hidden = false;
  $("verdict-badge").textContent = "error";
  $("verdict-badge").className = "verdict-badge error";
  $("verdict-meta").textContent = "";
  $("verdict-body").innerHTML = `<pre class="writ-detail">${escapeHtml(err.message)}</pre>`;
  markPipelineFailed("vote");
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
