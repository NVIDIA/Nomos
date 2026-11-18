// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// What the dashboard says about a response. Run by tests/test_monitor_dashboard_format.py.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const F = createRequire(import.meta.url)('../monitor/static/format.js');
const now = 1_000_000_000;
const chain = (fields) => ({ chain_id: 'c1', head_sequence: 9, is_valid: true, broken_at: null, ...fields });

test('a fast ledger read is never called valid', () => {
  const fast = {
    validation_mode: 'fast',
    chains: [chain({ fully_validated: false, head_signed: true })],
    summary: { entry_count: 10, validated_entry_count: 1, signed_entry_count: null },
  };
  const verdict = F.ledgerIntegrity(fast);
  assert.equal(verdict.text, 'head entry ok');
  assert.equal(verdict.cls, '');
  assert.match(verdict.detail, /only the newest entry/);
  assert.deepEqual(F.ledgerSigned(fast), { text: 'head entry signed', detail: 'other entries not counted' });
});

test('a full check says valid, or where the chain breaks', () => {
  const summary = { entry_count: 10, validated_entry_count: 10, signed_entry_count: 7 };
  const valid = { validation_mode: 'full', chains: [chain({ fully_validated: true })], summary };
  assert.deepEqual(F.ledgerIntegrity(valid), {
    text: 'valid', cls: 'ok', detail: 'all 10 entries checked: hashes and links',
  });
  const broken = { ...valid, chains: [chain({ is_valid: false, broken_at: 6 })] };
  assert.equal(F.ledgerIntegrity(broken).text, 'broken at #6 · c1');
  assert.equal(F.ledgerIntegrity(broken).cls, 'bad');
  assert.equal(F.ledgerSigned(valid).text, '7/10');
  assert.match(F.ledgerSigned(valid).detail, /not verified/);
});

test('a check that stopped early, and no ledger at all, give no verdict', () => {
  const partial = {
    validation_mode: 'partial',
    chains: [chain({ fully_validated: false })],
    summary: { entry_count: 50, validated_entry_count: 20 },
  };
  assert.equal(F.ledgerIntegrity(partial).text, 'no break found');
  assert.equal(F.ledgerIntegrity(partial).detail, '20 of 50 entries checked');
  assert.equal(F.ledgerIntegrity({ chains: [], summary: {} }).text, '—');
  assert.equal(F.ledgerSigned({ chains: [], summary: {} }).text, '—');
});

test('the newest record ages; the server clock does not keep it fresh', () => {
  const base = { window_seconds: 60 };
  assert.deepEqual(F.recordAge({ ...base, last_record_ms: now - 500 }, now), { text: 'just now', stale: false });
  assert.deepEqual(F.recordAge({ ...base, last_record_ms: now - 30_000 }, now), { text: '30s ago', stale: false });
  assert.deepEqual(
    F.recordAge({ ...base, last_record_ms: now - 75_000 }, now),
    { text: '1m ago · none in window', stale: true },
  );
  assert.deepEqual(F.recordAge(base, now), { text: 'no records received', stale: true });
});

test('an unreachable source is named, with the time of the last good read', () => {
  assert.equal(F.sourceStatus({ source_status: { source: 'inline', ok: true } }, now).bad, false);
  assert.equal(F.sourceStatus({ source_status: { source: 'redis', ok: true } }, now).text, 'source: council telemetry');
  const down = F.sourceStatus(
    { source_status: { source: 'redis', ok: false, error: 'refused', last_success_ms: now - 12_000 } }, now,
  );
  assert.deepEqual(down, { text: 'source: Redis unreachable · last read 12s ago', bad: true, detail: 'refused' });
  assert.match(F.sourceStatus({ source_status: { source: 'redis', ok: false } }, now).text, /never read/);
});

test('errors are split into council errors and transport failures', () => {
  assert.deepEqual(
    F.errorSummary({ error_count: 3, council_error_count: 2, transport_error_count: 1 }),
    { count: 3, sub: '2 council errors · 1 transport failure' },
  );
  assert.equal(
    F.errorSummary({ error_count: 1, council_error_count: 1, transport_error_count: null }).sub,
    '1 council error · transport not observed',
  );
});

test('no samples is a dash, not zero', () => {
  assert.equal(F.windowLatency({ completed_in_window: 0, latency_ms: { p50: 0, p95: 0, p99: 0 } }), null);
  assert.deepEqual(F.windowLatency({ completed_in_window: 2, latency_ms: { p50: 5 } }), { p50: 5 });
  assert.equal(F.formatDuration(null), '—');
  assert.equal(F.formatDuration(574), '574ms');
  assert.equal(F.formatDuration(12_600), '12.6s');
});

test('latency is coloured only against a configured target', () => {
  assert.equal(F.latencyTone(12_000, undefined), '');
  assert.equal(F.latencyTone(12_000, 15_000), '');
  assert.equal(F.latencyTone(16_000, 15_000), 'warn');
  assert.equal(F.latencyTone(31_000, 15_000), 'bad');
});

test('votes the source never carried are not shown as 0/0/0', () => {
  assert.equal(F.voteText(null), '—');
  assert.equal(F.voteText({ facts_recorded: false }), 'not recorded');
  assert.equal(F.voteText({ approval_votes: 2, denial_votes: 1, abstain_votes: 0 }), '2/1/0');
  assert.equal(F.voteText({ approval_votes: 0, denial_votes: 0, abstain_votes: 0 }), 'no vote');
});

test('an empty decision panel says whether nothing happened or nothing was reported', () => {
  assert.equal(F.decisionGapText({ decisions_total: 5, decisions_without_facts_total: 0 }, 'none'), 'none');
  assert.equal(
    F.decisionGapText({ decisions_total: 5, decisions_without_facts_total: 5 }, 'none'),
    'not reported by this council build',
  );
  assert.equal(
    F.decisionGapText({ decisions_total: 5, decisions_without_facts_total: 2 }, 'none'),
    'not reported for 2 of 5 records',
  );
});

test('a stage lists only the actors that run in it', () => {
  const rows = [
    { role: 'chairman' }, { role: 'code_review' },
    { role: 'council_member', member_id: 'm1' }, { role: 'council_member_debate_1', member_id: 'm1' },
  ];
  assert.deepEqual(F.actorsForStage('chairman', rows), [rows[0]]);
  assert.deepEqual(F.actorsForStage('code_review', rows), [rows[1]]);
  assert.deepEqual(F.actorsForStage('review_collect', rows), [rows[2], rows[3]]);
  assert.deepEqual(F.actorsForStage('opa', rows), []);
});

test('an old limiter gauge is labelled with its age', () => {
  assert.equal(F.limiterAge({ asof_ms: now - 2_000 }, now), '');
  assert.equal(F.limiterAge({ asof_ms: now - 26_000 }, now), 'as of 26s ago');
  assert.equal(F.limiterAge(undefined, now), '');
});

test('a model that answered in place of the configured one is shown', () => {
  assert.equal(F.modelText({ configured_model: 'a', returned_model: 'a' }), 'a');
  assert.equal(F.modelText({ configured_model: 'a', returned_model: 'b' }), 'a → b');
  assert.equal(F.modelText({}), '—');
  assert.equal(F.failedAttempts({ attempt_outcomes: { success: 9, timeout: 2, rate_limit: 1 } }), '2 timeout, 1 rate_limit');
  assert.equal(F.failedAttempts({}), '');
});
