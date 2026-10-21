// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
// Presentation semantics only. Findings and trust decisions remain server-owned.
export function coverageNotes(limitations) {
  return limitations.map(text => text.includes('tool schemas missing') ?
    'Tool schemas are unavailable in these traces, limiting checks of arguments against a tool’s contract.' : text).join(' · ');
}

const descriptions = {
  ethos_divergence: {
    title: 'Possible divergence from ETHOS', category: 'Agent purpose', order: 0,
    meaning: 'Compass found behavior that may conflict with a supported requirement in the configured agent contract.',
    nextStep: 'Review the cited requirement, document version, and ledger evidence. Establish whether the observed behavior conflicts with the requirement within its scope.',
    boundary: 'This is an inference-based candidate for human review. Authorization or a successful tool call does not establish compliance with the agent’s purpose.',
  },
  repeated_unchanged_denial: {
    title: 'Repeated requests after denial', category: 'Policy boundary', order: 0,
    meaning: 'The same agent requested an unchanged command and target at least three times within 15 minutes, and each request was denied.',
    nextStep: 'Review the denial guidance and whether the agent should have corrected the request, stopped, or asked for help.',
    boundary: 'This is a pattern worth reviewing. The records do not establish why the agent repeated the request.',
  },
  repeated_identical_failed_call: {
    title: 'Repeated retries with the same failure', category: 'Recovery behavior', order: 1,
    meaning: 'Compass found patterns of at least three identical tool calls that reported failure. Each finding represents one detected retry pattern.',
    nextStep: 'Check whether the agent changed its approach, honored a retry limit, or escalated the failure.',
    boundary: 'These patterns can include the same records shown under individual tool failures. Counts overlap.',
  },
  verification_failure: {
    title: 'Verification reported a failure', category: 'Verification', order: 2,
    meaning: 'A signed ledger record reports a verification failure. The monitor surfaces the recorded result for review.',
    nextStep: 'Inspect the original verification record to determine the failed check and whether the associated action was stopped.',
    boundary: 'The finding alone does not establish the cause, impact, or intent of the agent.',
  },
  explicit_tool_failure: {
    title: 'Tool calls reported failure', category: 'Tool outcome', order: 3,
    meaning: 'Recorded tool results explicitly indicate failure. An individual tool failure may come from a service, an input, or the agent’s behavior.',
    nextStep: 'Review the original tool result and the agent’s response. Related retry patterns can help identify poor recovery.',
    boundary: 'A tool failure is an observation. Its cause and effect still need to be established.',
  },
  ledger_integrity_failure: {
    title: 'Ledger verification incident', category: 'Monitor incident', order: -1,
    meaning: 'The observer rejected a ledger response. The reference identifies the last trusted checkpoint; rejected data was not accepted as evidence.',
    nextStep: 'Check source availability, signing keys, and the ledger response. Resume ingestion only when verification succeeds.',
    boundary: 'This is a source-integrity observation with no agent attribution. It remains in history after successful polling resumes.',
  },
};

export function describeKind(kind) {
  return descriptions[kind] || {
    title: String(kind || 'Unclassified observation').replaceAll('_', ' '),
    category: 'Observation', order: 10,
    meaning: 'The detector recorded an observation that needs a reviewer’s interpretation.',
    nextStep: 'Inspect its evidence references and detector details before drawing a conclusion.',
    boundary: 'No cause or impact is established by this dashboard.',
  };
}

function uniqueEvidence(findings) {
  const records = new Map();
  for (const item of findings) {
    for (const ref of item.evidence || []) {
      records.set(JSON.stringify([ref.chain_id, ref.sequence, ref.content_hash]), ref);
    }
  }
  return [...records.values()].sort((a, b) => a.sequence - b.sequence);
}

export function buildReview(items) {
  const incidents = items.filter(f => f.kind === 'ledger_integrity_failure');
  const behavior = items.filter(f => f.kind !== 'ledger_integrity_failure');
  const groups = new Map();
  for (const item of behavior) {
    const agent = item.agent_id || 'unattributed';
    const synthetic = item.synthetic === true;
    const key = JSON.stringify([agent, item.kind, synthetic]);
    if (!groups.has(key)) {
      groups.set(key, {key, agent, kind: item.kind, synthetic, findings: []});
    }
    groups.get(key).findings.push(item);
  }
  const grouped = [...groups.values()].map(group => ({
    ...group, evidence: uniqueEvidence(group.findings), description: describeKind(group.kind),
  })).sort((a, b) => a.description.order - b.description.order || a.agent.localeCompare(b.agent));
  const agents = [...new Set(behavior.map(f => f.agent_id).filter(a => a && a !== 'unattributed'))].sort();
  return {groups: grouped, incidents, agents, agentCount: agents.length,
    findingCount: behavior.length, uniqueRecordCount: uniqueEvidence(behavior).length,
    syntheticCount: behavior.filter(f => f.synthetic === true).length};
}

export function filterGroups(groups, agent = '', kind = '') {
  return groups.filter(g => (!agent || g.agent === agent) && (!kind || g.kind === kind));
}

export function analysisProgress(status) {
  const sequence = Number.isInteger(status?.checkpoint?.sequence) ? status.checkpoint.sequence : -1;
  const analyzed = Number.isInteger(status?.analysis_sequence) ? status.analysis_sequence : -1;
  const verified = Math.max(0, sequence + 1);
  const pending = Math.max(0, sequence - analyzed);
  return {verified, pending, sequence, analyzed,
    label: verified === 0 ? 'Waiting for records' : pending ? `${pending} records awaiting analysis` : 'Verified records analyzed',
    percentage: verified ? Math.max(0, Math.min(100, (analyzed + 1) / verified * 100)) : 0};
}

export function monitoringState(status, {now = Date.now(), connectionError = false} = {}) {
  if (connectionError) return {label: 'Connection interrupted', tone: 'warning',
    explanation: 'The dashboard cannot refresh its data. Any displayed findings are from the last successful response.'};
  if (!status) return {label: 'Connecting', tone: 'neutral', explanation: 'Requesting the latest monitor status.'};
  const worker = status.worker || {};
  if (worker.state === 'integrity_error') return {label: 'Ledger verification blocked', tone: 'danger',
    explanation: 'The latest ledger response failed verification. The observer is holding its last trusted checkpoint.'};
  if (worker.state === 'worker_error') return {label: 'Monitor needs attention', tone: 'warning',
    explanation: 'The latest processing cycle failed. The observer will retry; findings remain available for review.'};
  if (worker.state !== 'ok') return {label: 'Waiting for first check', tone: 'neutral',
    explanation: 'The service is available and has not yet reported a successful processing cycle.'};
  const checked = Date.parse(worker.checked_at);
  if (!Number.isFinite(checked) || now - checked > Math.max(30000, (status.poll_seconds || 2) * 3000)) {
    return {label: 'Check overdue', tone: 'warning',
      explanation: 'No recent completed check has been reported. The displayed results may be behind the source.'};
  }
  return {label: 'Polling normally', tone: 'good',
    explanation: 'The latest processing cycle completed successfully. Retained findings still need human review.'};
}

// A healthy ingestion worker does not imply that either analysis stream ran.
export function streamState(stream, runs, {enabled = true, intervalSeconds = 300, latestRun = null, checkpoint = null} = {}, {now = Date.now()} = {}) {
  const history = runs.filter(run => run.stream === stream);
  const latest = latestRun || history[0];
  const succeeded = run => ['complete', 'completed'].includes(run?.status);
  const lastSuccess = succeeded(latest) ? latest.completed_at :
    history.find(succeeded)?.completed_at || null;
  const details = {latest, lastSuccess};
  if (!enabled) return {...details, label: 'Disabled', tone: 'neutral',
    explanation: 'This stream requires explicit opt-in configuration.'};
  if (!latest) return {...details, label: 'Not yet analyzed', tone: 'neutral',
    explanation: 'No completed analysis is available. An empty queue does not establish compliance.'};
  const statuses = {
    pending: ['Queued', 'neutral'], running: ['Running', 'neutral'],
    failed: ['Failed', 'warning'], skipped: ['Skipped', 'neutral'],
    insufficient_evidence: ['Insufficient evidence', 'warning'],
    complete: ['Completed', 'good'], completed: ['Completed', 'good'],
    not_configured: ['Not configured', 'neutral'],
  };
  const [label, tone] = statuses[latest.status] || [String(latest.status), 'neutral'];
  if (succeeded(latest) && Number.isInteger(checkpoint) && Number.isInteger(latest.sequence_end) && latest.sequence_end >= checkpoint) {
    return {...details, label: 'Up to date', tone: 'good',
      explanation: 'Analysis covers the current verified checkpoint. Consult coverage before interpreting the findings.'};
  }
  const timestamp = Date.parse(latest.completed_at || latest.started_at);
  if (succeeded(latest) && (!Number.isFinite(timestamp) || now - timestamp > intervalSeconds * 2000)) {
    return {...details, label: 'Analysis overdue', tone: 'warning',
      explanation: 'The latest successful analysis is older than two configured analysis intervals.'};
  }
  return {...details, label, tone, explanation: succeeded(latest) ?
    'Analysis completed for its recorded evidence scope. Consult coverage before interpreting the findings.' :
    'This outcome does not establish compliant behavior. Review run coverage and configuration.'};
}
