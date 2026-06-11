/* Council Monitor — dashboard driver.
 *
 * Polls /api/metrics + /api/recent on a 1-second cadence, renders
 * stat cards, sparklines, breakdowns, and a live request table.
 *
 * Sparklines use vanilla canvas — no external libraries — to keep
 * the page weight minimal and the asset footprint reproducible.
 */

const POLL_MS = 1000;
const HISTORY_POINTS = 300; // 5 minutes at 1s cadence
const LEDGER_PAGE_SIZE = 100;
const COUNCIL_TOOLS = new Set([
  "council_authorize",
  "council_propose_intent",
  "council_v1_authorize",
  "council_v1_propose_intent",
  "council_v2_authorize",
  "council_v2_propose_intent",
]);
const KNOWN_SOURCES = new Set(["direct", "grpc", "demo"]);
const SERIES_COLORS = [
  "#d97706",
  "#38bdf8",
  "#34d399",
  "#fbbf24",
  "#8b5cf6",
  "#f87171",
  "#14b8a6",
  "#e879f9",
];

// ── State ─────────────────────────────────────────────────────────
const history = {
  throughput: [], // { ts, value }
  p50: [],
  p95: [],
  stageLatency: {},
  actorLatency: {},
};
let lastInflight = null;
let windowSeconds = 60;
let latestRows = [];
let latestLedger = { chains: [], entries: [], summary: {} };
let ledgerEntries = [];
let ledgerCursor = "";
let ledgerHasMore = false;
let ledgerLoading = false;
let ledgerPages = [];
let ledgerPageIndex = 0;
let activeTab = "dashboard";
let selectedLedgerKey = "";
let renderedLedgerDetailKey = "";
let loadingLedgerDetailKey = "";

// ── DOM ───────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const els = {
  upstreamPill: $("upstream-pill"),
  windowPill: $("window-pill"),
  lastPollPill: $("last-poll-pill"),
  endpointValue: $("endpoint-value"),
  healthValue: $("health-value"),
  sampleValue: $("sample-value"),
  inflight: $("stat-inflight"),
  inflightCard: document.querySelector('[data-stat="inflight"]'),
  completed: $("stat-completed"),
  completedSub: $("stat-completed-sub"),
  throughput: $("stat-throughput"),
  throughputSub: $("stat-throughput-sub"),
  throughputChartSub: $("throughput-chart-sub"),
  success: $("stat-success"),
  successSub: $("stat-success-sub"),
  successCard: document.querySelector('[data-stat="success"]'),
  p50: $("stat-p50"),
  p95: $("stat-p95"),
  p99: $("stat-p99"),
  errors: $("stat-errors"),
  errorsSub: $("stat-errors-sub"),
  errorsCard: document.querySelector('[data-stat="errors"]'),
  bySource: $("by-source"),
  byTool: $("by-tool"),
  agentActivitySub: $("agent-activity-sub"),
  agentActivityBody: $("agent-activity-body"),
  stageLatencyBody: $("stage-latency-body"),
  actorLatencyBody: $("actor-latency-body"),
  byDecisionOutcome: $("by-decision-outcome"),
  byRisk: $("by-risk"),
  byErrorClass: $("by-error-class"),
  byMissingPermission: $("by-missing-permission"),
  byCodeReview: $("by-code-review"),
  recentSub: $("recent-sub"),
  recentBody: $("recent-body"),
  requestFilter: $("request-filter"),
  sourceFilter: $("source-filter"),
  stateFilter: $("state-filter"),
  outcomeFilter: $("outcome-filter"),
  riskFilter: $("risk-filter"),
  statusFilter: $("status-filter"),
  requestSort: $("request-sort"),
  clearFilters: $("clear-filters"),
  ledgerSub: $("ledger-sub"),
  ledgerChainCount: $("ledger-chain-count"),
  ledgerEntryCount: $("ledger-entry-count"),
  ledgerIntegrity: $("ledger-integrity"),
  ledgerSignedCount: $("ledger-signed-count"),
  ledgerFilter: $("ledger-filter"),
  ledgerChainFilter: $("ledger-chain-filter"),
  ledgerTypeFilter: $("ledger-type-filter"),
  ledgerIntegrityFilter: $("ledger-integrity-filter"),
  ledgerList: $("ledger-list"),
  ledgerDetail: $("ledger-detail"),
  ledgerPageStatus: $("ledger-page-status"),
  ledgerPrevPage: $("ledger-prev-page"),
  ledgerNextPage: $("ledger-next-page"),
  chartThroughput: $("chart-throughput"),
  chartLatency: $("chart-latency"),
  chartStageLatency: $("chart-stage-latency"),
  chartActorLatency: $("chart-actor-latency"),
  stageLatencyChartSub: $("stage-latency-chart-sub"),
  actorLatencyChartSub: $("actor-latency-chart-sub"),
};

// ── Boot ──────────────────────────────────────────────────────────
(async function boot() {
  wireTabs();
  wireCollapsibleRows();
  wireTableControls();
  wireLedgerControls();
  await loadConfig();
  tick();
  setInterval(tick, POLL_MS);
})();

function wireTabs() {
  const buttons = Array.from(document.querySelectorAll("[data-tab-target]"));
  const dash = document.querySelector(".dash");
  const panels = {
    dashboard: $("tab-dashboard"),
    requests: $("tab-requests"),
    ledger: $("tab-ledger"),
  };
  dash?.setAttribute("data-active-tab", "dashboard");
  for (const button of buttons) {
    button.addEventListener("click", () => {
      const target = button.dataset.tabTarget;
      if (target) activeTab = target;
      if (target) dash?.setAttribute("data-active-tab", target);
      for (const candidate of buttons) {
        candidate.classList.toggle("active", candidate === button);
      }
      for (const [name, panel] of Object.entries(panels)) {
        if (!panel) continue;
        const active = name === target;
        panel.hidden = !active;
        panel.classList.toggle("active", active);
      }
      drawCharts();
      if (target === "ledger" && ledgerEntries.length === 0 && !ledgerLoading) {
        resetAndLoadLedger();
      }
    });
  }
}

function wireLedgerControls() {
  for (const el of [
    els.ledgerFilter,
    els.ledgerChainFilter,
    els.ledgerTypeFilter,
    els.ledgerIntegrityFilter,
  ]) {
    el.addEventListener("input", () => resetAndLoadLedger());
  }
  els.ledgerPrevPage.addEventListener("click", () => showLedgerPage(ledgerPageIndex - 1));
  els.ledgerNextPage.addEventListener("click", () => showLedgerPage(ledgerPageIndex + 1));
}

function wireCollapsibleRows() {
  for (const row of document.querySelectorAll("[data-collapse-key]")) {
    const key = row.dataset.collapseKey;
    const title = row.dataset.collapseTitle || key || "Section";
    const storageKey = `council-monitor:collapsed:${key}`;
    const content = document.createElement("div");
    content.className = "row-content";
    while (row.firstChild) content.appendChild(row.firstChild);

    const toggle = document.createElement("button");
    toggle.className = "row-toggle";
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "true");
    toggle.innerHTML = `
      <span class="row-toggle-title">${escapeHtml(title)}</span>
      <span class="row-toggle-state">collapse</span>`;

    row.classList.add("monitor-row");
    row.append(toggle, content);
    const collapsed = localStorage.getItem(storageKey) === "1";
    setRowCollapsed(row, toggle, collapsed);
    toggle.addEventListener("click", () => {
      const nextCollapsed = !row.classList.contains("collapsed");
      setRowCollapsed(row, toggle, nextCollapsed);
      localStorage.setItem(storageKey, nextCollapsed ? "1" : "0");
      drawCharts();
    });
  }
}

function setRowCollapsed(row, toggle, collapsed) {
  row.classList.toggle("collapsed", collapsed);
  toggle.setAttribute("aria-expanded", String(!collapsed));
  const state = toggle.querySelector(".row-toggle-state");
  if (state) state.textContent = collapsed ? "expand" : "collapse";
}

function wireTableControls() {
  for (const el of [
    els.requestFilter,
    els.sourceFilter,
    els.stateFilter,
    els.outcomeFilter,
    els.riskFilter,
    els.statusFilter,
    els.requestSort,
  ]) {
    el.addEventListener("input", () => renderRecent(latestRows));
  }
  els.clearFilters.addEventListener("click", () => {
    els.requestFilter.value = "";
    els.sourceFilter.value = "all";
    els.stateFilter.value = "all";
    els.outcomeFilter.value = "all";
    els.riskFilter.value = "all";
    els.statusFilter.value = "all";
    els.requestSort.value = "recent";
    renderRecent(latestRows);
  });
}

async function loadConfig() {
  try {
    const r = await fetch("/api/config");
    if (!r.ok) return;
    const cfg = await r.json();
    windowSeconds = Number(cfg.window_seconds || 60);
    els.windowPill.textContent = `window: ${formatWindow(windowSeconds)}`;
    els.endpointValue.textContent = `/mcp -> ${trimUrl(cfg.upstream_url || "upstream")}`;
  } catch (_) {
    /* ignore — config is optional */
  }
}

async function tick() {
  const t0 = performance.now();
  try {
    const [metrics, recent, health] = await Promise.all([
      fetchJson("/api/metrics"),
      fetchJson("/api/recent?limit=50"),
      fetchJson("/healthz").catch(() => null),
    ]);
    renderStats(metrics);
    renderBreakdowns(metrics);
    renderAgentActivity(metrics);
    renderCouncilLatency(metrics);
    renderDecisionInsights(metrics);
    renderRecent(recent.rows);
    if (activeTab === "ledger" && ledgerEntries.length === 0 && !ledgerLoading) {
      resetAndLoadLedger();
    }
    pushHistory(metrics);
    drawCharts();
    if (health) renderHealth(health);
    const ms = Math.round(performance.now() - t0);
    els.lastPollPill.textContent = `last poll: ${ms}ms`;
    els.sampleValue.textContent = metrics.asof_ms
      ? new Date(metrics.asof_ms).toLocaleTimeString()
      : "fresh";
    els.lastPollPill.classList.remove("pill-bad");
    els.lastPollPill.classList.add("pill-meta");
  } catch (err) {
    els.lastPollPill.textContent = "last poll: failed";
    els.lastPollPill.classList.remove("pill-meta", "pill-ok");
    els.lastPollPill.classList.add("pill-bad");
    console.error("poll failed:", err);
  }
}

async function fetchJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return r.json();
}

// ── Renderers ─────────────────────────────────────────────────────
function renderStats(m) {
  // Inflight
  const inflight = m.inflight ?? 0;
  if (lastInflight !== null && lastInflight !== inflight) {
    els.inflight.parentElement.classList.add("changed");
    setTimeout(
      () => els.inflight.parentElement.classList.remove("changed"),
      400,
    );
  }
  els.inflight.textContent = inflight;
  els.inflightCard.classList.toggle("active", inflight > 0);
  lastInflight = inflight;

  const completed = m.completed_in_window ?? 0;
  const councilCompleted = m.council_completed_in_window ?? councilRequestCount(m);
  const otherCompleted = Math.max(0, completed - councilCompleted);
  els.completed.textContent = completed;
  els.completedSub.textContent = `${councilCompleted} council / ${otherCompleted} other`;

  const effectiveWindow = m.window_seconds || windowSeconds;
  const councilTpm = m.council_throughput_per_min
    ?? councilCompleted * (60 / effectiveWindow);
  els.throughput.textContent = formatThroughput(councilTpm);
  els.throughputSub.textContent =
    `council req/min (${councilCompleted} in ${formatWindow(effectiveWindow)})`;
  els.throughputChartSub.textContent =
    `${formatThroughput(councilTpm)} council req/min, ${otherCompleted} protocol/other in window`;

  const errorRate = m.error_rate || 0;
  const successRate = completed > 0 ? Math.max(0, 1 - errorRate) : null;
  els.success.textContent = successRate == null
    ? "—"
    : `${(successRate * 100).toFixed(1)}%`;
  els.successSub.textContent = completed > 0
    ? `${completed - (m.error_count ?? 0)} ok / ${completed}`
    : "no samples";
  els.successCard.classList.remove("warn", "bad");
  if (successRate != null && successRate < 0.95) els.successCard.classList.add("bad");
  else if (successRate != null && successRate < 0.99) els.successCard.classList.add("warn");

  // Latency
  const lat = m.latency_ms || { p50: 0, p95: 0, p99: 0 };
  els.p50.textContent = formatMs(lat.p50);
  els.p95.textContent = formatMs(lat.p95);
  els.p99.textContent = formatMs(lat.p99);
  applyLatencyTone("p95", lat.p95);
  applyLatencyTone("p99", lat.p99);

  // Errors
  const errs = m.error_count ?? 0;
  els.errors.textContent = errs;
  els.errorsSub.textContent = `${(m.error_rate * 100 || 0).toFixed(1)}% in window`;
  els.errorsCard.classList.toggle("bad", errs > 0);
}

function applyLatencyTone(stat, ms) {
  const card = document.querySelector(`[data-stat="${stat}"]`);
  if (!card) return;
  card.classList.remove("warn", "bad");
  if (ms >= 5000) card.classList.add("bad");
  else if (ms >= 1500) card.classList.add("warn");
}

function renderBreakdowns(m) {
  renderBars(els.bySource, m.by_source_total || m.by_source || {}, "source");
  renderBars(els.byTool, m.by_tool_total || m.by_tool || {}, "tool");
}

function renderAgentActivity(m) {
  const activity = m.agent_activity || {};
  const rows = Object.values(activity)
    .filter((row) => row && row.agent_id)
    .sort((a, b) => {
      const totalDelta = (b.completed_total || 0) - (a.completed_total || 0);
      if (totalDelta) return totalDelta;
      return (b.last_seen_ms || 0) - (a.last_seen_ms || 0);
    });

  if (!rows.length) {
    els.agentActivityBody.innerHTML =
      '<tr class="empty"><td colspan="6">waiting for attributed calls</td></tr>';
    els.agentActivitySub.textContent = "calls attributed by agent_id";
    return;
  }

  const totalCalls = rows.reduce((sum, row) => sum + (row.completed_total || 0), 0);
  const windowCalls = rows.reduce((sum, row) => sum + (row.completed_in_window || 0), 0);
  els.agentActivitySub.textContent =
    `${rows.length} account(s), ${totalCalls} total / ${windowCalls} in window`;
  els.agentActivityBody.innerHTML = rows
    .map((row) => `
      <tr>
        <td title="${escapeHtml(row.agent_id)}"><span class="agent-chip">${escapeHtml(row.agent_id)}</span></td>
        <td class="num">${row.completed_total || 0}</td>
        <td class="num">${row.completed_in_window || 0}</td>
        <td class="num ${row.error_total ? "bad" : ""}">${row.error_total || 0}</td>
        <td title="${formatTimestamp(row.last_seen_ms)}">${formatLastSeen(row.last_seen_ms)}</td>
        <td>${formatToolMix(row.by_tool_total || row.by_tool || {})}</td>
      </tr>`)
    .join("");
}

function renderDecisionInsights(m) {
  renderBars(
    els.byDecisionOutcome,
    m.by_decision_outcome_total || m.by_decision_outcome || {},
    "outcome",
  );
  renderBars(els.byRisk, m.by_risk_total || m.by_risk || {}, "risk");
  renderBars(els.byErrorClass, m.by_error_class_total || m.by_error_class || {}, "reason");
  renderBars(
    els.byMissingPermission,
    m.by_missing_permission_total || m.by_missing_permission || {},
    "permission",
  );
  renderBars(els.byCodeReview, m.by_code_review_total || m.by_code_review || {}, "review");
}

function renderCouncilLatency(m) {
  renderStageLatency(m.council_stage_latency || {});
  renderActorLatency(m.council_actor_latency || {});
}

function renderStageLatency(stages) {
  const rows = Object.values(stages).sort((a, b) => (b.p95_ms || 0) - (a.p95_ms || 0));
  if (rows.length === 0) {
    els.stageLatencyBody.innerHTML =
      '<tr class="empty"><td colspan="6">waiting for council telemetry</td></tr>';
    return;
  }
  els.stageLatencyBody.innerHTML = rows
    .map((row) => `
      <tr>
        <td title="${escapeHtml(row.stage || "")}">${escapeHtml(row.stage || "unknown")}</td>
        <td class="num">${row.count || 0}</td>
        <td class="num">${formatDuration(row.avg_ms || 0)}</td>
        <td class="num ${latencyTone(row.p95_ms)}">${formatDuration(row.p95_ms || 0)}</td>
        <td class="num ${latencyTone(row.max_ms)}">${formatDuration(row.max_ms || 0)}</td>
        <td class="num">${formatDuration(row.last_ms || 0)}</td>
      </tr>`)
    .join("");
}

function renderActorLatency(actors) {
  const rows = Object.values(actors).sort((a, b) => (b.p95_ms || 0) - (a.p95_ms || 0));
  if (rows.length === 0) {
    els.actorLatencyBody.innerHTML =
      '<tr class="empty"><td colspan="7">waiting for council telemetry</td></tr>';
    return;
  }
  els.actorLatencyBody.innerHTML = rows
    .map((row) => {
      const actor = row.role === "chairman" || row.role === "code_review"
        ? row.role
        : row.member_id || row.role || "unknown";
      return `
        <tr>
          <td title="${escapeHtml(row.role || "")}">${escapeHtml(actor)}</td>
          <td title="${escapeHtml(row.provider || "")}">${escapeHtml(row.provider || "—")}</td>
          <td class="num">${row.count || 0}</td>
          <td class="num">${formatDuration(row.avg_ms || 0)}</td>
          <td class="num ${latencyTone(row.p95_ms)}">${formatDuration(row.p95_ms || 0)}</td>
          <td class="num ${latencyTone(row.max_ms)}">${formatDuration(row.max_ms || 0)}</td>
          <td class="num" title="${escapeHtml(row.last_decision || "")}">${formatDuration(row.last_ms || 0)}</td>
        </tr>`;
    })
    .join("");
}

function renderBars(container, counts, kind) {
  const entries = Object.entries(counts).sort(([, a], [, b]) => b - a);
  const total = entries.reduce((sum, [, count]) => sum + count, 0);
  if (entries.length === 0) {
    container.innerHTML = '<li class="bar-empty">no traffic recorded</li>';
    return;
  }
  const max = Math.max(...entries.map(([, v]) => v));
  container.innerHTML = entries
    .map(([name, count]) => {
      const pct = max > 0 ? Math.max(2, (count / max) * 100) : 0;
      const share = total > 0 ? (count / total) * 100 : 0;
      const cls = `${kind}-${escape(String(name).toLowerCase())}`;
      return `
        <li class="bar-row">
          <span class="bar-label" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
          <span class="bar-track">
            <span class="bar-fill ${cls}" style="width:${pct.toFixed(1)}%"></span>
          </span>
          <span class="bar-count">${count}<span class="bar-share">${share.toFixed(0)}%</span></span>
        </li>`;
    })
    .join("");
}

function renderRecent(rows) {
  latestRows = Array.isArray(rows) ? rows : [];
  const viewRows = filterAndSortRows(latestRows);
  if (latestRows.length === 0) {
    els.recentBody.innerHTML =
      '<tr class="empty"><td colspan="10">no requests yet</td></tr>';
    els.recentSub.textContent = "inflight + last 50 completed";
    return;
  }
  if (viewRows.length === 0) {
    els.recentBody.innerHTML =
      '<tr class="empty"><td colspan="10">no matching requests</td></tr>';
    els.recentSub.textContent = `0 shown from ${latestRows.length} recent`;
    return;
  }
  els.recentBody.innerHTML = viewRows
    .map((r) => {
      const stateClass = r.state === "inflight"
        ? "inflight"
        : `completed ${r.ok ? "ok" : "bad"}`;
      const stateLabel = r.state === "inflight"
        ? "inflight"
        : r.ok ? "ok" : "fail";
      const tool = r.tool
        ? escapeHtml(r.tool)
        : `<span class="muted">${escapeHtml(r.method || "-")}</span>`;
      const decision = r.decision || null;
      const request = r.request || (decision || {}).request || {};
      const agent = request.agent_id || "";
      const outcome = decision ? decision.outcome || "unknown" : "";
      const risk = decision ? decision.risk_level || "" : "";
      const voteText = decision
        ? `${decision.approval_votes || 0}/${decision.denial_votes || 0}/${decision.abstain_votes || 0}`
        : "—";
      const detailTitle = decisionTitle(decision);
      const status = formatStatus(r.status_code, r.ok);
      const dur = r.duration_ms != null ? formatDuration(r.duration_ms) : "…";
      const age = r.state === "inflight"
        ? formatAge(r.age_ms || 0)
        : `${formatAge(r.age_ms || 0)} ago`;
      const sourceClass = `source-${escape(r.source || "direct")}`;
      const durationTone = r.duration_ms >= 5000
        ? "bad"
        : r.duration_ms >= 1500 ? "warn" : "";
      return `
        <tr class="${r.state}">
          <td><span class="state-chip ${stateClass}">${stateLabel}</span></td>
          <td>${tool}</td>
          <td><span class="source-chip ${sourceClass}">${escapeHtml(r.source || "direct")}</span></td>
          <td title="${escapeHtml(agent)}">${agent ? `<span class="agent-chip">${escapeHtml(agent)}</span>` : '<span class="muted">—</span>'}</td>
          <td>${formatOutcome(outcome, detailTitle)}</td>
          <td>${formatRisk(risk)}</td>
          <td class="num" title="approve / deny / abstain">${voteText}</td>
          <td>${status}</td>
          <td class="num duration ${durationTone}">${dur}</td>
          <td class="num">${age}</td>
        </tr>`;
    })
    .join("");
  const inflight = viewRows.filter((r) => r.state === "inflight").length;
  const suffix = viewRows.length === latestRows.length ? "shown" : `shown from ${latestRows.length}`;
  els.recentSub.textContent = `${inflight} inflight, ${viewRows.length - inflight} completed ${suffix}`;
}

async function resetAndLoadLedger() {
  ledgerEntries = [];
  ledgerCursor = "";
  ledgerHasMore = false;
  ledgerPages = [];
  ledgerPageIndex = 0;
  selectedLedgerKey = "";
  renderedLedgerDetailKey = "";
  loadingLedgerDetailKey = "";
  els.ledgerList.innerHTML = '<div class="ledger-empty">loading ledger records</div>';
  els.ledgerDetail.innerHTML = '<div class="ledger-empty">select a ledger entry</div>';
  await fetchLedgerPage({ cursor: "", pageIndex: 0 });
}

async function showLedgerPage(pageIndex) {
  if (ledgerLoading || pageIndex < 0) return;
  const cached = ledgerPages[pageIndex];
  if (cached) {
    renderLedgerPage(cached, pageIndex);
    return;
  }
  if (pageIndex !== ledgerPageIndex + 1 || !ledgerCursor || !ledgerHasMore) return;
  await fetchLedgerPage({ cursor: ledgerCursor, pageIndex });
}

async function fetchLedgerPage({ cursor, pageIndex }) {
  if (ledgerLoading) return;
  ledgerLoading = true;
  updateLedgerPagination();
  els.ledgerPageStatus.textContent = pageIndex > 0 ? "Loading next page..." : "Loading ledger...";
  try {
    const params = new URLSearchParams({
      limit: String(LEDGER_PAGE_SIZE),
      chain_id: els.ledgerChainFilter.value === "all" ? "" : els.ledgerChainFilter.value,
      entry_type: els.ledgerTypeFilter.value === "all" ? "" : els.ledgerTypeFilter.value,
      query: els.ledgerFilter.value.trim(),
      integrity: els.ledgerIntegrityFilter.value === "all" ? "" : els.ledgerIntegrityFilter.value,
    });
    if (cursor) params.set("cursor", cursor);
    const ledger = await fetchJson(`/api/ledger?${params.toString()}`);
    const page = {
      snapshot: ledger || { chains: [], entries: [], summary: {} },
      nextCursor: (ledger || {}).next_cursor || "",
      hasMore: Boolean((ledger || {}).has_more && (ledger || {}).next_cursor),
    };
    ledgerPages[pageIndex] = page;
    renderLedgerPage(page, pageIndex);
  } catch (err) {
    if (els.ledgerSub) els.ledgerSub.textContent = "ledger unavailable";
    if (els.ledgerList) {
      els.ledgerList.innerHTML = '<div class="ledger-empty">ledger unavailable</div>';
    }
    console.error("ledger fetch failed:", err);
  } finally {
    ledgerLoading = false;
    updateLedgerPagination();
  }
}

function renderLedgerPage(page, pageIndex) {
  ledgerPageIndex = pageIndex;
  latestLedger = page.snapshot || { chains: [], entries: [], summary: {} };
  ledgerEntries = latestLedger.entries || [];
  ledgerCursor = page.nextCursor || "";
  ledgerHasMore = Boolean(page.hasMore && ledgerCursor);
  selectedLedgerKey = "";
  renderedLedgerDetailKey = "";
  loadingLedgerDetailKey = "";
  els.ledgerList.scrollTop = 0;
  renderLedger(latestLedger);
}

function renderLedger(snapshot) {
  latestLedger = snapshot || { chains: [], entries: [], summary: {} };
  const summary = latestLedger.summary || {};
  const chains = latestLedger.chains || [];
  const entries = ledgerEntries;

  els.ledgerChainCount.textContent = summary.chain_count ?? 0;
  els.ledgerEntryCount.textContent = summary.entry_count ?? 0;
  const invalid = summary.invalid_chain_count ?? 0;
  els.ledgerIntegrity.textContent = invalid ? `${invalid} broken` : "valid";
  els.ledgerIntegrity.className = invalid ? "bad" : "ok";
  els.ledgerSignedCount.textContent = `${summary.signed_entry_count ?? 0}/${summary.entry_count ?? 0}`;
  els.ledgerSub.textContent = chains.length
    ? `${chains.length} chain(s), newest entries first`
    : "no ledger files found";

  updateLedgerFilterOptions(chains, entries);
  const rows = entries;
  if (!selectedLedgerKey && rows.length) selectedLedgerKey = ledgerKey(rows[0]);
  if (selectedLedgerKey && !rows.some((row) => ledgerKey(row) === selectedLedgerKey)) {
    selectedLedgerKey = rows.length ? ledgerKey(rows[0]) : "";
  }

  if (!rows.length) {
    renderedLedgerDetailKey = "";
    loadingLedgerDetailKey = "";
    els.ledgerList.innerHTML = '<div class="ledger-empty">no matching ledger entries</div>';
    els.ledgerDetail.innerHTML = '<div class="ledger-empty">select a ledger entry</div>';
    return;
  }

  els.ledgerList.innerHTML = rows.map(renderLedgerRow).join("");
  for (const item of els.ledgerList.querySelectorAll("[data-ledger-key]")) {
    item.addEventListener("click", () => {
      const nextKey = item.getAttribute("data-ledger-key") || "";
      if (nextKey === selectedLedgerKey) return;
      selectedLedgerKey = nextKey;
      renderLedger(latestLedger);
    });
  }
  if (
    selectedLedgerKey
    && selectedLedgerKey !== renderedLedgerDetailKey
    && selectedLedgerKey !== loadingLedgerDetailKey
  ) {
    loadLedgerDetail(selectedLedgerKey);
  }
  updateLedgerPagination();
}

function updateLedgerPagination() {
  const summary = (latestLedger || {}).summary || {};
  const totalEntries = summary.entry_count ?? 0;
  const pageLabel = ledgerEntries.length
    ? `Page ${ledgerPageIndex + 1}: ${ledgerEntries.length} shown, ${totalEntries} ledger entries`
    : "No page loaded";
  els.ledgerPageStatus.textContent = ledgerLoading ? els.ledgerPageStatus.textContent : pageLabel;
  els.ledgerPrevPage.disabled = ledgerLoading || ledgerPageIndex === 0;
  els.ledgerNextPage.disabled = ledgerLoading || (!ledgerHasMore && !ledgerPages[ledgerPageIndex + 1]);
}

function updateLedgerFilterOptions(chains, entries) {
  updateSelectOptions(
    els.ledgerChainFilter,
    chains.map((chain) => chain.chain_id).filter(Boolean),
    "All",
  );
  updateSelectOptions(
    els.ledgerTypeFilter,
    [
      ...new Set(
        chains.flatMap((chain) => Object.keys(chain.type_counts || {}))
          .concat(entries.map((entry) => entry.entry_type))
          .filter(Boolean),
      ),
    ].sort((a, b) => a.localeCompare(b)),
    "All",
  );
}

function updateSelectOptions(select, values, allLabel) {
  const current = select.value || "all";
  const options = [`<option value="all">${escapeHtml(allLabel)}</option>`]
    .concat(values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`));
  select.innerHTML = options.join("");
  select.value = values.includes(current) ? current : "all";
}

function renderLedgerRow(entry) {
  const key = ledgerKey(entry);
  const selected = key === selectedLedgerKey ? " selected" : "";
  const integrity = entry.hash_valid && entry.link_valid ? "valid" : "broken";
  const signed = entry.signature_count ? "signed" : "unsigned";
  return `
    <button class="ledger-row${selected}" type="button" data-ledger-key="${escapeHtml(key)}">
      <span class="ledger-row-top">
        <span class="ledger-seq">#${entry.sequence}</span>
        <span class="ledger-type">${escapeHtml(entry.entry_type || "unknown")}</span>
        <span class="ledger-chip ${integrity}">${integrity}</span>
        <span class="ledger-chip ${signed}">${signed}</span>
      </span>
      <span class="ledger-row-main">${escapeHtml(entry.intent_id || entry.command || entry.content_hash || "ledger entry")}</span>
      <span class="ledger-row-meta">
        ${escapeHtml(entry.chain_id || "chain")} · ${escapeHtml(formatLedgerTime(entry.timestamp))}
      </span>
    </button>`;
}

async function loadLedgerDetail(key) {
  if (!key) return;
  if (key === renderedLedgerDetailKey || key === loadingLedgerDetailKey) return;
  loadingLedgerDetailKey = key;
  const [chainId, sequence] = key.split("::");
  try {
    const detail = await fetchJson(`/api/ledger/${encodeURIComponent(chainId)}/${encodeURIComponent(sequence)}`);
    if (selectedLedgerKey === key) {
      renderLedgerDetail(detail);
      renderedLedgerDetailKey = key;
    }
  } catch (err) {
    if (selectedLedgerKey === key) {
      renderedLedgerDetailKey = "";
      els.ledgerDetail.innerHTML = '<div class="ledger-empty">entry detail unavailable</div>';
    }
    console.error("ledger detail fetch failed:", err);
  } finally {
    if (loadingLedgerDetailKey === key) loadingLedgerDetailKey = "";
  }
}

function renderLedgerDetail(entry) {
  const integrity = entry.hash_valid && entry.link_valid;
  els.ledgerDetail.innerHTML = `
    <div class="ledger-detail-head">
      <div>
        <div class="ledger-detail-title">${escapeHtml(entry.entry_type || "ledger entry")} #${entry.sequence}</div>
        <div class="ledger-detail-sub">${escapeHtml(entry.chain_id || "")}</div>
      </div>
      <span class="ledger-chip ${integrity ? "valid" : "broken"}">${integrity ? "valid chain" : "broken chain"}</span>
    </div>
    <div class="ledger-facts">
      ${ledgerFact("Timestamp", formatLedgerTime(entry.timestamp))}
      ${ledgerFact("Agent", entry.agent_id || "—")}
      ${ledgerFact("Intent", entry.intent_id || "—")}
      ${ledgerFact("Decision", entry.decision || "—")}
      ${ledgerFact("Signatures", `${entry.signature_count || 0}`)}
    </div>
    <div class="hash-grid">
      ${hashBlock("Content hash", entry.content_hash, entry.hash_valid)}
      ${hashBlock("Expected hash", entry.expected_hash, entry.hash_valid)}
      ${hashBlock("Previous hash", entry.previous_hash || "genesis", entry.link_valid)}
    </div>
    <div class="payload-block">
      <div class="payload-title">Redacted payload</div>
      <pre>${escapeHtml(JSON.stringify(entry.payload || {}, null, 2))}</pre>
    </div>
    <div class="payload-block">
      <div class="payload-title">Metadata</div>
      <pre>${escapeHtml(JSON.stringify(entry.metadata || {}, null, 2))}</pre>
    </div>`;
}

function ledgerFact(label, value) {
  return `<div class="ledger-fact"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function hashBlock(label, value, ok) {
  return `
    <div class="hash-block ${ok ? "ok" : "bad"}">
      <span>${escapeHtml(label)}</span>
      <code>${escapeHtml(value || "—")}</code>
    </div>`;
}

function ledgerKey(entry) {
  return `${entry.chain_id}::${entry.sequence}`;
}

function ledgerSearchText(entry) {
  return [
    entry.chain_id,
    entry.entry_type,
    entry.intent_id,
    entry.agent_id,
    entry.decision,
    entry.command,
    entry.content_hash,
    entry.previous_hash,
    (entry.signer_ids || []).join(" "),
  ].filter(Boolean).join(" ").toLowerCase();
}

function filterAndSortRows(rows) {
  const query = els.requestFilter.value.trim().toLowerCase();
  const source = els.sourceFilter.value;
  const state = els.stateFilter.value;
  const status = els.statusFilter.value;
  const outcome = els.outcomeFilter.value;
  const risk = els.riskFilter.value;
  const sort = els.requestSort.value;

  const filtered = rows.filter((r) => {
    if (source !== "all") {
      const rowSource = r.source || "direct";
      if (source === "other") {
        if (KNOWN_SOURCES.has(rowSource)) return false;
      } else if (rowSource !== source) {
        return false;
      }
    }
    if (state !== "all") {
      if (state === "ok") {
        if (r.ok !== true) return false;
      } else if (state === "failed") {
        if (r.ok !== false) return false;
      } else if (r.state !== state) {
        return false;
      }
    }
    const rowOutcome = (r.decision || {}).outcome || "none";
    if (outcome !== "all" && rowOutcome !== outcome) return false;
    const rowRisk = (r.decision || {}).risk_level || "none";
    if (risk !== "all" && rowRisk !== risk) return false;
    if (status !== "all" && statusBucket(r.status_code) !== status) return false;
    if (query && !rowSearchText(r).includes(query)) return false;
    return true;
  });

  if (sort === "recent") return filtered;
  return [...filtered].sort((a, b) => compareRows(a, b, sort));
}

function compareRows(a, b, sort) {
  if (sort === "oldest") return (b.age_ms || 0) - (a.age_ms || 0);
  if (sort === "duration_desc") return (b.duration_ms ?? -1) - (a.duration_ms ?? -1);
  if (sort === "duration_asc") return (a.duration_ms ?? Infinity) - (b.duration_ms ?? Infinity);
  if (sort === "status_desc") return (b.status_code ?? -1) - (a.status_code ?? -1);
  if (sort === "status_asc") return (a.status_code ?? Infinity) - (b.status_code ?? Infinity);
  if (sort === "outcome") {
    return ((a.decision || {}).outcome || "").localeCompare((b.decision || {}).outcome || "");
  }
  if (sort === "risk") return riskRank(b) - riskRank(a);
  if (sort === "tool") return rowName(a).localeCompare(rowName(b));
  if (sort === "source") return (a.source || "direct").localeCompare(b.source || "direct");
  return 0;
}

function rowName(row) {
  return row.tool || row.method || "";
}

function rowSearchText(row) {
  return [
    row.state,
    row.tool,
    row.method,
    row.source || "direct",
    row.status_code,
    statusBucket(row.status_code),
    (row.decision || {}).outcome,
    (row.decision || {}).risk_level,
    (row.decision || {}).error_class,
    ((row.decision || {}).missing_permissions || []).join(" "),
    (row.request || {}).agent_id,
    (row.request || {}).command_executable,
    ((row.decision || {}).request || {}).agent_id,
    ((row.decision || {}).request || {}).command_executable,
    row.ok === true ? "ok" : row.ok === false ? "fail failed" : "",
  ]
    .filter((v) => v != null)
    .join(" ")
    .toLowerCase();
}

function riskRank(row) {
  const risk = ((row.decision || {}).risk_level || "").toLowerCase();
  return { critical: 4, high: 3, medium: 2, low: 1 }[risk] || 0;
}

function statusBucket(statusCode) {
  if (statusCode == null) return "none";
  const numeric = Number(statusCode);
  if (numeric >= 200 && numeric < 300) return "2xx";
  if (numeric >= 400 && numeric < 500) return "4xx";
  if (numeric >= 500 && numeric < 600) return "5xx";
  return `${Math.floor(numeric / 100)}xx`;
}

function renderHealth(h) {
  if (!h || !h.upstream) return;
  if (h.upstream.ok) {
    els.upstreamPill.textContent = "upstream: ok";
    els.upstreamPill.className = "pill pill-ok";
    els.healthValue.textContent = `ok (${h.upstream.status_code || 200})`;
    els.healthValue.className = "overview-value ok";
  } else {
    const detail = h.upstream.error || h.upstream.status_code || "down";
    els.upstreamPill.textContent = `upstream: ${detail}`;
    els.upstreamPill.className = "pill pill-bad";
    els.healthValue.textContent = String(detail);
    els.healthValue.className = "overview-value bad";
  }
}

// ── Sparklines ────────────────────────────────────────────────────
function pushHistory(m) {
  const ts = Date.now();
  const effectiveWindow = m.window_seconds || windowSeconds;
  const councilTpm = m.council_throughput_per_min
    ?? councilRequestCount(m) * (60 / effectiveWindow);
  history.throughput.push({ ts, value: councilTpm });
  history.p50.push({ ts, value: (m.latency_ms || {}).p50 || 0 });
  history.p95.push({ ts, value: (m.latency_ms || {}).p95 || 0 });
  pushLatencyHistory(history.stageLatency, m.council_stage_latency_window || {}, "stage", ts);
  pushLatencyHistory(history.actorLatency, m.council_actor_latency_window || {}, "actor", ts);
  for (const k of ["throughput", "p50", "p95"]) {
    if (history[k].length > HISTORY_POINTS) history[k].shift();
  }
  pruneLatencyHistory(history.stageLatency, ts);
  pruneLatencyHistory(history.actorLatency, ts);
}

function drawCharts() {
  drawSparkline(
    els.chartThroughput,
    [{ data: history.throughput, color: "#d97706" }],
    { yLabel: "req/min", valueFormatter: formatRateAxis, emptyMax: 10 },
  );
  drawSparkline(els.chartLatency, [
    { data: history.p50, color: "#34d399" },
    { data: history.p95, color: "#fbbf24" },
  ], { yLabel: "latency", valueFormatter: formatAxisDuration, emptyMax: 1000 });
  const stageSeries = topLatencySeries(history.stageLatency);
  const actorSeries = topLatencySeries(history.actorLatency);
  drawSparkline(
    els.chartStageLatency,
    stageSeries,
    { fill: false, yLabel: "p95 latency", valueFormatter: formatAxisDuration, emptyMax: 1000 },
  );
  drawSparkline(
    els.chartActorLatency,
    actorSeries,
    { fill: false, yLabel: "p95 latency", valueFormatter: formatAxisDuration, emptyMax: 1000 },
  );
  els.stageLatencyChartSub.innerHTML = seriesLegend(stageSeries, "rolling p95 by stage");
  els.actorLatencyChartSub.innerHTML = seriesLegend(actorSeries, "rolling p95 by actor");
}

function pushLatencyHistory(store, rows, labelField, ts) {
  for (const [fallbackKey, row] of Object.entries(rows)) {
    const label = latencySeriesLabel(row, fallbackKey, labelField);
    if (!label) continue;
    if (!store[label]) store[label] = [];
    store[label].push({ ts, value: row.p95_ms || 0 });
    if (store[label].length > HISTORY_POINTS) store[label].shift();
  }
}

function pruneLatencyHistory(store, now) {
  const spanMs = POLL_MS * (HISTORY_POINTS - 1);
  for (const [label, points] of Object.entries(store)) {
    store[label] = points.filter((p) => now - p.ts <= spanMs);
    if (store[label].length === 0) delete store[label];
  }
}

function topLatencySeries(store) {
  return Object.entries(store)
    .map(([label, points], index) => ({
      label,
      data: points,
      color: SERIES_COLORS[index % SERIES_COLORS.length],
      latest: points.length ? points[points.length - 1].value : 0,
    }))
    .sort((a, b) => b.latest - a.latest);
}

function latencySeriesLabel(row, fallbackKey, labelField) {
  if (labelField === "stage") return row.stage || fallbackKey;
  if (row.role === "chairman" || row.role === "code_review") return row.role;
  return row.member_id || row.role || fallbackKey;
}

function seriesLegend(series, emptyText) {
  if (!series.length) return escapeHtml(emptyText);
  return series
    .map((s) => `<span class="chart-legend-item"><span class="legend-dot" style="background:${s.color}"></span>${escapeHtml(s.label)}</span>`)
    .join("");
}

function drawSparkline(canvas, series, options = {}) {
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 800;
  const cssH = canvas.clientHeight || 120;
  if (canvas.width !== cssW * dpr || canvas.height !== cssH * dpr) {
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  const hasData = series.some((s) => s.data.length > 1 && s.data.some((p) => p.value > 0));
  let max = hasData ? 1e-6 : (options.emptyMax || 1);
  for (const s of series) for (const p of s.data) if (p.value > max) max = p.value;
  if (hasData) max *= 1.12;

  const now = Date.now();
  const spanMs = POLL_MS * (HISTORY_POINTS - 1);
  const axis = chartAxis(cssW, cssH);
  drawAxes(ctx, axis, {
    max,
    spanMs,
    yLabel: options.yLabel || "",
    valueFormatter: options.valueFormatter || formatAxisNumber,
  });

  if (!hasData) {
    ctx.fillStyle = "#5d6d7e";
    ctx.font = "12px ui-monospace, Menlo, Consolas, monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("waiting for traffic", axis.left + axis.width / 2, axis.top + axis.height / 2);
    return;
  }

  for (const s of series) {
    if (s.data.length < 2) continue;
    const points = s.data;
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    points.forEach((p, i) => {
      const ageMs = Math.max(0, now - p.ts);
      const x = axis.right - Math.min(1, ageMs / spanMs) * axis.width;
      const y = axis.bottom - (p.value / max) * axis.height;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();

    if (options.fill !== false) {
      ctx.fillStyle = s.color + "22";
      const lastPoint = points[points.length - 1];
      const lastX = axis.right
        - Math.min(1, Math.max(0, now - lastPoint.ts) / spanMs) * axis.width;
      const firstX = axis.right
        - Math.min(1, Math.max(0, now - points[0].ts) / spanMs) * axis.width;
      ctx.lineTo(lastX, axis.bottom);
      ctx.lineTo(firstX, axis.bottom);
      ctx.closePath();
      ctx.fill();
    }
  }
}

function chartAxis(width, height) {
  const left = 78;
  const right = Math.max(left + 120, width - 18);
  const top = 30;
  const bottom = Math.max(top + 56, height - 32);
  return {
    left,
    right,
    top,
    bottom,
    width: right - left,
    height: bottom - top,
  };
}

function drawAxes(ctx, axis, options) {
  const ticks = [options.max, options.max / 2, 0];
  ctx.lineWidth = 1;
  ctx.font = "10px ui-monospace, Menlo, Consolas, monospace";
  ctx.textBaseline = "middle";
  ctx.textAlign = "right";
  for (const tick of ticks) {
    const y = axis.bottom - (tick / options.max) * axis.height;
    ctx.strokeStyle = tick === 0 ? "#3a4a58" : "#25313c";
    ctx.beginPath();
    ctx.moveTo(axis.left, y);
    ctx.lineTo(axis.right, y);
    ctx.stroke();
    ctx.fillStyle = "#8b9bad";
    ctx.fillText(options.valueFormatter(tick), axis.left - 10, y);
  }

  ctx.fillStyle = "#5d6d7e";
  ctx.textBaseline = "alphabetic";
  ctx.textAlign = "left";
  ctx.fillText(options.yLabel, axis.left, axis.top - 12);

  ctx.strokeStyle = "#2f3d49";
  ctx.beginPath();
  ctx.moveTo(axis.left, axis.top);
  ctx.lineTo(axis.left, axis.bottom);
  ctx.stroke();

  ctx.textBaseline = "top";
  ctx.textAlign = "left";
  ctx.fillStyle = "#6f8092";
  ctx.fillText(`-${formatWindow(Math.round(options.spanMs / 1000))}`, axis.left, axis.bottom + 9);
  ctx.textAlign = "right";
  ctx.fillText("now", axis.right, axis.bottom + 9);
}

// ── Helpers ───────────────────────────────────────────────────────
function formatMs(ms) {
  if (ms == null) return "—";
  if (ms < 1) return "0";
  if (ms < 1000) return `${Math.round(ms)}`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatDuration(ms) {
  if (ms == null) return "—";
  if (ms < 1) return "0ms";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatAxisDuration(ms) {
  if (ms == null || ms < 1) return "0";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatRateAxis(value) {
  if (value == null || value < 0.005) return "0";
  if (value < 10) return value.toFixed(1);
  return String(Math.round(value));
}

function formatThroughput(value) {
  if (value == null || value < 0.05) return "0";
  if (value < 10) return value.toFixed(1);
  return String(Math.round(value));
}

function formatAxisNumber(value) {
  if (value == null) return "0";
  if (Math.abs(value) >= 1000) return `${(value / 1000).toFixed(1)}k`;
  if (Math.abs(value) >= 10) return `${Math.round(value)}`;
  return value.toFixed(1);
}

function latencyTone(ms) {
  if (ms >= 5000) return "bad";
  if (ms >= 1500) return "warn";
  return "";
}

function formatAge(ms) {
  if (ms == null) return "—";
  if (ms < 1000) return "now";
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.round(minutes / 60)}h`;
}

function formatLedgerTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function formatLastSeen(ms) {
  if (!ms) return "—";
  const ageMs = Date.now() - Number(ms);
  if (ageMs < 0) return "now";
  return `${formatAge(ageMs)} ago`;
}

function formatTimestamp(ms) {
  if (!ms) return "—";
  const date = new Date(Number(ms));
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString();
}

function formatToolMix(counts) {
  const entries = Object.entries(counts).sort(([, a], [, b]) => b - a);
  if (!entries.length) return '<span class="muted">—</span>';
  const visible = entries.slice(0, 4).map(([tool, count]) => `
    <span class="tool-chip" title="${escapeHtml(tool)}">
      <span>${escapeHtml(tool)}</span><strong>${count}</strong>
    </span>`);
  const hidden = entries.length - visible.length;
  if (hidden > 0) visible.push(`<span class="tool-chip muted">+${hidden}</span>`);
  return `<span class="tool-mix">${visible.join("")}</span>`;
}

function formatWindow(seconds) {
  if (seconds < 60) return `${seconds}s`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

function formatStatus(statusCode, ok) {
  if (statusCode == null) return '<span class="status-code muted">—</span>';
  const numeric = Number(statusCode);
  const cls = !ok || numeric >= 500 ? "bad" : numeric >= 400 ? "warn" : "ok";
  return `<span class="status-code ${cls}">${escapeHtml(statusCode)}</span>`;
}

function formatOutcome(outcome, title) {
  if (!outcome) return '<span class="muted">—</span>';
  const normalized = String(outcome).toLowerCase();
  const cls = normalized === "approved" || normalized === "accepted"
    ? "ok"
    : normalized === "denied" || normalized === "error" ? "bad" : "warn";
  return `<span class="outcome-chip ${cls}" title="${escapeHtml(title || outcome)}">${escapeHtml(outcome)}</span>`;
}

function formatRisk(risk) {
  if (!risk) return '<span class="muted">—</span>';
  const normalized = String(risk).toLowerCase();
  const cls = ["critical", "high", "medium", "low"].includes(normalized)
    ? normalized
    : "unknown";
  return `<span class="risk-chip ${cls}">${escapeHtml(risk)}</span>`;
}

function decisionTitle(decision) {
  if (!decision) return "";
  const bits = [];
  if (decision.error_class) bits.push(`reason: ${decision.error_class}`);
  if (decision.missing_permissions && decision.missing_permissions.length) {
    bits.push(`missing: ${decision.missing_permissions.join(", ")}`);
  }
  if (decision.guidance_count) bits.push(`${decision.guidance_count} guidance item(s)`);
  if (decision.debate_rounds) bits.push(`${decision.debate_rounds} debate round(s)`);
  if (decision.writ_id) bits.push(`writ: ${decision.writ_id}`);
  return bits.join(" · ");
}

function trimUrl(url) {
  return String(url).replace(/^https?:\/\//, "");
}

function councilRequestCount(metrics) {
  const byTool = metrics.by_tool || {};
  let count = 0;
  for (const tool of COUNCIL_TOOLS) count += byTool[tool] || 0;
  return count;
}

function escape(s) {
  return String(s).replace(/[^a-zA-Z0-9_-]/g, "-");
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
