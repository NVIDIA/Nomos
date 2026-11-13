# Workflow state and read-cache retention

The deployed CouncilService owns all workflow/reservation/quorum mutations on
its event loop. Policy preflight, submit, voting, finalization, expiry and abort
are synchronous transitions with no await or storage I/O. A snapshot serializes
and detaches its JSON before entering the bounded outbox. One dedicated writer
orders snapshots per intent and coalesces only updates that have not started.
Requests await the final snapshot attempt before releasing their service lease;
cache failures preserve local status and do not stand in for audit receipts.
The authoritative consolidated audit must still commit before writ mint.

The writer and status-reader lanes are separate from the default executor.
Cancellation drains the actual operation, including repeated cancellation.
`CouncilService.aclose_workflows()` stops maintenance and drains both lanes;
the registry/service lifecycle must invoke it after request leases drain and
before closing the shared PostgreSQL pool.

`max_pending_intents` bounds active reviews plus preflight reservations.
`max_retained_workflows` (default 1000) targets inactive local status history.
The absolute local workflow/reservation bound is their sum. Retained entries
can leave memory early only when their latest exact snapshot is acknowledged
by the shared store. A cache outage or a memory/file-only service preserves
unexpired status and backpressures admission at that combined bound. The
outbox and its receipt indexes use the same bound. Quorum records are evicted
with their inactive workflows. Cleanup processes at most
`workflow_cleanup_batch` (default 128) local entries and durable rows per pass.
Maintenance runs every second after the service's first review.

`workflow_retention_seconds` defaults to `default_token_ttl_seconds` (300).
Approvals expire at the actual writ deadline (30/60/120/300 seconds according
to risk), persisted together with writ ID. The fallback decision deadline is
used before mint. Denied/failed status has an explicit deadline derived from
its decision/completion time. A workflow's retirement horizon never decreases:
shortening the approval lifetime after mint cannot let an older snapshot
outlive the final row and reinsert it after deletion. Status can therefore be
retained longer than the minimum interval. The immutable audit ledger is never
reaped by this mechanism.

## Rollout gate

Apply migrations 0005 and 0006 before using versioned snapshots. Durable
**deletion is disabled by default** (`workflow_durable_cleanup_enabled=false`).
Expiry updates continue; storage grows until deletion is enabled. The database
trigger fences versioned rows against unversioned updates, terminal reversal,
shorter retention and old-reaper deletion. Only the new maintenance operation
sets its transaction-local delete permission, and only with explicit opt-in.

Keep deletion disabled throughout the mixed-version rollout. Drain and retire
all old council processes/background writers, then enable
`workflow_durable_cleanup_enabled=true`. An arbitrarily delayed old writer can
still INSERT after a deleted row because its old payload carries no retirement
horizon; this is why default deletion is gated rather than claiming indefinite
mixed-version safety. All new writers use monotonic horizons and reject
snapshots after their retirement deadline, including after row deletion.

The synchronous SecurityCouncil API retains its legacy behavior. A
CouncilService switched to legacy intermediate audits supports only the exact
built-in in-memory ledger and built-in local signers; file, PostgreSQL and
custom signer configurations fail before request mutation. Use the synchronous
API for such legacy integrations. Normal configured CouncilService always
uses consolidated audits. Custom policy engines with synchronous I/O are not
part of the deployed pure-policy contract.

Foreign status reads remain detached from the local active owner. If local
state changes while a read is in flight, local state wins. Legacy synchronous
status helpers remain available for local state; MCP callbacks use async
shared-store status methods. List reads are bounded by configured admission
capacity (up to 10000 remote rows), with `has_more=true` when that read is truncated. Service reload safety additionally requires the registry lease and
close lifecycle change supplied by the OPA transport component.

## Configuration and validation

These are CouncilSettings YAML keys. For a single council, put them beside
`max_pending_intents`; for a catalog, use `shared` or the individual council
mapping. The packaged profile and Helm profile explicitly default deletion to
false. Helm exposes the complete profile through
`--set-file council.configOverride=PATH`; update that YAML to set
`workflow_durable_cleanup_enabled: true` only after verifying all running images
contain this change and old request/service leases have drained. There is no
automatic environment-variable override for this key. Restart/reload the
services with the updated profile; the database trigger remains installed.

For an all-new isolated retention validation, explicitly enable that key and
run beyond the largest captured retirement horizon (normally 600 seconds with
the fallback300 plus retention300). Confirm EXPIRED updates at actual writ
expiry, bounded cache deletion after retirement, bounded local workflow/quorum/
outbox indexes and unchanged immutable audit count. A default-off run can
validate expiry and memory bounds, but cannot establish bounded durable rows.
