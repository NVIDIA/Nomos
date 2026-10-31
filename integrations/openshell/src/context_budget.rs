// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! Bounded supplementary context without changing the original HTTP observation.
//!
//! Serialized UTF-8 bytes are counted as conservative allocation units, one
//! estimated token per byte. This is not a tokenizer and does not prove that a
//! particular model accepts the input. The byte cap is exact; upstream context
//! admission remains authoritative. Only supplementary context is reduced.
//! Known omissions are reported separately from publication completeness: a
//! fail-open publisher can lose events even when the returned selection fits.

use crate::{config::Catalog, provider::questions};
use serde_json::{Map, Value, json};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};

/// Maximum serialized `conversation_context`, including provenance and omissions.
pub const MAX_CONTEXT_BYTES: usize = 32 * 1024;

/// Operator-selected model windows used only to budget supplementary context.
#[derive(Clone, Copy, Debug)]
pub struct BudgetLimits {
    /// State plus one question window; defaults to Jev's standard 32,000 tokens.
    pub per_question_tokens: usize,
    /// State plus all questions window; defaults to Jev's standard 64,000 tokens.
    pub per_request_tokens: usize,
    /// Reserved estimated units for model framing and estimation uncertainty.
    pub safety_margin_tokens: usize,
}

impl Default for BudgetLimits {
    fn default() -> Self {
        Self {
            per_question_tokens: 32_000,
            per_request_tokens: 64_000,
            safety_margin_tokens: 2_048,
        }
    }
}

impl BudgetLimits {
    /// Reject empty/inverted windows or a margin that consumes either window.
    pub fn validate(self) -> Result<(), &'static str> {
        if self.safety_margin_tokens == 0
            || self.per_question_tokens <= self.safety_margin_tokens
            || self.per_request_tokens < self.per_question_tokens
        {
            return Err("invalid_context_model_budget");
        }
        Ok(())
    }
}

/// Selected content and counters suitable for sanitized diagnostics.
pub struct BudgetResult {
    /// Nested supplemental model field, absent when no safe allocation is available.
    pub context: Option<Value>,
    /// Complete tool-event records included in `context`.
    pub included_records: usize,
    /// Returned tool records omitted locally; upstream omitted count remains unknown.
    pub omitted_returned_records: usize,
    /// Fixed diagnostic category; never includes source values or model content.
    pub reason: &'static str,
}

/// Select fresh whole records under the byte cap and estimated model headroom.
///
/// `base` and `catalog` are never changed. `lookup` is a context-service content
/// response; its retained context remains nested to prevent publisher fields
/// from overriding reader provenance. Records are kept as a newest-first prefix
/// after stale/malformed records are excluded. No field or argument is sliced.
pub fn budget_context(
    base: &Value,
    catalog: &Catalog,
    lookup: &Value,
    limits: BudgetLimits,
) -> BudgetResult {
    budget_context_at(base, catalog, lookup, limits, OffsetDateTime::now_utc())
}

fn unavailable(reason: &'static str, returned: usize) -> BudgetResult {
    BudgetResult {
        context: None,
        included_records: 0,
        omitted_returned_records: returned,
        reason,
    }
}

fn parse_deadline(value: Option<&Value>) -> Option<OffsetDateTime> {
    OffsetDateTime::parse(value?.as_str()?, &Rfc3339).ok()
}

fn identifier(value: Option<&Value>) -> Option<&str> {
    let value = value?.as_str()?;
    (!value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b)))
    .then_some(value)
}

/// Count exact state/question JSON bytes with both outer field wrappers included.
fn estimated_input_units(state: &Value, catalog: &Catalog) -> Option<(usize, usize)> {
    let state_size = serde_json::to_vec(state).ok()?.len();
    let questions = questions(catalog);
    let all_questions = serde_json::to_vec(&questions).ok()?.len();
    let mut largest_question = 0;
    for (id, question) in questions.as_object()? {
        let one = serde_json::to_vec(&json!({id: question})).ok()?.len();
        largest_question = largest_question.max(one);
    }
    let wrapper = b"{\"state\":,\"questions\":}".len();
    let common = state_size.checked_add(wrapper)?;
    Some((
        common.checked_add(largest_question)?,
        common.checked_add(all_questions)?,
    ))
}

fn fits(base: &Value, catalog: &Catalog, context: &Value, limits: BudgetLimits) -> bool {
    let Some(context_bytes) = serde_json::to_vec(context).ok().map(|v| v.len()) else {
        return false;
    };
    if context_bytes > MAX_CONTEXT_BYTES {
        return false;
    }
    let Some(mut state) = base.as_object().cloned() else {
        return false;
    };
    state.insert("conversation_context".into(), context.clone());
    estimated_input_units(&Value::Object(state), catalog).is_some_and(|(one, all)| {
        one <= limits.per_question_tokens - limits.safety_margin_tokens
            && all <= limits.per_request_tokens - limits.safety_margin_tokens
    })
}

fn fresh_record(
    record: &Value,
    expiry: Option<&Map<String, Value>>,
    current_snapshot: &str,
    current_deadline: OffsetDateTime,
    now: OffsetDateTime,
) -> bool {
    let Some(record_id) = identifier(record.get("record_id")) else {
        return false;
    };
    let Some(parent) = identifier(record.get("snapshot_id")) else {
        return false;
    };
    // A previous parent's expiry cannot be inferred from the current snapshot.
    // Same-parent records may use the current expiry for older API responses.
    let deadline = if let Some(value) = expiry.and_then(|values| values.get(record_id)) {
        parse_deadline(Some(value))
    } else if parent == current_snapshot {
        Some(current_deadline)
    } else {
        None
    };
    deadline.is_some_and(|deadline| deadline > now)
}

fn mark_selection(
    context: &mut Value,
    included: &[Value],
    returned: usize,
    source_truncated: bool,
) {
    let omitted = returned - included.len();
    let retained = &context["retained_context"];
    let incomplete = source_truncated
        || omitted > 0
        || retained
            .get("user_request_truncated")
            .and_then(Value::as_bool)
            != Some(false)
        || !retained
            .get("latest_user_request")
            .is_some_and(Value::is_string)
        || included.iter().any(|record| {
            // An absent legacy indicator is unknown, not proof that all tool
            // arguments were captured. Council guidance can also be shortened.
            record.get("arguments_incomplete").and_then(Value::as_bool) != Some(false)
                || record["council_review"]["guidance_truncated"].as_bool() == Some(true)
        });
    context["recent_tool_records"] = Value::Array(included.to_vec());
    context["omitted_returned_records"] = json!(omitted);
    context["context_incomplete"] = json!(incomplete);
}

fn budget_context_at(
    base: &Value,
    catalog: &Catalog,
    lookup: &Value,
    limits: BudgetLimits,
    now: OffsetDateTime,
) -> BudgetResult {
    let Some(records) = lookup.get("tool_records").and_then(Value::as_array) else {
        return unavailable("invalid_context_response", 0);
    };
    let returned = records.len();
    if limits.validate().is_err() {
        return unavailable("invalid_model_budget", returned);
    }
    if base
        .as_object()
        .is_none_or(|value| value.contains_key("conversation_context"))
    {
        return unavailable("invalid_base_state", returned);
    }
    let Some(snapshot_id) = identifier(lookup.get("snapshot_ref")) else {
        return unavailable("invalid_snapshot", returned);
    };
    let snapshot = &lookup["snapshot"];
    if identifier(snapshot.get("snapshot_id")) != Some(snapshot_id)
        || !snapshot.get("context").is_some_and(Value::is_object)
    {
        return unavailable("invalid_snapshot", returned);
    }
    let Some(deadline) = parse_deadline(lookup.get("snapshot_expires_at")) else {
        return unavailable("invalid_snapshot", returned);
    };
    let Some(parent_deadline) = parse_deadline(snapshot.get("not_after")) else {
        return unavailable("invalid_snapshot", returned);
    };
    let deadline = deadline.min(parent_deadline);
    if deadline <= now {
        return unavailable("context_expired", returned);
    }
    let Ok(deadline_text) = deadline.format(&Rfc3339) else {
        return unavailable("invalid_snapshot", returned);
    };
    let source_truncated = lookup
        .get("tool_records_truncated")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let mut selected = Vec::new();
    let mut context = json!({
        "retained_context": snapshot["context"],
        "snapshot_ref": snapshot_id,
        "snapshot_expires_at": deadline_text,
        "association": "name_based_pilot",
        "content_trust": "agent_reported_workload_mutable",
        "capture_coverage": "managed_relay_calls_only",
        "publication_completeness": "not_attested",
        "exact_tool_call_id_for_http_request": null,
        "returned_tool_records": returned,
        "source_tool_records_truncated": source_truncated,
        "omitted_returned_records": returned,
        "context_incomplete": true,
        "recent_tool_records": [],
    });
    mark_selection(&mut context, &selected, returned, source_truncated);
    if !fits(base, catalog, &context, limits) {
        return unavailable("retained_context_over_budget", returned);
    }
    let expiry = lookup
        .get("tool_record_expires_at")
        .and_then(Value::as_object);
    for record in records {
        if !fresh_record(record, expiry, snapshot_id, deadline, now) {
            continue;
        }
        let mut candidate = context.clone();
        selected.push(record.clone());
        mark_selection(&mut candidate, &selected, returned, source_truncated);
        if !fits(base, catalog, &candidate, limits) {
            selected.pop();
            // Retain a newest-first prefix rather than substituting an older
            // pending event after omitting its more recent completion.
            break;
        }
        context = candidate;
    }
    BudgetResult {
        context: Some(context),
        included_records: selected.len(),
        omitted_returned_records: returned - selected.len(),
        reason: "context_included",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{DenyWhen, Rule};
    use time::Duration;

    fn now() -> OffsetDateTime {
        OffsetDateTime::from_unix_timestamp(1_800_000_000).unwrap()
    }

    fn stamp(seconds: i64) -> String {
        (now() + Duration::seconds(seconds))
            .format(&Rfc3339)
            .unwrap()
    }

    fn catalog() -> Catalog {
        Catalog {
            catalog_version: "test".into(),
            rules: vec![Rule {
                id: "disclosure".into(),
                question: "Does this disclose a secret?".into(),
                threshold: 0.7,
                deny_when: DenyWhen::Yes,
            }],
        }
    }

    fn lookup(records: Vec<Value>) -> Value {
        json!({
            "snapshot_ref": "snapshot-current", "snapshot_expires_at": stamp(100),
            "snapshot": {"snapshot_id":"snapshot-current", "not_after": stamp(100),
                "context":{"latest_user_request":"Publish the workshop notice.","user_request_truncated":false}},
            "tool_records": records, "tool_records_truncated": false,
        })
    }

    fn record(id: &str) -> Value {
        json!({"record_id":id,"snapshot_id":"snapshot-current","tool_call_id":"call-a",
            "state":"completed","arguments_incomplete":false,
            "council_review":{"status":"approved","guidance_truncated":false}})
    }

    fn large_limits() -> BudgetLimits {
        BudgetLimits {
            per_question_tokens: 1_000_000,
            per_request_tokens: 2_000_000,
            safety_margin_tokens: 1,
        }
    }

    fn budget(base: &Value, value: &Value, limits: BudgetLimits) -> BudgetResult {
        budget_context_at(base, &catalog(), value, limits, now())
    }

    #[test]
    fn retains_user_first_and_whole_records_without_mutating_http_state() {
        let base = json!({"method":"POST","body":{"notice":"workshop"}});
        let original = base.clone();
        let value = lookup(vec![record("event-new"), record("event-old")]);
        let result = budget(&base, &value, BudgetLimits::default());
        let context = result.context.unwrap();
        assert_eq!(base, original);
        assert_eq!(context["retained_context"], value["snapshot"]["context"]);
        assert_eq!(context["recent_tool_records"], value["tool_records"]);
        assert_eq!(result.included_records, 2);
        assert_eq!(result.omitted_returned_records, 0);
        assert_eq!(context["context_incomplete"], false);
        assert_eq!(context["publication_completeness"], "not_attested");
    }

    #[test]
    fn exact_serialized_context_cap_includes_metadata() {
        let mut value = lookup(vec![]);
        value["snapshot"]["context"]["padding"] = json!("");
        let first = budget(&json!({}), &value, large_limits()).context.unwrap();
        let overhead = serde_json::to_vec(&first).unwrap().len();
        value["snapshot"]["context"]["padding"] = json!("x".repeat(MAX_CONTEXT_BYTES - overhead));
        let exact = budget(&json!({}), &value, large_limits()).context.unwrap();
        assert_eq!(serde_json::to_vec(&exact).unwrap().len(), MAX_CONTEXT_BYTES);
        value["snapshot"]["context"]["padding"] =
            json!("x".repeat(MAX_CONTEXT_BYTES - overhead + 1));
        assert!(budget(&json!({}), &value, large_limits()).context.is_none());
    }

    #[test]
    fn multibyte_and_json_escaping_are_counted_as_actual_utf8_bytes() {
        for text in ["界".repeat(11_000), "\\\"".repeat(9_000)] {
            let mut value = lookup(vec![]);
            value["snapshot"]["context"]["latest_user_request"] = json!(text);
            assert!(budget(&json!({}), &value, large_limits()).context.is_none());
        }
    }

    #[test]
    fn large_http_only_base_is_preserved_but_receives_no_context() {
        let base = json!({"body": {"message":"x".repeat(40_000)}});
        let original = base.clone();
        let result = budget(
            &base,
            &lookup(vec![record("event-1")]),
            BudgetLimits::default(),
        );
        assert!(result.context.is_none());
        assert_eq!(result.omitted_returned_records, 1);
        assert_eq!(base, original);
    }

    #[test]
    fn both_exact_question_envelopes_consume_model_headroom() {
        let base = json!({"body":{"message":"hello"}});
        let value = lookup(vec![]);
        let context = budget(&base, &value, large_limits()).context.unwrap();
        let mut state = base.clone();
        state["conversation_context"] = context;
        let (one, all) = estimated_input_units(&state, &catalog()).unwrap();
        assert_eq!(one, all);
        let exact = BudgetLimits {
            per_question_tokens: one + 10,
            per_request_tokens: all + 10,
            safety_margin_tokens: 10,
        };
        assert!(budget(&base, &value, exact).context.is_some());
        assert!(
            budget(
                &base,
                &value,
                BudgetLimits {
                    per_question_tokens: one + 9,
                    ..exact
                }
            )
            .context
            .is_none()
        );
        let mut many = catalog();
        many.rules.push(Rule {
            id: "second".into(),
            question: "Another question?".into(),
            threshold: 0.7,
            deny_when: DenyWhen::No,
        });
        let result = budget_context_at(&base, &many, &value, exact, now());
        assert!(result.context.is_none());
    }

    #[test]
    fn stops_at_nonfitting_record_instead_of_selecting_older_pending_state() {
        let mut newest = record("event-new");
        newest["arguments"] = json!({"content":"x".repeat(MAX_CONTEXT_BYTES)});
        let mut older = record("event-old");
        older["state"] = json!("execution_pending");
        let result = budget(&json!({}), &lookup(vec![newest, older]), large_limits());
        assert_eq!(result.included_records, 0);
        assert_eq!(result.omitted_returned_records, 2);
        let context = result.context.unwrap();
        assert_eq!(context["context_incomplete"], true);
        assert_eq!(context["recent_tool_records"], json!([]));
    }

    #[test]
    fn expires_latest_snapshot_immediately_without_using_older_context() {
        for expiry in [stamp(0), stamp(-1), "bad-time".into()] {
            let mut value = lookup(vec![record("event-1")]);
            value["snapshot_expires_at"] = json!(expiry);
            assert!(
                budget(&json!({}), &value, BudgetLimits::default())
                    .context
                    .is_none()
            );
        }
        let mut value = lookup(vec![]);
        value["snapshot"]["not_after"] = json!(stamp(-1));
        assert!(
            budget(&json!({}), &value, BudgetLimits::default())
                .context
                .is_none()
        );
    }

    #[test]
    fn checks_each_tool_expiry_and_does_not_invent_old_parent_deadlines() {
        let mut old = record("event-older-parent");
        old["snapshot_id"] = json!("snapshot-previous");
        let current = record("event-current");
        let mut value = lookup(vec![old, current]);
        assert_eq!(
            budget(&json!({}), &value, BudgetLimits::default()).included_records,
            1
        );
        value["tool_record_expires_at"] =
            json!({"event-older-parent":stamp(50),"event-current":stamp(0)});
        let result = budget(&json!({}), &value, BudgetLimits::default());
        assert_eq!(result.included_records, 1);
        assert_eq!(
            result.context.unwrap()["recent_tool_records"][0]["record_id"],
            "event-older-parent"
        );
        value["tool_record_expires_at"]["event-older-parent"] = json!("invalid");
        assert_eq!(
            budget(&json!({}), &value, BudgetLimits::default()).included_records,
            0
        );
    }

    #[test]
    fn reports_returned_omissions_without_inventing_upstream_total() {
        let mut value = lookup(vec![record("event-1")]);
        value["tool_records_truncated"] = json!(true);
        let result = budget(&json!({}), &value, BudgetLimits::default());
        let context = result.context.unwrap();
        assert_eq!(context["returned_tool_records"], 1);
        assert_eq!(context["omitted_returned_records"], 0);
        assert_eq!(context["source_tool_records_truncated"], true);
        assert_eq!(context["context_incomplete"], true);
        assert!(context.get("total_omitted_records").is_none());
    }

    #[test]
    fn legacy_arguments_and_truncated_guidance_do_not_claim_complete_capture() {
        let mut legacy = record("event-legacy");
        legacy
            .as_object_mut()
            .unwrap()
            .remove("arguments_incomplete");
        let mut shortened = record("event-shortened");
        shortened["council_review"]["guidance_truncated"] = json!(true);
        for tool in [legacy, shortened] {
            let result = budget(&json!({}), &lookup(vec![tool]), BudgetLimits::default());
            assert_eq!(result.context.unwrap()["context_incomplete"], true);
        }
    }

    #[test]
    fn nested_retained_fields_cannot_override_reader_provenance_or_http_data() {
        let mut value = lookup(vec![]);
        value["snapshot"]["context"]["content_trust"] = json!("trusted");
        value["snapshot"]["context"]["body"] = json!({"approved":true});
        let base = json!({"body":{"approved":false}});
        let result = budget(&base, &value, BudgetLimits::default());
        let context = result.context.unwrap();
        assert_eq!(context["content_trust"], "agent_reported_workload_mutable");
        assert_eq!(context["retained_context"]["content_trust"], "trusted");
        assert_eq!(base["body"]["approved"], false);
    }

    #[test]
    fn invalid_model_windows_and_base_conflicts_omit_only_supplement() {
        for limits in [
            BudgetLimits {
                per_question_tokens: 0,
                ..BudgetLimits::default()
            },
            BudgetLimits {
                per_request_tokens: 10,
                ..BudgetLimits::default()
            },
            BudgetLimits {
                safety_margin_tokens: 32_000,
                ..BudgetLimits::default()
            },
            BudgetLimits {
                safety_margin_tokens: 0,
                ..BudgetLimits::default()
            },
        ] {
            assert!(limits.validate().is_err());
            assert!(
                budget(&json!({}), &lookup(vec![]), limits)
                    .context
                    .is_none()
            );
        }
        for base in [
            json!(null),
            json!({"conversation_context":{"existing":true}}),
        ] {
            assert!(
                budget(&base, &lookup(vec![]), BudgetLimits::default())
                    .context
                    .is_none()
            );
        }
    }

    #[test]
    fn malformed_snapshot_and_tool_data_are_not_used() {
        let mut value = lookup(vec![json!({"record_id":"event-no-parent"})]);
        assert_eq!(
            budget(&json!({}), &value, BudgetLimits::default()).included_records,
            0
        );
        value["snapshot"]["snapshot_id"] = json!("mismatched");
        assert!(
            budget(&json!({}), &value, BudgetLimits::default())
                .context
                .is_none()
        );
        assert!(
            budget(&json!({}), &json!({}), BudgetLimits::default())
                .context
                .is_none()
        );
    }
}
