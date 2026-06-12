// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// Pipeline visualization + per-API e2e + inference rollup panels.
// Self-contained: app.js calls window.nomosPipeline.update(metrics) each poll.
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
    { id: "submit", label: "Ledger" },
    { id: "mint_writ", label: "Writ", optional: true },
  ];

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
      (metrics.council_stage_latency_window || {})[id] ||
      (metrics.council_stage_latency || {})[id] ||
      null
    );
  }

  // ── SVG pipeline ────────────────────────────────────────────────
  function renderPipeline(metrics) {
    const svg = document.getElementById("pipeline-svg");
    if (!svg) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    const stages = STAGE_FLOW.map((s) => ({ ...s, stats: stageStats(metrics, s.id) })).filter(
      (s) => !s.optional || s.stats
    );
    const totals = metrics.council_e2e_latency_window || {};
    const e2eP95 = Math.max(
      1,
      ...Object.values(totals).map((t) => t.p95_ms || 0),
      ...stages.map((s) => (s.stats ? s.stats.p95_ms || 0 : 0))
    );
    const throughput = (metrics.telemetry_throughput || {}).per_sec || 0;
    const edgeWidth = Math.max(2, Math.min(14, 2 + throughput * 6));

    const W = 1180;
    const nodeY = 70;
    const nodeH = 64;
    const gap = 18;
    const nodeW = Math.floor((W - 80 - gap * (stages.length - 1)) / stages.length);
    let bottleneck = null;
    for (const s of stages) {
      const p95 = s.stats ? s.stats.p95_ms || 0 : 0;
      if (!bottleneck || p95 > bottleneck.p95) bottleneck = { id: s.id, p95 };
      pushTrend(s.id, p95);
    }

    // ingress arrow
    el("text", { x: 8, y: nodeY + nodeH / 2 + 4, class: "pl-ingress" }, svg).textContent = "▶";

    stages.forEach((s, i) => {
      const x = 40 + i * (nodeW + gap);
      const p95 = s.stats ? s.stats.p95_ms || 0 : 0;
      const share = p95 / e2eP95;
      const g = el("g", { class: "pl-node", "data-stage": s.id }, svg);
      const rect = el(
        "rect",
        {
          x, y: nodeY, rx: 10, width: nodeW, height: nodeH,
          fill: s.stats ? heatColor(share) : "hsl(215, 15%, 25%)",
          class: selectedStage === s.id ? "pl-rect pl-selected" : "pl-rect",
        },
        g
      );
      const title = el("title", {}, rect);
      title.textContent = s.stats
        ? `${s.label}\ncount ${s.stats.count} · avg ${fmtMs(s.stats.avg_ms)}\n` +
          `p50 ${fmtMs(s.stats.p50_ms)} · p95 ${fmtMs(s.stats.p95_ms)} · max ${fmtMs(s.stats.max_ms)}`
        : `${s.label}\nno samples in window`;
      el("text", { x: x + nodeW / 2, y: nodeY + 24, class: "pl-label", "text-anchor": "middle" }, g)
        .textContent = s.label;
      el("text", { x: x + nodeW / 2, y: nodeY + 44, class: "pl-value", "text-anchor": "middle" }, g)
        .textContent = s.stats ? `p95 ${fmtMs(s.stats.p95_ms)}` : "idle";
      if (bottleneck && bottleneck.id === s.id && bottleneck.p95 > 0) {
        el("text", { x: x + nodeW / 2, y: nodeY - 8, class: "pl-badge", "text-anchor": "middle" }, g)
          .textContent = "▼ bottleneck";
      }
      g.addEventListener("click", () => selectStage(s.id, s.label));

      // edge to next node
      if (i < stages.length - 1) {
        el("line", {
          x1: x + nodeW, y1: nodeY + nodeH / 2,
          x2: x + nodeW + gap, y2: nodeY + nodeH / 2,
          class: "pl-edge", "stroke-width": edgeWidth,
        }, svg);
      }

      // dashed feed to the LLM endpoint node
      if (s.llm && s.stats) {
        el("line", {
          x1: x + nodeW / 2, y1: nodeY + nodeH,
          x2: W / 2, y2: 230,
          class: "pl-llm-edge",
        }, svg);
      }
    });

    // shared LLM endpoint node with limiter gauge
    const limiter = (metrics.limiters || [])[0];
    const llm = (metrics.llm_latency || {});
    const lg = el("g", { class: "pl-node pl-llm" }, svg);
    const pressure = limiter
      ? Math.min(1, (limiter.projected_call_ms || 0) / 120000)
      : 0;
    el("rect", {
      x: W / 2 - 170, y: 230, rx: 12, width: 340, height: 62,
      fill: heatColor(pressure), class: "pl-rect",
    }, lg);
    el("text", { x: W / 2, y: 252, class: "pl-label", "text-anchor": "middle" }, lg).textContent =
      limiter ? `LLM endpoint (${limiter.provider || "?"})` : "LLM endpoint";
    el("text", { x: W / 2, y: 274, class: "pl-value", "text-anchor": "middle" }, lg).textContent =
      limiter
        ? `queue ${limiter.waiting}/${limiter.max_concurrent} slots · ewma ${fmtMs(limiter.latency_ewma_ms)} · projected ${fmtMs(limiter.projected_call_ms)}`
        : `inference p95 ${fmtMs((llm.inference_ms || {}).p95)} · ${llm.calls_in_window || 0} calls`;
    const ltitle = el("title", {}, lg);
    ltitle.textContent = limiter
      ? `Endpoint limiter\nwaiting ${limiter.waiting} · max_concurrent ${limiter.max_concurrent}\nEWMA ${fmtMs(limiter.latency_ewma_ms)} · projected next-call ${fmtMs(limiter.projected_call_ms)}`
      : "No limiter gauge received yet";

    const sub = document.getElementById("pipeline-sub");
    if (sub) {
      sub.textContent = `req/s ${throughput.toFixed(2)} · window e2e p95 ${fmtMs(e2eP95)}${
        bottleneck && bottleneck.p95 > 0 ? ` · bottleneck: ${bottleneck.id}` : ""
      }`;
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
        row.textContent = `${a.member_id || a.role}: p95 ${fmtMs(a.p95_ms)} · max ${fmtMs(a.max_ms)} (${a.count})`;
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
    const limiter = (metrics.limiters || [])[0];
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
      limiter ? `projected ${fmtMs(limiter.projected_call_ms)}` : "no gauge yet"
    );
    setCard(
      "stat-true-throughput",
      `${(tput.per_sec || 0).toFixed(2)}/s`,
      `${tput.requests_in_window || 0} requests (telemetry)`
    );
  }

  window.nomosPipeline = {
    update(metrics) {
      try {
        renderPipeline(metrics);
        renderApiTable(metrics);
        renderRollupCards(metrics);
      } catch (err) {
        console.error("pipeline panel failed:", err);
      }
    },
  };
})();
