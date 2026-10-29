// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
import {buildReview, monitoringState, analysisProgress, filterGroups, describeKind, coverageNotes, streamState, evaluationSelection, previewMatches} from './dashboard-model.mjs';

const $ = id => document.getElementById(id);
const state = {status: null, items: [], review: buildReview([]), error: false,
  errorMessage: '', updatedAt: null, inFlight: false, queueSignature: null, workerState: null,
  runs: [], runsOffset: 0, runsNext: null, runsTotal: 0, findingsTotal: 0};
const selected = new Set();
let evaluationPreview = null;
let evaluationBusy = false;
const number = value => Number(value || 0).toLocaleString();
const plural = (count, word) => `${number(count)} ${word}${count === 1 ? '' : 's'}`;
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function relativeTime(value) {
  const timestamp = typeof value === 'number' ? value : Date.parse(value);
  if (!Number.isFinite(timestamp)) return 'not yet reported';
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 5) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function renderStatus() {
  const monitor = monitoringState(state.status, {connectionError: state.error});
  $('health-label').textContent = monitor.label;
  $('health-label').className = `status-value tone-${monitor.tone}`;
  $('health-dot').className = `status-dot ${monitor.tone}`;
  $('health-explanation').textContent = monitor.explanation;
  const checked = state.status?.worker?.checked_at;
  $('checked').textContent = checked ? `Last completed check ${relativeTime(checked)}` : 'No completed check received yet';
  $('checked').title = checked ? new Date(checked).toLocaleString() : '';
  $('updated').textContent = state.updatedAt ? `Dashboard updated ${relativeTime(state.updatedAt)}` : 'Connecting to the monitor…';
  $('connection-notice').hidden = !state.error;
  $('connection-notice').textContent = state.errorMessage;
  if (!state.status) return;

  const progress = analysisProgress(state.status);
  $('export').disabled = state.status.export_enabled !== true;
  if (!state.status.export_enabled) $('export-status').textContent = 'Audit exports are disabled in this deployment.';
  const coverage = state.status.coverage || {};
  $('verified-value').textContent = number(progress.verified);
  $('verified-detail').textContent = progress.verified ? `Latest verified record #${progress.sequence} · numbering begins at 0` : 'Waiting for a trusted checkpoint';
  $('analysis-label').textContent = progress.label;
  $('analysis-progress').value = progress.percentage;
  $('analysis-detail').textContent = progress.verified ?
    `${plural(coverage.events_in_window, 'record')} in the most recently completed tool analysis window.` : 'Analysis begins after ledger verification.';
  $('agent-count').textContent = number(state.review.agentCount);
  $('review-count').textContent = `${plural(state.review.groups.length, 'behavior group')} from ${plural(state.review.findingCount, 'finding')}. Review their evidence to determine significance.`;
  $('chain-detail').textContent = `Chain: ${state.status.chain_id}. Verified through record #${progress.sequence}; tool analysis through record #${progress.analyzed}.`;
  const diagnostics = JSON.stringify(coverage, null, 2);
  if ($('coverage').textContent !== diagnostics) $('coverage').textContent = diagnostics;
  $('polling-note').textContent = 'Dashboard refreshes every 5 seconds';

  $('review-spans').textContent = number(coverage.review_spans);
  $('tool-spans').textContent = number(coverage.tool_spans);
  $('unsupported').textContent = number(coverage.unsupported_execution_events);
  $('window-summary').textContent = `${plural(coverage.events_in_window, 'record')} examined in a window of up to ${number(coverage.window_limit || 2000)}. Other ledger events can also contribute to Nomos rules.`;
  const cards = (coverage.recurring_cards || []).filter(card => card.eligible);
  $('recurrence-summary').hidden = cards.length === 0;
  $('recurrence-summary').textContent = `${plural(cards.length, 'issue type')} recurred across multiple logical cases and qualified for Compass’s recurring-pattern review.`;
  const limitations = coverage.limitations || [];
  $('coverage-limit').hidden = limitations.length === 0;
  $('coverage-limit').textContent = coverageNotes(limitations);

  const synthetic = state.review.syntheticCount;
  $('data-notice').hidden = synthetic === 0;
  $('data-title').textContent = synthetic === state.review.findingCount ? 'You’re viewing synthetic agent scenarios' : 'This view includes synthetic scenarios';
  $('data-description').textContent = `${plural(synthetic, 'agent-behavior finding')} ${synthetic === 1 ? 'comes' : 'come'} from generated test events. The fixtures exercise the monitor without running real agent commands. Monitor incidents are shown separately.`;

  const incidents = state.review.incidents;
  $('incident-panel').hidden = incidents.length === 0;
  $('incident-dot').className = `status-dot ${monitor.tone === 'danger' ? 'danger' : 'warning'}`;
  $('incident-title').textContent = `${plural(incidents.length, 'ledger verification incident')} retained in history`;
  $('incident-context').textContent = monitor.tone === 'good' ?
    'Current polling is healthy. These retained incidents still document earlier rejected responses.' : `Current status: ${monitor.label.toLowerCase()}. No agent is attributed to these incidents.`;
  if (state.status.worker?.state === 'integrity_error' && state.workerState !== 'integrity_error') $('incident-panel').open = true;
  state.workerState = state.status.worker?.state;
  renderStreams();
  renderEvaluation();
}

function renderStreams() {
  $('stream-status').replaceChildren();
  for (const [stream, title, enabled] of [
    ['tool-issues', 'Tool issues', true], ['ethos-divergence', 'ETHOS divergence', state.status?.streams?.['ethos-divergence']?.enabled === true],
  ]) {
    const value = streamState(stream, state.runs, {enabled,
      intervalSeconds: state.status?.analysis_seconds || 300,
      latestRun: state.status?.streams?.[stream]?.latest_run,
      checkpoint: state.status?.checkpoint?.sequence});
    const card = element('article', 'side-card');
    card.append(element('h3', '', title), element('strong', `tone-${value.tone}`, value.label),
      element('p', 'small-copy', value.explanation),
      element('p', 'small-copy', value.lastSuccess ? `Last successful analysis ${relativeTime(value.lastSuccess)}` : 'No successful analysis in the loaded run history'));
    $('stream-status').append(card);
  }
  $('run-summary').textContent = `${plural(state.runs.length, 'run')} shown of ${number(state.runsTotal)} retained runs`;
  $('run-list').replaceChildren();
  for (const run of state.runs) {
    const item = element('details', 'run-item');
    item.append(element('summary', '', `${run.stream} · ${run.status} · ${relativeTime(run.completed_at || run.started_at)}`));
    item.append(element('p', 'small-copy', `Run ${run.id} · records ${run.sequence_start}–${run.sequence_end} · attempts ${run.attempts || 1}`));
    item.append(element('pre', 'run-coverage', JSON.stringify(run.coverage || {}, null, 2)));
    $('run-list').append(item);
  }
  $('runs-previous').disabled = state.runsOffset === 0;
  $('runs-next').disabled = state.runsNext === null;
}

function evidenceList(references, checkpoint = false) {
  const list = element('ol', 'evidence-list');
  for (const ref of references) {
    const item = element('li');
    const label = ref.sequence >= 0 ? `${checkpoint ? 'Last trusted record' : 'Record'} #${ref.sequence}` : 'No trusted checkpoint was established';
    item.append(element('span', 'record-label', label), element('span', 'record-chain', ref.chain_id || 'Unknown chain'));
    const fingerprint = element('details', 'fingerprint');
    fingerprint.append(element('summary', '', 'Record fingerprint'));
    fingerprint.append(element('code', '', ref.content_hash || 'No content hash available'));
    item.append(fingerprint);
    list.append(item);
  }
  return list;
}

function findingCard(group, opened) {
  const card = element('article', 'finding-card');
  card.dataset.kind = group.kind;
  card.dataset.agent = group.agent;
  const main = element('div', 'finding-main');
  const top = element('div', 'finding-topline');
  top.append(element('span', 'category', group.description.category),
    element('span', `badge ${group.synthetic ? 'synthetic' : 'observed'}`, group.synthetic ? 'Synthetic scenario' : 'Recorded observation'));
  const title = element('h3', '', group.description.title);
  main.append(top, title, element('p', 'meaning', group.description.meaning));
  const meta = element('div', 'finding-meta');
  meta.append(element('span', 'agent-label', group.agent === 'unattributed' ? 'Agent not attributed' : group.agent));
  meta.append(element('span', 'finding-metric', plural(group.findings.length, 'finding')));
  meta.append(element('span', 'finding-metric', plural(group.evidence.length, 'supporting record')));
  const detectors = [...new Set(group.findings.map(f => f.detector?.startsWith('compass:') ? 'Compass' : 'Nomos rule'))];
  meta.append(element('span', '', detectors.join(' · ')));
  main.append(meta);

  const details = element('details', 'finding-details');
  details.dataset.key = group.key;
  details.open = opened.has(group.key);
  details.append(element('summary', '', 'Inspect evidence & suggested next step'));
  const body = element('div', 'finding-detail-body');
  const next = element('div', 'next-step');
  next.append(element('p', 'detail-label', 'SUGGESTED NEXT STEP'), element('p', '', group.description.nextStep));
  body.append(next, element('p', 'boundary', group.description.boundary));
  if (group.kind === 'ethos_divergence') {
    for (const finding of group.findings) {
      const contract = finding.ethos || {};
      body.append(element('p', 'detail-label', `${contract.document_id || 'ETHOS'} · ${contract.version || 'unknown version'} · ${contract.requirement_id || 'requirement unavailable'}`));
      if (finding.explanation) body.append(element('p', '', finding.explanation));
      body.append(element('code', '', `SHA256 ${contract.sha256 || 'unavailable'}`));
    }
  }
  body.append(element('p', 'detail-label', 'SUPPORTING LEDGER RECORDS'));
  body.append(element('p', '', 'These references identify the verified records behind this group. Commands and raw tool output are omitted. A fingerprint identifies the exact record.'));
  body.append(evidenceList(group.evidence));
  const technical = element('details', 'finding-id-list');
  technical.append(element('summary', '', 'Detector versions & finding IDs'));
  for (const detector of new Set(group.findings.map(f => f.detector))) technical.append(element('code', '', detector));
  for (const finding of group.findings) technical.append(element('code', '', finding.id));
  const groupSelect = element('button', 'button', 'Select this group’s findings');
  groupSelect.type = 'button'; groupSelect.disabled = evaluationBusy; groupSelect.dataset.evaluationSelection = 'true';
  groupSelect.addEventListener('click', () => {
    for (const finding of group.findings) selected.add(finding.id);
    evaluationPreview = null; renderQueue(); renderEvaluation();
  });
  body.append(groupSelect);
  for (const finding of group.findings) {
    const label = element('label', 'export-selection');
    const check = element('input'); check.type = 'checkbox'; check.checked = selected.has(finding.id); check.disabled = evaluationBusy;
    check.addEventListener('change', () => {
      if (check.checked) selected.add(finding.id); else selected.delete(finding.id);
      evaluationPreview = null; renderEvaluation();
    });
    label.append(check, document.createTextNode(` Select finding ${finding.id}`));
    body.append(label);
  }
  body.append(technical);
  details.append(body);
  card.append(main, details);
  return card;
}

function setOptions(select, entries, initial) {
  const signature = JSON.stringify(entries);
  if (select.dataset.options === signature) return;
  const selection = select.value;
  select.replaceChildren(new Option(initial, ''));
  for (const [value, label] of entries) select.add(new Option(label, value));
  select.value = entries.some(([value]) => value === selection) ? selection : '';
  select.dataset.options = signature;
}

function renderQueue() {
  const groups = filterGroups(state.review.groups, $('agent').value, $('behavior').value);
  const opened = new Set([...document.querySelectorAll('.finding-details[open]')].map(node => node.dataset.key));
  $('findings').replaceChildren(...groups.map(group => findingCard(group, opened)));
  $('findings').setAttribute('aria-busy', 'false');
  const totalFindings = groups.reduce((total, group) => total + group.findings.length, 0);
    $('showing').textContent = `${plural(groups.length, 'group')} shown · ${plural(totalFindings, 'finding')} from ${number(state.findingsTotal)} retained`;
  $('clear-filters').hidden = !$('agent').value && !$('behavior').value;
  $('empty').hidden = groups.length > 0;
  if (groups.length === 0) {
    if ($('agent').value || $('behavior').value) {
      $('empty-title').textContent = 'No observations match these filters';
      $('empty-description').textContent = 'Clear the filters to see the full review queue.';
    } else if (analysisProgress(state.status).verified === 0) {
      $('empty-title').textContent = 'Waiting for verified ledger records';
      $('empty-description').textContent = 'The observer will examine records after they pass verification. Findings will appear here when a detector flags a behavior.';
    } else {
      $('empty-title').textContent = 'No agent-behavior findings are currently retained';
      $('empty-description').textContent = 'No findings are retained in this view. Review each stream’s run status and coverage; failed or skipped checks do not establish compliant behavior.';
    }
  }
}

function renderIncidents() {
  $('incident-list').replaceChildren();
  for (const incident of state.review.incidents) {
    const description = describeKind(incident.kind);
    const section = element('section', 'incident-item');
    section.append(element('h3', '', 'Ledger response rejected; checkpoint protected'),
      element('p', '', description.meaning), element('p', '', description.boundary),
      element('p', 'detail-label', 'SUGGESTED CHECK'), element('p', '', description.nextStep),
      evidenceList(incident.evidence || [], true));
    $('incident-list').append(section);
  }
}

async function refresh() {
  if (state.inFlight) return;
  state.inFlight = true;
  $('refresh').disabled = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const responses = await Promise.all(['/api/status', '/api/findings?limit=1000', `/api/runs?limit=20&offset=${state.runsOffset}`].map(url => fetch(url, {signal: controller.signal})));
    if (responses.some(response => response.status === 401)) throw new Error('Sign-in required. Reload this page to sign in again.');
    if (responses.some(response => !response.ok)) throw new Error('The dashboard could not refresh. Showing the last available data; retrying automatically.');
    const [status, result, runs] = await Promise.all(responses.map(response => response.json()));
    state.status = status;
    state.items = result.items || result.findings || [];
    state.findingsTotal = result.total ?? state.items.length;
    state.runs = runs.items || [];
    state.runsTotal = runs.total || 0;
    state.runsNext = runs.next_offset ?? null;
    state.review = buildReview(state.items);
    state.error = false;
    state.updatedAt = Date.now();
    const signature = JSON.stringify([state.items, status.checkpoint?.sequence]);
    if (signature !== state.queueSignature) {
      const agents = [...new Set(state.review.groups.map(g => g.agent))].sort();
      setOptions($('agent'), agents.map(agent => [agent, agent === 'unattributed' ? 'Agent not attributed' : agent]), 'All agents');
      const kinds = [...new Set(state.review.groups.map(g => g.kind))];
      setOptions($('behavior'), kinds.map(kind => [kind, describeKind(kind).title]), 'All behaviors');
      renderQueue();
      renderIncidents();
      state.queueSignature = signature;
    }
  } catch (error) {
    state.error = true;
    state.errorMessage = error.name === 'AbortError' || error instanceof TypeError ?
      'The dashboard cannot reach the monitor. Showing the last available data; retrying automatically.' : error.message;
    $('findings').setAttribute('aria-busy', 'false');
    if (!state.status) {
      $('empty').hidden = false;
      $('empty-title').textContent = 'No monitor data available yet';
      $('empty-description').textContent = 'Check the connection or use Refresh now to retry.';
      $('showing').textContent = 'Review queue unavailable';
    }
  } finally {
    clearTimeout(timeout);
    state.inFlight = false;
    $('refresh').disabled = false;
    renderStatus();
  }
}

function renderEvaluation() {
  const enabled = state.status?.evaluation_export_enabled === true;
  $('evaluation-subject').disabled = evaluationBusy;
  $('evaluation-clear').disabled = evaluationBusy;
  for (const control of document.querySelectorAll('.export-selection input, [data-evaluation-selection]')) control.disabled = evaluationBusy;
  $('selection-count').textContent = `${selected.size} findings selected (maximum 20; selection persists across filters)`;
  $('evaluation-preview').disabled = !enabled || evaluationBusy || selected.size < 1 || selected.size > 20;
  $('evaluation-download').disabled = !enabled || evaluationBusy || !previewMatches(evaluationPreview, selected, $('evaluation-subject').value);
  if (!enabled) $('evaluation-context').textContent = 'Private authoring exports are disabled in this deployment.';
  else if (!$('evaluation-context').textContent || $('evaluation-context').textContent.includes('disabled in this deployment')) $('evaluation-context').textContent = 'Select findings and preview their available context. Operator sign-in is required.';
}
async function evaluationRequest(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 90000);
  try {
    const response = await fetch(path, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Assurance-Operator': 'evaluation-export'},
      body: JSON.stringify(body), signal: controller.signal});
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(typeof detail.detail === 'string' ? detail.detail : 'Evaluation export unavailable; refresh the preview or check operator credentials.');
    }
    return response;
  } finally { clearTimeout(timeout); }
}
$('evaluation-clear').addEventListener('click', () => { selected.clear(); evaluationPreview = null; renderQueue(); renderEvaluation(); });
$('evaluation-subject').addEventListener('change', () => { evaluationPreview = null; renderEvaluation(); });
$('evaluation-preview').addEventListener('click', async () => {
  const selection = evaluationSelection(selected, $('evaluation-subject').value);
  evaluationBusy = true; evaluationPreview = null; renderEvaluation();
  $('evaluation-context').textContent = 'Freezing selected evidence and checking available context…';
  try {
    const response = await evaluationRequest('/api/evaluation-export/preview', selection);
    const preview = await response.json();
    if (!previewMatches(preview, selected, $('evaluation-subject').value)) {
      throw new Error('Selection changed during preview. Preview the new selection.');
    }
    evaluationPreview = preview;
    const container = $('evaluation-context'); container.replaceChildren();
    for (const capture of preview.completeness) {
      container.append(element('p', '', `${capture.finding_id}: ${capture.status.replaceAll('_', ' ')} · ${capture.route}`));
      for (const missing of capture.missing_inputs) container.append(element('p', '', missing));
    }
    container.append(element('p', '', `${preview.redaction_count} transformations. ${preview.omissions.length} omissions. Frozen preview expires in 10 minutes.`));
  } catch (error) { $('evaluation-context').textContent = error.message; }
  finally { evaluationBusy = false; renderEvaluation(); }
});
$('evaluation-download').addEventListener('click', async () => {
  const preview = evaluationPreview;
  if (!previewMatches(preview, selected, $('evaluation-subject').value)) return;
  evaluationBusy = true; renderEvaluation();
  try {
    const response = await evaluationRequest('/api/evaluation-export', {capture_id: preview.capture_id});
    const blob = await response.blob();
    if (!previewMatches(preview, selected, $('evaluation-subject').value)) throw new Error('Selection changed; preview again.');
    const url = URL.createObjectURL(blob);
    const link = element('a'); link.href = url; link.download = 'nomos-evaluation-authoring.zip';
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    $('evaluation-context').textContent = 'Authoring bundle downloaded. See its README for validated inputs, missing context, and next steps. Evaluation tasks and agent performance are not yet validated.';
  } catch (error) { evaluationPreview = null; $('evaluation-context').textContent = error.message; }
  finally { evaluationBusy = false; renderEvaluation(); }
});

$('agent').addEventListener('change', renderQueue);
$('behavior').addEventListener('change', renderQueue);
$('clear-filters').addEventListener('click', () => { $('agent').value = ''; $('behavior').value = ''; renderQueue(); });
$('refresh').addEventListener('click', refresh);
$('runs-next').addEventListener('click', () => { if (state.runsNext !== null) {state.runsOffset = state.runsNext; refresh();} });
$('runs-previous').addEventListener('click', () => { state.runsOffset = Math.max(0, state.runsOffset - 20); refresh(); });
$('export').addEventListener('click', async () => {
  $('export').disabled = true;
  $('export-status').textContent = 'Preparing the draft and validating its audit schema…';
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 40000);
  try {
    const response = await fetch('/api/export', {signal: controller.signal});
    if (!response.ok) throw new Error('Export unavailable. The audit validator must succeed before a draft can be downloaded. Please try again.');
    const url = URL.createObjectURL(await response.blob());
    const link = element('a');
    link.href = url;
    link.download = 'nomos-eval-handoff.zip';
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    $('export-status').textContent = 'Draft downloaded. Its audit schema passed validation; independent replay tests and measured coverage remain pending.';
  } catch (error) {
    $('export-status').textContent = error.name === 'AbortError' ? 'The export timed out. Please try again.' : error.message;
  } finally {
    clearTimeout(timeout);
    $('export').disabled = false;
  }
});

refresh();
setInterval(refresh, 5000);
setInterval(renderStatus, 1000);
