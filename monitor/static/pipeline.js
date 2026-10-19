// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// Pipeline visualization + per-API e2e + inference rollup panels.
// Self-contained: app.js calls globalThis.nomosPipeline.update(metrics) each poll.
// Hand-rolled SVG — no external dependencies (cluster has no CDN access).
(function () {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const TREND_POINTS = 300; // 5 min at 1s cadence

  // Canonical stage order; nodes render only for stages the council reports.
  // `llm: true` stages consume the shared inference endpoint.
  const STAGE_FLOW = [
    { id: "auth", label: "Auth" },
    { id: "evidence", label: "Evidence" },
    { id: "intent_build", label: "Build" },
    { id: "opa", label: "OPA" },
    { id: "code_review", label: "Code Review", llm: true, optional: true },
    { id: "chairman", label: "Chairman", llm: true, optional: true },
    { id: "review_collect", label: "Members", llm: true },
    { id: "tally", label: "Tally", optional: true },
    { id: "submit", label: "Ledger" },
    { id: "mint_writ", label: "Writ", optional: true },
  ];

  // With parallel member evaluation the council reports timing_model
  // "overlapping": these stages start together, so they are one step on the
  // critical path (its wall time is the `parallel_review_wall` stage), not two.
  const OVERLAP_GROUP = ["chairman", "review_collect"];

  const trend = new Map(); // stage -> ring buffer of p95 values
  let selectedStage = null;

  function pushTrend(stage, value) {
    let buf = trend.get(stage);
    if (!buf) {
      buf = [];
      trend.set(stage, buf);
    }
    buf.push(value);
    if (buf.length > TREND_POINTS) buf.shift();
  }

  function fmtTokens(n) {
    if (n == null || Number.isNaN(n)) return "—";
    if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
    if (n >= 1e4) return `${(n / 1e3).toFixed(1)}k`;
    return Math.round(n).toLocaleString();
  }

  function fmtMs(ms) {
    if (ms == null || Number.isNaN(ms)) return "—";
    if (ms >= 10000) return `${(ms / 1000).toFixed(1)}s`;
    if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
    return `${Math.round(ms)}ms`;
  }

  function heatColor(share) {
    // 0 → calm steel, 1 → hot red, through amber.
    const clamped = Math.max(0, Math.min(1, share));
    const hue = 210 - clamped * 190; // 210 (blue) → 20 (red-orange)
    const sat = 35 + clamped * 50;
    return `hsl(${hue}, ${sat}%, ${38 + clamped * 8}%)`;
  }

  function el(name, attrs, parent) {
    const node = document.createElementNS(SVG_NS, name);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    if (parent) parent.appendChild(node);
    return node;
  }

  function stageStats(metrics, id) {
    return (
      metrics.council_stage_latency_window?.[id] ||
      metrics.council_stage_latency?.[id] ||
      null
    );
  }

  // shared LLM endpoint node with limiter gauge
  // Most loaded limiter first, so the node shows the endpoint that matters.
  function rankedLimiters(metrics) {
    return [...(metrics.limiters || [])].sort(
      (a, b) => (b.projected_call_ms || 0) - (a.projected_call_ms || 0)
    );
  }

  function renderLlmEndpoint(svg, metrics, W) {
    const limiters = rankedLimiters(metrics);
    const limiter = limiters[0];
    const others = limiters.length - 1;
    const llm = metrics.llm_latency || {};
    const lg = el("g", { class: "pl-node pl-llm" }, svg);
    const pressure = limiter
      ? Math.min(1, (limiter.projected_call_ms || 0) / 120000)
      : 0;
    el("rect", {
      x: W / 2 - 170, y: 230, rx: 12, width: 340, height: 62,
      fill: heatColor(pressure), class: "pl-rect",
    }, lg);
    el("text", { x: W / 2, y: 252, class: "pl-label", "text-anchor": "middle" }, lg).textContent =
      limiter
        ? `LLM endpoint (${limiter.provider || "?"})${others > 0 ? ` · +${others} more` : ""}`
        : "LLM endpoint";
    el("text", { x: W / 2, y: 274, class: "pl-value", "text-anchor": "middle" }, lg).textContent =
      limiter
        ? `queue ${limiter.waiting}/${limiter.max_concurrent} slots · ewma ${fmtMs(limiter.latency_ewma_ms)} · projected ${fmtMs(limiter.projected_call_ms)}`
        : `inference p95 ${fmtMs(llm.inference_ms?.p95)} · ${llm.calls_in_window || 0} calls`;
    const ltitle = el("title", {}, lg);
    ltitle.textContent = limiter
      ? limiters
          .map(
            (l) =>
              `${l.provider || "?"}: waiting ${l.waiting} · max_concurrent ${l.max_concurrent} · ` +
              `EWMA ${fmtMs(l.latency_ewma_ms)} · projected next-call ${fmtMs(l.projected_call_ms)}`
          )
          .join("\n")
      : "No limiter gauge received yet";
  }

  // ── SVG pipeline ────────────────────────────────────────────────
  function renderPipeline(metrics) {
    const svg = document.getElementById("pipeline-svg");
    if (!svg) return;
    while (svg.firstChild) svg.firstChild.remove();

    const stages = STAGE_FLOW.map((s) => ({ ...s, stats: stageStats(metrics, s.id) })).filter(
      (s) => !s.optional || s.stats
    );
    const totals = metrics.council_e2e_latency_window || {};
    const e2eP95 = Math.max(
      1,
      ...Object.values(totals).map((t) => t.p95_ms || 0),
      ...stages.map((s) => (s.stats ? s.stats.p95_ms || 0 : 0))
    );
    const throughput = metrics.telemetry_throughput?.per_sec || 0;
    const edgeWidth = Math.max(2, Math.min(14, 2 + throughput * 6));

    const W = 1180;
    const midY = 102;
    const gap = 18;
    const overlapping =
      metrics.timing_model === "overlapping" &&
      OVERLAP_GROUP.every((id) => stages.some((s) => s.id === id && s.stats));

    // One column per sequential step; the overlap group shares a column.
    const columns = [];
    for (const s of stages) {
      const last = columns[columns.length - 1];
      if (overlapping && OVERLAP_GROUP.includes(s.id) && last?.parallel) last.stages.push(s);
      else columns.push({ parallel: overlapping && OVERLAP_GROUP.includes(s.id), stages: [s] });
    }
    const nodeW = Math.floor((W - 80 - gap * (columns.length - 1)) / columns.length);

    // Only the slowest member of an overlap group is on the critical path.
    let bottleneck = null;
    for (const col of columns) {
      let slowest = null;
      for (const s of col.stages) {
        const p95 = s.stats ? s.stats.p95_ms || 0 : 0;
        pushTrend(s.id, p95);
        if (!slowest || p95 > slowest.p95) slowest = { id: s.id, p95 };
      }
      if (slowest && (!bottleneck || slowest.p95 > bottleneck.p95)) bottleneck = slowest;
    }

    // ingress arrow
    el("text", { x: 8, y: midY + 4, class: "pl-ingress" }, svg).textContent = "▶";

    columns.forEach((col, i) => {
      const x = 40 + i * (nodeW + gap);
      const nodeH = col.stages.length > 1 ? 46 : 64;
      const stackGap = 8;
      const stackH = col.stages.length * nodeH + (col.stages.length - 1) * stackGap;
      const top = midY - stackH / 2;

      col.stages.forEach((s, j) => {
        const y = top + j * (nodeH + stackGap);
        const p95 = s.stats ? s.stats.p95_ms || 0 : 0;
        const g = el("g", { class: "pl-node", "data-stage": s.id }, svg);
        const rect = el(
          "rect",
          {
            x, y, rx: 10, width: nodeW, height: nodeH,
            fill: s.stats ? heatColor(p95 / e2eP95) : "hsl(215, 15%, 25%)",
            class: selectedStage === s.id ? "pl-rect pl-selected" : "pl-rect",
          },
          g
        );
        el("title", {}, rect).textContent = s.stats
          ? `${s.label}\ncount ${s.stats.count} · avg ${fmtMs(s.stats.avg_ms)}\n` +
            `p50 ${fmtMs(s.stats.p50_ms)} · p95 ${fmtMs(s.stats.p95_ms)} · max ${fmtMs(s.stats.max_ms)}`
          : `${s.label}\nno samples in window`;
        const compact = nodeH < 60;
        el("text", { x: x + nodeW / 2, y: y + (compact ? 18 : 24), class: "pl-label", "text-anchor": "middle" }, g)
          .textContent = s.label;
        el("text", { x: x + nodeW / 2, y: y + (compact ? 35 : 44), class: "pl-value", "text-anchor": "middle" }, g)
          .textContent = s.stats ? `p95 ${fmtMs(s.stats.p95_ms)}` : "idle";
        if (bottleneck?.id === s.id && bottleneck.p95 > 0) {
          el("text", { x: x + nodeW / 2, y: top - (col.parallel ? 14 : 8), class: "pl-badge", "text-anchor": "middle" }, g)
            .textContent = col.parallel ? `▼ bottleneck (${s.label})` : "▼ bottleneck";
        }
        g.addEventListener("click", () => selectStage(s.id, s.label));
      });

      if (col.stages.length > 1) {
        const wall = stageStats(metrics, "parallel_review_wall");
        el("rect", {
          x: x - 6, y: top - 6, rx: 12, width: nodeW + 12, height: stackH + 12, class: "pl-parallel",
        }, svg);
        el("text", { x: x + nodeW / 2, y: top + stackH + 22, class: "pl-parallel-label", "text-anchor": "middle" }, svg)
          .textContent = wall ? `run in parallel · wall p95 ${fmtMs(wall.p95_ms)}` : "run in parallel";
      }

      // edge to next column
      if (i < columns.length - 1) {
        el("line", {
          x1: x + nodeW, y1: midY, x2: x + nodeW + gap, y2: midY,
          class: "pl-edge", "stroke-width": edgeWidth,
        }, svg);
      }

      // dashed feed to the LLM endpoint node
      if (col.stages.some((s) => s.llm && s.stats)) {
        el("line", {
          x1: x + nodeW / 2, y1: top + stackH + (col.stages.length > 1 ? 28 : 0),
          x2: W / 2, y2: 230,
          class: "pl-llm-edge",
        }, svg);
      }
    });

    renderLlmEndpoint(svg, metrics, W);

    const sub = document.getElementById("pipeline-sub");
    if (sub) {
      sub.textContent = `req/s ${throughput.toFixed(2)} · window e2e p95 ${fmtMs(e2eP95)}${
        bottleneck && bottleneck.p95 > 0 ? ` · bottleneck: ${bottleneck.id}` : ""
      }${overlapping ? " · chairman ∥ members" : ""}`;
    }
    if (selectedStage) renderDetail(metrics);
  }

  // ── stage detail panel ──────────────────────────────────────────
  function selectStage(id, label) {
    selectedStage = selectedStage === id ? null : id;
    const panel = document.getElementById("pipeline-detail");
    if (!panel) return;
    panel.classList.toggle("hidden", selectedStage === null);
    if (selectedStage) {
      panel.dataset.stage = id;
      panel.dataset.label = label;
    }
  }

  function renderDetail(metrics) {
    const panel = document.getElementById("pipeline-detail");
    if (!panel || !selectedStage) return;
    const stats = stageStats(metrics, selectedStage);
    const label = panel.dataset.label || selectedStage;
    panel.innerHTML = "";
    const head = document.createElement("div");
    head.className = "pl-detail-head";
    head.textContent = `${label} — stage detail`;
    const close = document.createElement("button");
    close.className = "pl-detail-close";
    close.textContent = "×";
    close.addEventListener("click", () => selectStage(selectedStage, label));
    head.appendChild(close);
    panel.appendChild(head);

    const body = document.createElement("div");
    body.className = "pl-detail-body";
    if (stats) {
      body.textContent =
        `count ${stats.count} · avg ${fmtMs(stats.avg_ms)} · p50 ${fmtMs(stats.p50_ms)} · ` +
        `p95 ${fmtMs(stats.p95_ms)} · p99 ${fmtMs(stats.p99_ms)} · max ${fmtMs(stats.max_ms)} · last ${fmtMs(stats.last_ms)}`;
    } else {
      body.textContent = "no samples in the current window";
    }
    panel.appendChild(body);

    // per-actor breakdown for LLM stages
    const actorRows = Object.values(metrics.council_actor_latency_window || {});
    if (actorRows.length && ["review_collect", "chairman", "code_review"].includes(selectedStage)) {
      const list = document.createElement("div");
      list.className = "pl-detail-actors";
      for (const a of actorRows) {
        const row = document.createElement("div");
        const name = globalThis.actorDisplayName ? globalThis.actorDisplayName(a) : a.member_id || a.role;
        row.textContent = `${name}: p95 ${fmtMs(a.p95_ms)} · max ${fmtMs(a.max_ms)} (${a.count})`;
        list.appendChild(row);
      }
      panel.appendChild(list);
    }

    const canvas = document.createElement("canvas");
    canvas.width = 360;
    canvas.height = 90;
    panel.appendChild(canvas);
    drawTrend(canvas, trend.get(selectedStage) || []);
  }

  function drawTrend(canvas, values) {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!values.length) return;
    const max = Math.max(...values, 1);
    ctx.strokeStyle = "#7fb4ff";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    values.forEach((v, i) => {
      const x = (i / Math.max(1, values.length - 1)) * (canvas.width - 8) + 4;
      const y = canvas.height - 6 - (v / max) * (canvas.height - 16);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.fillStyle = "#8aa0b8";
    ctx.font = "10px system-ui";
    ctx.fillText(`p95 trend · peak ${fmtMs(max)}`, 6, 12);
  }

  // ── per-API e2e table + rollup cards ────────────────────────────
  function renderApiTable(metrics) {
    const body = document.getElementById("api-e2e-body");
    if (!body) return;
    const rows = Object.values(metrics.council_e2e_latency_window || {}).sort(
      (a, b) => (b.p95_ms || 0) - (a.p95_ms || 0)
    );
    body.innerHTML = "";
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 6;
      td.className = "empty";
      td.textContent = "no council requests in window";
      tr.appendChild(td);
      body.appendChild(tr);
      return;
    }
    for (const r of rows) {
      const tr = document.createElement("tr");
      for (const v of [
        r.tool, r.count, `${(r.per_sec || 0).toFixed(2)}/s`,
        fmtMs(r.p50_ms), fmtMs(r.p95_ms), fmtMs(r.max_ms),
      ]) {
        const td = document.createElement("td");
        td.textContent = String(v);
        tr.appendChild(td);
      }
      body.appendChild(tr);
    }
  }

  function setCard(id, value, sub) {
    const v = document.getElementById(id);
    if (v) v.textContent = value;
    const s = document.getElementById(`${id}-sub`);
    if (s && sub !== undefined) s.textContent = sub;
  }

  function renderRollupCards(metrics) {
    const llm = metrics.llm_latency || {};
    const inf = llm.inference_ms || {};
    const queue = llm.queue_wait_ms || {};
    const limiters = rankedLimiters(metrics);
    const limiter = limiters[0];
    const tput = metrics.telemetry_throughput || {};
    setCard("stat-inference-p50", fmtMs(inf.p50), `p95 ${fmtMs(inf.p95)} · max ${fmtMs(inf.max)}`);
    setCard("stat-queue-wait", fmtMs(queue.p50), `p95 ${fmtMs(queue.p95)} · max ${fmtMs(queue.max)}`);
    setCard(
      "stat-llm-calls",
      `${(llm.calls_per_sec || 0).toFixed(2)}/s`,
      `${llm.calls_in_window || 0} in window · ${llm.retries_in_window || 0} retries`
    );
    setCard(
      "stat-limiter",
      limiter ? `${limiter.waiting}/${limiter.max_concurrent}` : "—",
      limiter
        ? `projected ${fmtMs(limiter.projected_call_ms)}${
            limiters.length > 1 ? ` · busiest of ${limiters.length} (${limiter.provider || "?"})` : ""
          }`
        : "no gauge yet"
    );
    const tokens = (metrics.llm_tokens || {}).window || {};
    setCard(
      "stat-tokens",
      tokens.calls ? fmtTokens(tokens.total) : "—",
      tokens.calls
        ? `${fmtTokens(tokens.input)} in · ${fmtTokens(tokens.output)} out · ${fmtTokens(tokens.per_sec)}/s`
        : llm.calls_in_window
          ? "council is not reporting tokens"
          : "in window"
    );
    setCard(
      "stat-true-throughput",
      `${(tput.per_sec || 0).toFixed(2)}/s`,
      `${tput.requests_in_window || 0} requests (telemetry)`
    );
  }

  // ── per-member token usage ──────────────────────────────────────
  function tokenCell(tr, text, cls) {
    const td = document.createElement("td");
    td.textContent = text;
    if (cls) td.className = cls;
    tr.appendChild(td);
    return td;
  }

  function renderTokenTable(metrics) {
    const body = document.getElementById("token-usage-body");
    if (!body) return;
    const retained = (metrics.llm_tokens || {}).retained || {};
    const windowRows = new Map(
      (((metrics.llm_tokens || {}).window || {}).by_member || []).map((r) => [r.member_id, r])
    );
    const rows = retained.by_member || [];
    body.innerHTML = "";
    if (!rows.length) {
      const tr = document.createElement("tr");
      tr.className = "empty";
      const td = tokenCell(
        tr,
        (metrics.llm_latency || {}).calls_in_window
          ? "LLM calls seen, but the council is not reporting token usage (older council build?)"
          : "waiting for council token telemetry"
      );
      td.colSpan = 9;
      body.appendChild(tr);
      return;
    }
    // Bars share one scale (the largest member) so lengths compare across rows.
    const max = Math.max(...rows.map((r) => r.total), 1);
    for (const r of rows) {
      const tr = document.createElement("tr");
      const name = r.role === "chairman" || r.role === "code_review" ? r.role : r.member_id;
      tokenCell(tr, name).title = `${r.role} · ${r.member_id}`;
      tokenCell(tr, r.provider || "—");
      tokenCell(tr, r.calls.toLocaleString(), "num");
      tokenCell(tr, fmtTokens(r.input), "num").title = r.input.toLocaleString();
      tokenCell(tr, fmtTokens(r.output), "num").title = r.output.toLocaleString();
      tokenCell(tr, fmtTokens(r.total), "num").title = r.total.toLocaleString();
      tokenCell(tr, fmtTokens(r.avg_per_call), "num");
      const win = windowRows.get(r.member_id);
      tokenCell(tr, win ? fmtTokens(win.total) : "0", "num");

      const barTd = document.createElement("td");
      const cell = document.createElement("div");
      cell.className = "token-bar-cell";
      const bar = document.createElement("div");
      bar.className = "token-bar";
      bar.title = `${r.input.toLocaleString()} input · ${r.output.toLocaleString()} output`;
      for (const [cls, value] of [["token-input", r.input], ["token-output", r.output]]) {
        const seg = document.createElement("span");
        seg.className = cls;
        seg.style.width = `${(value / max) * 100}%`;
        bar.appendChild(seg);
      }
      const share = document.createElement("span");
      share.className = "token-share";
      share.textContent = `${(r.share * 100).toFixed(0)}%`;
      cell.append(bar, share);
      barTd.appendChild(cell);
      tr.appendChild(barTd);
      body.appendChild(tr);
    }
  }

  // Panels are independent: one failing must not blank the others.
  function guarded(name, fn, metrics) {
    try {
      fn(metrics);
    } catch (err) {
      console.error(`${name} panel failed:`, err);
    }
  }

  globalThis.nomosPipeline = {
    update(metrics) {
      guarded("pipeline", renderPipeline, metrics);
      guarded("api table", renderApiTable, metrics);
      guarded("rollup cards", renderRollupCards, metrics);
      guarded("token table", renderTokenTable, metrics);
    },
  };
})();
