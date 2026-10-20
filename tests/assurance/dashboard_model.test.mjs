// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import {buildReview, monitoringState, analysisProgress, filterGroups, describeKind, coverageNotes} from '../../assurance/dashboard-model.mjs';

const now = Date.parse('2026-10-08T20:00:00Z');
test('missing schemas never hide certified receipt correlation limits',()=>{
  const note=coverageNotes(['tool schemas missing in all traces','Receipts have no run/case correlation.']);
  assert.match(note,/schemas are unavailable/);
  assert.match(note,/no run\/case correlation/);
  assert.equal(coverageNotes([]),'');
});
const status = {checkpoint:{sequence:15,content_hash:'hash'},analysis_sequence:15,
  worker:{state:'ok',checked_at:'2026-10-08T19:59:58Z'},poll_seconds:2,
  coverage:{events_in_window:16,tool_spans:9,review_spans:5}};
const ref = sequence => ({chain_id:'demo',sequence,content_hash:`sha256:${sequence}`});
const finding = (id,kind,agent,refs,synthetic=true) => ({id,kind,agent_id:agent,
  evidence:refs,synthetic,detector:'compass:tid-v1:commit'});
const rows = [
  finding('a','explicit_tool_failure','loop-agent',[ref(6)]),
  finding('b','explicit_tool_failure','loop-agent',[ref(7)]),
  finding('c','explicit_tool_failure','loop-agent',[ref(8)]),
  finding('d','repeated_identical_failed_call','loop-agent',[ref(6),ref(7),ref(8)]),
  finding('e','repeated_unchanged_denial','boundary-agent',[ref(3),ref(4),ref(5)]),
  finding('f','ledger_integrity_failure','unattributed',[ref(15)],false),
];

test('groups behavior while keeping monitor incidents out of the agent count',()=>{
  const view=buildReview(rows);
  assert.equal(view.groups.length,3);
  assert.equal(view.incidents.length,1);
  assert.equal(view.agentCount,2);
  assert.equal(view.findingCount,5);
  assert.equal(view.uniqueRecordCount,6);
  assert.equal(view.syntheticCount,5);
  assert.equal(view.groups.find(g=>g.kind==='explicit_tool_failure').findings.length,3);
});

test('does not conflate demo and observed findings or duplicate evidence',()=>{
  const view=buildReview([...rows,finding('g','explicit_tool_failure','loop-agent',[ref(6),ref(6)],false)]);
  const failures=view.groups.filter(g=>g.kind==='explicit_tool_failure');
  assert.equal(failures.length,2);
  assert.equal(failures.find(g=>!g.synthetic).evidence.length,1);
  assert.equal(view.syntheticCount,5);
  assert.equal(view.findingCount,6);
});

test('sequence zero is one record; caught up refers only to the verified checkpoint',()=>{
  assert.equal(analysisProgress(status).verified,16);
  assert.equal(analysisProgress(status).pending,0);
  assert.equal(analysisProgress(status).label,'Verified records analyzed');
  assert.equal(analysisProgress({...status,analysis_sequence:12}).pending,3);
  assert.equal(analysisProgress({checkpoint:{sequence:-1},analysis_sequence:-1}).verified,0);
  assert.equal(analysisProgress({checkpoint:{sequence:-1},analysis_sequence:-1}).label,'Waiting for records');
});

test('healthy current polling does not erase historical incidents',()=>{
  const review=buildReview(rows);
  assert.equal(monitoringState(status,{now}).tone,'good');
  assert.equal(review.incidents[0].kind,'ledger_integrity_failure');
  assert.match(describeKind('ledger_integrity_failure').meaning,/last trusted checkpoint/i);
});

test('stale checks, failed requests and integrity failures cannot appear healthy',()=>{
  assert.equal(monitoringState(status,{now:now+90000}).label,'Check overdue');
  assert.equal(monitoringState(status,{now,connectionError:true}).label,'Connection interrupted');
  assert.equal(monitoringState({...status,worker:{state:'integrity_error',checked_at:status.worker.checked_at}},{now}).label,'Ledger verification blocked');
  assert.equal(monitoringState(null,{now}).label,'Connecting');
});

test('agent and behavior filters compose and can produce an empty view',()=>{
  const groups=buildReview(rows).groups;
  assert.equal(filterGroups(groups,'loop-agent','repeated_identical_failed_call').length,1);
  assert.equal(filterGroups(groups,'boundary-agent','explicit_tool_failure').length,0);
  assert.equal(filterGroups(groups,'','').length,3);
});

test('each supported finding explains significance, review steps and evidence limits',()=>{
  for(const kind of ['explicit_tool_failure','repeated_identical_failed_call','repeated_unchanged_denial','verification_failure','ledger_integrity_failure']){
    const copy=describeKind(kind);
    assert.ok(copy.title && copy.meaning && copy.nextStep && copy.boundary);
  }
  assert.match(describeKind('new_detector_kind').title,/new detector kind/i);
});

test('empty results and unattributed records do not invent an agent count',()=>{
  assert.equal(buildReview([]).agentCount,0);
  assert.equal(buildReview([finding('u','verification_failure','unattributed',[ref(1)],false)]).agentCount,0);
});

test('stream outcomes distinguish missing, failed, skipped, and completed analysis', async()=>{
  const {streamState}=await import('../../assurance/dashboard-model.mjs');
  assert.equal(streamState('ethos',[],{enabled:false},{now}).label,'Disabled');
  assert.equal(streamState('tool_issues',[],{enabled:true},{now}).label,'Not yet analyzed');
  const run=(status,extra={})=>[{id:'r',stream:'ethos',status,completed_at:'2026-10-08T19:59:58Z',...extra}];
  assert.equal(streamState('ethos',run('failed'),{enabled:true},{now}).tone,'warning');
  assert.equal(streamState('ethos',run('skipped'),{enabled:true},{now}).label,'Skipped');
  assert.equal(streamState('ethos',run('insufficient_evidence'),{enabled:true},{now}).label,'Insufficient evidence');
  assert.equal(streamState('ethos',run('completed'),{enabled:true},{now}).label,'Completed');
  assert.equal(streamState('ethos',run('completed'),{enabled:true},{now:now+1000000}).label,'Analysis overdue');
});

test('new skipped runs cannot hide stale successful results or claim healthy empty findings', async()=>{
  const {streamState}=await import('../../assurance/dashboard-model.mjs');
  const runs=[{stream:'ethos',status:'skipped',completed_at:'2026-10-08T19:59:58Z'},
    {stream:'ethos',status:'completed',completed_at:'2026-10-08T18:00:00Z'}];
  const value=streamState('ethos',runs,{enabled:true},{now});
  assert.equal(value.label,'Skipped');
  assert.equal(value.lastSuccess,'2026-10-08T18:00:00Z');
  assert.match(value.explanation,/not establish/i);
});

test('stream summary uses current server state while browsing older history', async()=>{
  const {streamState}=await import('../../assurance/dashboard-model.mjs');
  const latestRun={stream:'tool-issues',status:'complete',completed_at:'2026-10-08T19:59:58Z'};
  const old=[{stream:'tool-issues',status:'failed',completed_at:'2026-10-07T19:59:58Z'}];
  assert.equal(streamState('tool-issues',old,{latestRun},{now}).label,'Completed');
});
