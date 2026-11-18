// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/* What the dashboard says about a response, as functions of the response alone.
 *
 * Kept free of the DOM so tests can load this file in Node and check the
 * wording: every function here decides a claim the page makes (valid, stale,
 * not recorded), which is where the page has been wrong before.
 */
(function (root) {
  "use strict";

  function formatAge(ms) {
    if (ms == null) return "—";
    if (ms < 1000) return "now";
    const seconds = Math.round(ms / 1000);
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes}m`;
    return `${Math.round(minutes / 60)}h`;
  }

  function formatDuration(ms) {
    if (ms == null) return "—";
    if (ms < 1) return "0ms";
    if (ms < 1000) return `${Math.round(ms)}ms`;
    return `${(ms / 1000).toFixed(1)}s`;
  }

  // Colour only against a target the operator configured: amber above it, red
  // above twice it. With no target a figure is a figure, not a verdict.
  function latencyTone(ms, target) {
    if (!target || ms == null) return "";
    if (ms > 2 * target) return "bad";
    return ms > target ? "warn" : "";
  }

  // Window percentiles, or null when nothing completed: zero samples is not 0ms.
  function windowLatency(m) {
    return (m.completed_in_window || 0) > 0 ? m.latency_ms || null : null;
  }

  function plural(count, word) {
    return `${count} ${word}${count === 1 ? "" : "s"}`;
  }

  // Failed requests, split into the two ways one can fail.
  function errorSummary(m) {
    const council = plural(m.council_error_count ?? 0, "council error");
    const transport = m.transport_error_count == null
      ? "transport not observed"
      : plural(m.transport_error_count, "transport failure");
    return { count: m.error_count ?? 0, sub: `${council} · ${transport}` };
  }

  // Age of the newest record the monitor holds. The server's own clock says
  // nothing about whether data is still arriving.
  function recordAge(m, nowMs) {
    if (!m.last_record_ms) return { text: "no records received", stale: true };
    const age = Math.max(0, nowMs - m.last_record_ms);
    const stale = age > (m.window_seconds || 60) * 1000;
    const when = age < 2000 ? "just now" : `${formatAge(age)} ago`;
    return { text: stale ? `${when} · none in window` : when, stale };
  }

  function sourceStatus(m, nowMs) {
    const status = m.source_status || {};
    if (status.source !== "redis") return { text: "source: proxy", bad: false };
    if (status.ok) return { text: "source: council telemetry", bad: false };
    const last = status.last_success_ms
      ? `last read ${formatAge(Math.max(0, nowMs - status.last_success_ms))} ago`
      : "never read";
    return { text: `source: Redis unreachable · ${last}`, bad: true, detail: status.error || "" };
  }

  function voteText(decision) {
    if (!decision) return "—";
    if (decision.facts_recorded === false) return "not recorded";
    const votes = [decision.approval_votes || 0, decision.denial_votes || 0, decision.abstain_votes || 0];
    // Refused before any member was asked (auth, policy, permissions).
    return votes.some(Boolean) ? votes.join("/") : "no vote";
  }

  // Why a decision panel is empty: nothing happened, or the source cannot say.
  function decisionGapText(m, fallback) {
    const missing = m.decisions_without_facts_total || 0;
    if (!missing) return fallback;
    const total = m.decisions_total || 0;
    return missing >= total
      ? "not reported by this council build"
      : `not reported for ${missing} of ${total} records`;
  }

  function firstBroken(chains) {
    const broken = chains.find((chain) => !chain.is_valid);
    if (!broken) return "";
    return `#${broken.broken_at ?? broken.head_sequence} · ${broken.chain_id}`;
  }

  // The ledger verdict. "valid" is only ever said of a chain that was walked
  // end to end; a fast read reports the one entry it looked at.
  function ledgerIntegrity(ledger) {
    const summary = ledger.summary || {};
    const chains = ledger.chains || [];
    if (!chains.length) return { text: "—", cls: "", detail: "no chain loaded" };
    const checked = summary.validated_entry_count ?? 0;
    const total = summary.entry_count ?? 0;
    const broken = firstBroken(chains);
    if (ledger.validation_mode === "full") {
      return broken
        ? { text: `broken at ${broken}`, cls: "bad", detail: `${checked} of ${total} entries checked` }
        : { text: "valid", cls: "ok", detail: `all ${checked} entries checked: hashes and links` };
    }
    if (ledger.validation_mode === "partial") {
      return broken
        ? { text: `broken at ${broken}`, cls: "bad", detail: `${checked} of ${total} entries checked` }
        : { text: "no break found", cls: "", detail: `${checked} of ${total} entries checked` };
    }
    return broken
      ? { text: "head entry broken", cls: "bad", detail: broken }
      : { text: "head entry ok", cls: "", detail: "only the newest entry was checked" };
  }

  // Signatures are counted, never verified against the council's key.
  function ledgerSigned(ledger) {
    const summary = ledger.summary || {};
    const chains = ledger.chains || [];
    if (!chains.length) return { text: "—", detail: "" };
    if (summary.signed_entry_count != null) {
      return {
        text: `${summary.signed_entry_count}/${summary.entry_count ?? 0}`,
        detail: "signatures present, not verified against the council key",
      };
    }
    const signedHeads = chains.filter((chain) => chain.head_signed).length;
    const text = chains.length === 1
      ? signedHeads ? "head entry signed" : "head entry unsigned"
      : `${signedHeads}/${chains.length} heads signed`;
    return { text, detail: "other entries not counted" };
  }

  // The actors that run in a pipeline stage; none for stages with no LLM call.
  function actorsForStage(stage, rows) {
    const match = {
      chairman: (role) => role === "chairman",
      code_review: (role) => role === "code_review",
      review_collect: (role) => role.startsWith("council_member"),
    }[stage];
    return match ? rows.filter((row) => match(row.role || "")) : [];
  }

  // A limiter gauge is the last one a council sent; say so once it is old.
  function limiterAge(limiter, nowMs) {
    if (!limiter || !limiter.asof_ms) return "";
    const age = nowMs - limiter.asof_ms;
    return age > 10000 ? `as of ${formatAge(age)} ago` : "";
  }

  // "configured" or "configured → returned" when the endpoint answered as another model.
  function modelText(row) {
    const configured = row.configured_model || "";
    const returned = row.returned_model || "";
    if (!configured && !returned) return "—";
    return returned && configured && returned !== configured
      ? `${configured} → ${returned}`
      : configured || returned;
  }

  // LLM attempts that did not succeed, e.g. "2 timeout, 1 rate_limit".
  function failedAttempts(row) {
    return Object.entries(row.attempt_outcomes || {})
      .filter(([outcome]) => outcome !== "success")
      .sort(([, a], [, b]) => b - a)
      .map(([outcome, count]) => `${count} ${outcome}`)
      .join(", ");
  }

  const api = {
    actorsForStage,
    decisionGapText,
    errorSummary,
    failedAttempts,
    formatAge,
    formatDuration,
    latencyTone,
    ledgerIntegrity,
    ledgerSigned,
    limiterAge,
    modelText,
    recordAge,
    sourceStatus,
    voteText,
    windowLatency,
  };
  root.nomosFormat = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
