/* Intro tour for Council Chamber.
 *
 * Pure client-side walkthrough. Makes zero council or executor calls.
 *
 * At tour start, we seed placeholder content into the normally-empty
 * panels (sample chairman, sample member reasoning, sample verdict,
 * sample executor output) so the tour can highlight real DOM elements
 * without any live request. On tour end we call resetStage() to wipe
 * the placeholders — the UI is back to its initial state.
 */

import { resetStage, renderPendingMembers, renderIntent } from "./app.js";

// ── Tour steps ───────────────────────────────────────────────────────

const STEPS = [
  {
    target: null, // centered welcome modal
    title: "Welcome to the Council Chamber",
    body:
      "Everything you'll see here is live. The council is three LLM reviewers evaluating the intents you submit — no mocks, no canned responses, no shortcuts. This quick tour walks you through the UI.",
  },
  {
    target: "#status",
    title: "Live connection",
    body:
      "These pills confirm the council and executor are running and reachable. If either is red, the demo can't mint or verify writs.",
    placement: "bottom",
  },
  {
    target: ".scenario-custom",
    title: "Custom intent",
    body:
      "Click Custom to start from a blank template — write your own command, description, justification, and blast-radius. It gets the same live review as a scripted scenario.",
    placement: "right",
  },
  {
    target: "#silo-list",
    title: "Scripted scenarios, grouped by job",
    body:
      "Scripted scenarios are grouped by the job the submitting agent is doing. Click a silo header to toggle it, then click a scenario to load it into the editor. Nothing runs until you press Run.",
    placement: "right",
  },
  {
    target: "#intent-card",
    title: "The intent editor",
    body:
      "Whatever you loaded appears here as editable fields. The agent selector at the top chooses which seeded account submits the request — different agents have different permission scopes, and the chairman will deny on the permission gate if the agent lacks what the command needs.",
    placement: "bottom",
  },
  {
    target: "#intent-actions",
    title: "Run submits to the live council",
    body:
      "Press Run to send the current fields to council_authorize over MCP. Reset restores the loaded scenario's defaults if you've edited. Nothing hits the council until Run is clicked.",
    placement: "top",
  },
  {
    target: "#pipeline",
    title: "The authorization pipeline",
    body:
      "An intent flows left to right: authenticate, categorize, chairman pre-authorize, members vote, mint or deny, then executor verify. Each stage lights up as it runs on the server.",
    placement: "bottom",
  },
  {
    target: "#council-row",
    title: "Three independent reviewers",
    body:
      "Each member is a different LLM from a different model family. Each has its own priorities: Command Integrity watches WHAT, Environment Isolation watches WHERE, Blast Containment watches WHAT HAPPENS IF. Their reasoning streams in as they work.",
    placement: "bottom",
  },
  {
    target: "#chairman-card",
    title: "Risk and voting threshold",
    body:
      "The chairman runs before the vote. It compares the agent's permissions to what the command needs, assesses risk, and picks the threshold: quorum (majority) for low-risk, unanimous for high-risk.",
    placement: "bottom",
  },
  {
    target: "#verdict-card",
    title: "Approved — or informationally declined",
    body:
      "On approval, the council mints a cryptographically signed Writ of Execution. On denial the agent receives guidance: what was wrong, what to change, what permissions it lacked. Not a bare 404.",
    placement: "top",
  },
  {
    target: "#executor-card",
    title: "Defense in depth",
    body:
      "Even a council-approved writ must pass trusted executor verification: caller binding, cryptography, temporal bounds, nonce, then local policy. Council approval is necessary but not sufficient.",
    placement: "top",
  },
  {
    target: null,
    title: "Ready",
    body:
      "Close this and click any scenario on the left (or Custom) to start. The Intro tour is always here if you want to replay it.",
  },
];

// ── Sample placeholder content ───────────────────────────────────────

const SAMPLE_INTENT = {
  title: "Sample intent — tour placeholder",
  subtitle: "This is a sample payload for the walkthrough. No council call is being made.",
  command: "/bin/echo demo-sample > /workspace/sample.txt",
  description:
    "Write a small marker file to the sandbox workspace. Single literal write; no shell expansion, no network, no pre-existing path modified.",
  target_resource: "/workspace/sample.txt",
  justification:
    "Example intent used by the intro tour. Target is an ephemeral Docker volume; blast radius is a single 15-byte file; rollback is idempotent.",
  blast_radius: "Single new file of ~15 bytes in an ephemeral sandbox volume.",
  target_environment: "development",
};

const SAMPLE_MEMBERS = [
  {
    member_id: "command-integrity",
    decision: "approve",
    reasoning:
      "(Sample reasoning) The command uses an absolute path, matches the stated description, and has no shell-injection or obfuscation patterns. Single literal write; no pipes or substitutions.",
    risk_concerns: [],
  },
  {
    member_id: "environment-isolation",
    decision: "approve",
    reasoning:
      "(Sample reasoning) Working directory is scoped to /workspace inside the ephemeral executor volume. No cross-cluster or cross-namespace access. Target resource matches the command's effect.",
    risk_concerns: [],
  },
  {
    member_id: "blast-containment",
    decision: "approve",
    reasoning:
      "(Sample reasoning) Single new file of small size, idempotent rollback available via /bin/rm. No downstream dependencies. Blast radius is honestly declared and verified by the command semantics.",
    risk_concerns: [],
  },
];

const SAMPLE_CHAIRMAN = {
  risk_level: "low",
  threshold: "quorum",
  reasoning:
    "(Sample reasoning) Read/write inside sandbox, development environment, no cross-tenant access. Quorum threshold is sufficient; unanimous not required.",
};

const SAMPLE_WRIT =
  "eyJ3cml0X2lkIjoid3JpdC0wMTljNGU4NDhjNzdleGFtcGxlIiwiY29tbWFuZCI6Ii9iaW4vZWNobyBkZW1vLXNhbXBsZSA+IC93b3Jrc3BhY2Uvc2FtcGxlLnR4dCIsInRhcmdldF9hZ2VudF9pZCI6ImFnZW50LWRlbW8iLCJub25jZSI6ImExYjJjM2Q0ZTVmNiJ9";

// ── Tour state ───────────────────────────────────────────────────────

let stepIndex = 0;
let active = false;
let keydownHandler = null;
let resizeHandler = null;

// ── Public entry point ───────────────────────────────────────────────

export function startTour() {
  if (active) return;
  active = true;
  stepIndex = 0;
  // Mark tour active FIRST so the CSS rule that disables the silo
  // expand/collapse transition is in effect before we flip any classes
  // on individual silos — otherwise the first expand animates and the
  // spotlight measurement catches a mid-animation rect.
  document.body.classList.add("tour-active");
  expandAllSilos();
  // Force a synchronous layout flush so all geometry is committed
  // before the first spotlight measurement runs.
  void document.body.offsetHeight;
  seedPlaceholders();
  buildOverlay();
  keydownHandler = onKeydown;
  resizeHandler = () => renderStep(stepIndex);
  document.addEventListener("keydown", keydownHandler);
  window.addEventListener("resize", resizeHandler);
  renderStep(0);
}

function endTour() {
  if (!active) return;
  active = false;
  document.removeEventListener("keydown", keydownHandler);
  window.removeEventListener("resize", resizeHandler);
  document.body.classList.remove("tour-active");
  document.getElementById("tour-root").innerHTML = "";
  // Wipe placeholder content so nothing lingers; collapse silos back to
  // the default state so the rail looks the same as before the tour.
  resetStage();
  collapseAllSilos();
}

function expandAllSilos() {
  document.querySelectorAll(".silo.collapsed").forEach((s) => {
    s.classList.remove("collapsed");
    const h = s.querySelector(".silo-heading");
    if (h) h.setAttribute("aria-expanded", "true");
  });
}

function collapseAllSilos() {
  document.querySelectorAll(".silo").forEach((s) => {
    s.classList.add("collapsed");
    const h = s.querySelector(".silo-heading");
    if (h) h.setAttribute("aria-expanded", "false");
  });
}

// ── Placeholder seeding ──────────────────────────────────────────────

function seedPlaceholders() {
  // Intent card — reuse the real render path so the tour and live scenarios
  // look identical.
  renderIntent(SAMPLE_INTENT);

  // Council members — render three pending cards, then fill them with sample
  // votes + "None noted" concerns. Uses the same DOM structure as a live run.
  renderPendingMembers();
  const cards = document.querySelectorAll(".member");
  cards.forEach((card, i) => {
    const vote = SAMPLE_MEMBERS[i];
    card.classList.remove("thinking");
    const badge = card.querySelector(".member-vote");
    const reasoningEl = card.querySelector('[data-role="reasoning"]');
    const concernsSection = card.querySelector('[data-role="concerns-section"]');
    const concernsEl = card.querySelector('[data-role="concerns"]');
    if (badge) {
      badge.textContent = vote.decision;
      badge.className = "member-vote " + vote.decision;
    }
    if (reasoningEl) reasoningEl.textContent = vote.reasoning;
    if (concernsSection) concernsSection.hidden = false;
    if (concernsEl) {
      if (vote.risk_concerns && vote.risk_concerns.length) {
        concernsEl.innerHTML =
          "<ul>" +
          vote.risk_concerns.map((c) => `<li>${escapeHtml(c)}</li>`).join("") +
          "</ul>";
      } else {
        concernsEl.innerHTML = '<span class="member-empty">None noted.</span>';
      }
    }
  });

  // Chairman
  const chairmanCard = document.getElementById("chairman-card");
  if (chairmanCard) chairmanCard.hidden = false;
  const chairmanRisk = document.getElementById("chairman-risk");
  if (chairmanRisk) {
    chairmanRisk.textContent = SAMPLE_CHAIRMAN.risk_level;
    chairmanRisk.className = "chairman-risk " + SAMPLE_CHAIRMAN.risk_level;
  }
  const chairmanThreshold = document.getElementById("chairman-threshold");
  if (chairmanThreshold) chairmanThreshold.textContent = SAMPLE_CHAIRMAN.threshold;
  const chairmanReason = document.getElementById("chairman-reason");
  if (chairmanReason) chairmanReason.textContent = SAMPLE_CHAIRMAN.reasoning;

  // Verdict
  const verdictCard = document.getElementById("verdict-card");
  if (verdictCard) verdictCard.hidden = false;
  const badge = document.getElementById("verdict-badge");
  if (badge) {
    badge.textContent = "approved";
    badge.className = "verdict-badge approved";
  }
  const meta = document.getElementById("verdict-meta");
  if (meta) meta.textContent = "council latency: 6.2s  ·  writ_id: writ-019c4e848c77example";
  const body = document.getElementById("verdict-body");
  if (body) {
    body.innerHTML =
      '<p>Council minted a signed Writ of Execution. (Sample — this is what an approval looks like.)</p>' +
      '<div class="writ-detail"><b>writ (base64, truncated):</b> ' +
      escapeHtml(SAMPLE_WRIT.slice(0, 96)) +
      "…</div>";
  }

  // Executor
  const executorCard = document.getElementById("executor-card");
  if (executorCard) executorCard.hidden = false;
  const executeBtn = document.getElementById("execute-btn");
  if (executeBtn) executeBtn.disabled = true;
  document
    .querySelectorAll(".verify-steps li")
    .forEach((li) => li.classList.add("pass"));
  const execResult = document.getElementById("executor-result");
  if (execResult)
    execResult.textContent =
      "exit_code: 0\n" +
      "duration:  0.052s\n" +
      "writ_id:   writ-019c4e848c77example\n\n" +
      "--- stdout ---\n" +
      "(Sample executor output — no command is being run.)\n";
}

// ── Overlay DOM construction ─────────────────────────────────────────

function buildOverlay() {
  const root = document.getElementById("tour-root");
  root.innerHTML = `
    <div class="tour-backdrop"></div>
    <div class="tour-spotlight" id="tour-spotlight"></div>
    <div class="tour-callout" id="tour-callout" role="dialog" aria-modal="true">
      <div class="tour-callout-head">
        <span class="tour-callout-counter" id="tour-counter">1 / ${STEPS.length}</span>
        <button class="tour-btn-skip" id="tour-skip" type="button">skip tour ✕</button>
      </div>
      <h3 class="tour-callout-title" id="tour-title">—</h3>
      <p class="tour-callout-body" id="tour-body">—</p>
      <div class="tour-callout-actions">
        <button class="tour-btn" id="tour-back" type="button">Back</button>
        <div class="tour-callout-actions-right">
          <button class="tour-btn tour-btn-primary" id="tour-next" type="button">Next →</button>
        </div>
      </div>
    </div>
  `;
  root.querySelector(".tour-backdrop").addEventListener("click", endTour);
  document.getElementById("tour-skip").addEventListener("click", endTour);
  document.getElementById("tour-back").addEventListener("click", () => renderStep(stepIndex - 1));
  document.getElementById("tour-next").addEventListener("click", () => {
    if (stepIndex >= STEPS.length - 1) endTour();
    else renderStep(stepIndex + 1);
  });
}

// ── Step rendering / positioning ────────────────────────────────────

function renderStep(i) {
  if (i < 0) i = 0;
  if (i >= STEPS.length) {
    endTour();
    return;
  }
  stepIndex = i;
  const step = STEPS[i];

  document.getElementById("tour-counter").textContent = `${i + 1} / ${STEPS.length}`;
  document.getElementById("tour-title").textContent = step.title;
  document.getElementById("tour-body").textContent = step.body;
  document.getElementById("tour-back").disabled = i === 0;
  document.getElementById("tour-next").textContent =
    i === STEPS.length - 1 ? "Close" : "Next →";

  const spotlight = document.getElementById("tour-spotlight");
  const callout = document.getElementById("tour-callout");

  if (!step.target) {
    // Centered modal — no spotlight cutout.
    spotlight.className = "tour-spotlight center";
    spotlight.style.top = "50%";
    spotlight.style.left = "50%";
    spotlight.style.width = "0px";
    spotlight.style.height = "0px";
    // Center callout
    positionCalloutCenter(callout);
    return;
  }

  const targetEl = document.querySelector(step.target);
  if (!targetEl) {
    // Target missing for some reason — fall back to centered.
    spotlight.className = "tour-spotlight center";
    positionCalloutCenter(callout);
    return;
  }

  // Scroll target into view so the spotlight lines up. Instant (not
  // smooth) — we need a stable rect before we measure, and the tour's
  // own animations already pace the reveal.
  targetEl.scrollIntoView({ block: "center", behavior: "auto" });

  // Delay long enough to cover (a) scroll settle, (b) any remaining
  // CSS transition on the silo expand/collapse animation.
  setTimeout(() => {
    const rect = targetEl.getBoundingClientRect();
    const pad = 6;
    spotlight.className = "tour-spotlight";
    spotlight.style.top = rect.top - pad + "px";
    spotlight.style.left = rect.left - pad + "px";
    spotlight.style.width = rect.width + pad * 2 + "px";
    spotlight.style.height = rect.height + pad * 2 + "px";
    positionCallout(callout, rect, step.placement || "auto");
  }, 120);
}

function positionCallout(callout, rect, placement) {
  const margin = 16;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const cw = Math.min(380, vw - 40);
  callout.style.maxWidth = cw + "px";

  // measure
  callout.style.visibility = "hidden";
  callout.style.top = "0px";
  callout.style.left = "0px";
  const ch = callout.offsetHeight;

  let top = 0,
    left = 0;
  const tryBelow = rect.bottom + margin;
  const tryAbove = rect.top - ch - margin;
  const tryRight = rect.right + margin;
  const tryLeft = rect.left - cw - margin;

  let chosen = placement;
  if (chosen === "auto") {
    if (tryBelow + ch < vh) chosen = "bottom";
    else if (tryAbove > 0) chosen = "top";
    else if (tryRight + cw < vw) chosen = "right";
    else chosen = "left";
  }

  switch (chosen) {
    case "top":
      top = Math.max(margin, tryAbove);
      left = Math.min(Math.max(margin, rect.left), vw - cw - margin);
      break;
    case "right":
      left = Math.min(tryRight, vw - cw - margin);
      top = Math.min(Math.max(margin, rect.top), vh - ch - margin);
      break;
    case "left":
      left = Math.max(margin, tryLeft);
      top = Math.min(Math.max(margin, rect.top), vh - ch - margin);
      break;
    case "bottom":
    default:
      top = Math.min(tryBelow, vh - ch - margin);
      left = Math.min(Math.max(margin, rect.left), vw - cw - margin);
      break;
  }

  callout.style.top = top + "px";
  callout.style.left = left + "px";
  callout.style.visibility = "visible";
}

function positionCalloutCenter(callout) {
  callout.style.visibility = "hidden";
  callout.style.maxWidth = "440px";
  callout.style.top = "50%";
  callout.style.left = "50%";
  // Approx center
  const rect = callout.getBoundingClientRect();
  callout.style.top = window.innerHeight / 2 - rect.height / 2 + "px";
  callout.style.left = window.innerWidth / 2 - rect.width / 2 + "px";
  callout.style.visibility = "visible";
}

// ── Keyboard ─────────────────────────────────────────────────────────

function onKeydown(ev) {
  if (!active) return;
  if (ev.key === "Escape") {
    ev.preventDefault();
    endTour();
  } else if (ev.key === "ArrowRight" || ev.key === "Enter" || ev.key === " ") {
    ev.preventDefault();
    if (stepIndex >= STEPS.length - 1) endTour();
    else renderStep(stepIndex + 1);
  } else if (ev.key === "ArrowLeft") {
    ev.preventDefault();
    renderStep(stepIndex - 1);
  }
}

// ── Local helpers ────────────────────────────────────────────────────

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
