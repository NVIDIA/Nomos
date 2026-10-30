// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! SystemOne HTTP adapter without chat emulation, redirects or transport retries.
//! The service may explicitly reassess HTTP-only input after an enriched-input size rejection.
use crate::{config::Catalog, strict_json, timeouts::Timeouts};
use async_trait::async_trait;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    time::{Duration, Instant},
};

// A drop guard also records local wait when an outer deadline drops the future.
// It retains counters only: never response text, headers, credentials or URLs.
struct CallMetrics {
    started: Instant,
    request_bytes: usize,
    response_bytes: usize,
    outcome: &'static str,
    response_complete: bool,
    input_tokens: Option<u64>,
    output_tokens: Option<u64>,
    usage_schema: Option<&'static str>,
    output_tokens_basis: Option<&'static str>,
    generated_text_tokens: Option<u64>,
    decision_positions: Option<u64>,
    discarded_sampled_tokens: Option<u64>,
    server_total_ms: Option<f64>,
    server_engine_ms: Option<f64>,
    server_prepare_ms: Option<f64>,
    upstream_calls: Option<u64>,
}
impl Drop for CallMetrics {
    fn drop(&mut self) {
        tracing::info!(
            model_request_bytes = self.request_bytes,
            model_response_bytes = self.response_bytes,
            model_wait_ms = self.started.elapsed().as_secs_f64() * 1000.0,
            model_completion_observed = self.response_complete,
            model_outcome = self.outcome,
            input_tokens = self.input_tokens,
            output_tokens = self.output_tokens,
            usage_schema = self.usage_schema,
            output_tokens_basis = self.output_tokens_basis,
            generated_text_tokens = self.generated_text_tokens,
            decision_positions = self.decision_positions,
            discarded_sampled_tokens = self.discarded_sampled_tokens,
            server_total_ms = self.server_total_ms,
            server_engine_ms = self.server_engine_ms,
            server_prepare_ms = self.server_prepare_ms,
            upstream_calls = self.upstream_calls,
            "model_call_finished"
        );
    }
}

fn transport_failure(error: reqwest::Error, metrics: &mut CallMetrics) -> ProviderError {
    metrics.outcome = if error.is_timeout() {
        "timeout"
    } else {
        "transport_error"
    };
    ProviderError::Unavailable
}

/// Sanitized provider failures. Never retain raw provider bodies or error text.
#[derive(Debug, thiserror::Error)]
pub enum ProviderError {
    /// Connection, HTTP status, deadline or response-stream failure.
    #[error("assessment_unavailable")]
    Unavailable,
    /// Incomplete, ambiguous or invalid model response.
    #[error("invalid_assessment")]
    Invalid,
    /// The upstream model rejected the input size or token context.
    #[error("model_input_over_limit")]
    OverLimit,
}
/// Validated model result, suitable for deterministic threshold evaluation.
pub struct Assessment {
    /// Returned model identity, checked against the configured expected identity.
    pub model: String,
    /// Exact catalog IDs mapped to finite yes-probabilities in [0, 1].
    pub scores: BTreeMap<String, f64>,
}
/// Provider boundary keeps model I/O independent of OpenShell's wire protocol.
#[async_trait]
pub trait DecisionProvider: Send + Sync {
    /// Assess one complete observation using all catalog questions together.
    async fn assess(&self, state: Value, catalog: &Catalog) -> Result<Assessment, ProviderError>;
}

/// Process-configured HTTP adapter with a shared connection pool.
pub struct SystemOneProvider {
    client: reqwest::Client,
    endpoint: reqwest::Url,
    model: String,
    expected_model: String,
    token: Option<String>,
}
impl SystemOneProvider {
    /// Construct an adapter with the default 4-second model HTTP budget.
    /// Plain HTTP requires explicit isolated-lab opt-in.
    ///
    /// `endpoint` is the complete SystemOne URL, including `/v1/systemone`.
    /// `model` is the request alias. `expected_model` is the exact identity
    /// required in responses; deployments may return a versioned name that
    /// differs from the request alias. Neither value pins immutable weights.
    pub fn new(
        endpoint: &str,
        model: String,
        expected_model: String,
        token: Option<String>,
        allow_http: bool,
    ) -> Result<Self, &'static str> {
        Self::with_timeouts(
            endpoint,
            model,
            expected_model,
            token,
            allow_http,
            Timeouts::default(),
        )
    }

    /// Construct an adapter using the model HTTP budget from validated timeouts.
    /// The service separately enforces the longer whole-evaluation deadline.
    pub fn with_timeouts(
        endpoint: &str,
        model: String,
        expected_model: String,
        token: Option<String>,
        allow_http: bool,
        timeouts: Timeouts,
    ) -> Result<Self, &'static str> {
        let endpoint = reqwest::Url::parse(endpoint).map_err(|_| "invalid_model_endpoint")?;
        if endpoint.host_str().is_none()
            || !endpoint.username().is_empty()
            || endpoint.password().is_some()
            || endpoint.query().is_some()
            || endpoint.fragment().is_some()
            || !(endpoint.scheme() == "https" || (allow_http && endpoint.scheme() == "http"))
            || model.is_empty()
            || model.len() > 128
            || model.chars().any(char::is_control)
            || expected_model.is_empty()
            || expected_model.len() > 128
            || expected_model.chars().any(char::is_control)
        {
            return Err("invalid_model_configuration");
        }
        // Disable even protocol-level automatic retries. Disallow redirects so
        // credentials/content cannot reach an unconfigured destination.
        // Cargo selects rustls-tls-native-roots so installed enterprise CAs are
        // trusted without bypassing certificate or hostname verification.
        let client = reqwest::Client::builder()
            .retry(reqwest::retry::never())
            .redirect(reqwest::redirect::Policy::none())
            // Fail an unreachable connection quickly. This cap is inside, not
            // added to, the total request/response timeout below.
            .connect_timeout(Duration::from_millis(500))
            // Includes connecting, sending the request and reading the response
            // body; it is not a server-reported inference duration.
            .timeout(timeouts.model())
            .build()
            .map_err(|_| "invalid_model_configuration")?;
        Ok(Self {
            client,
            endpoint,
            model,
            expected_model,
            token,
        })
    }
}
/// Build the exact native question map shared by transport and context budgeting.
pub(crate) fn questions(catalog: &Catalog) -> Value {
    let questions: BTreeMap<_, _> = catalog
        .rules
        .iter()
        .map(|r| (&r.id, json!({"type":"noul","instructions":r.question})))
        .collect();
    json!(questions)
}

#[async_trait]
impl DecisionProvider for SystemOneProvider {
    async fn assess(&self, state: Value, catalog: &Catalog) -> Result<Assessment, ProviderError> {
        let questions = questions(catalog);
        let bytes =
            serde_json::to_vec(&json!({"model":self.model,"state":state,"questions":questions}))
                .map_err(|_| ProviderError::Invalid)?;
        tracing::info!(model_request_sha256=%format!("{:x}", Sha256::digest(&bytes)),
            model_request_bytes=bytes.len(), "model_call_started");
        let mut metrics = CallMetrics {
            started: Instant::now(),
            request_bytes: bytes.len(),
            response_bytes: 0,
            outcome: "cancelled",
            response_complete: false,
            input_tokens: None,
            output_tokens: None,
            usage_schema: None,
            output_tokens_basis: None,
            generated_text_tokens: None,
            decision_positions: None,
            discarded_sampled_tokens: None,
            server_total_ms: None,
            server_engine_ms: None,
            server_prepare_ms: None,
            upstream_calls: None,
        };
        let mut request = self
            .client
            .post(self.endpoint.clone())
            .header("content-type", "application/json")
            .body(bytes);
        if let Some(token) = &self.token {
            request = request.bearer_auth(token);
        }
        let mut response = request
            .send()
            .await
            .map_err(|error| transport_failure(error, &mut metrics))?;
        let status = response.status();
        // The status alone establishes this limit failure. Do not let an oversized,
        // slow or interrupted error body obscure it or consume the remaining budget.
        if status == reqwest::StatusCode::PAYLOAD_TOO_LARGE {
            metrics.outcome = "input_over_limit";
            return Err(ProviderError::OverLimit);
        }
        // Other provider errors use the same small response bound as answers.
        // Never copy an upstream error body into a status or diagnostic event.
        let mut bytes = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|error| transport_failure(error, &mut metrics))?
        {
            metrics.response_bytes += chunk.len();
            if bytes.len().saturating_add(chunk.len()) > 64 * 1024 {
                // An oversized HTTP error is still an HTTP failure, not an
                // invalid successful assessment. Keep the body bounded either way.
                return Err(if status.is_success() {
                    metrics.outcome = "invalid_assessment";
                    ProviderError::Invalid
                } else {
                    metrics.outcome = "http_error";
                    ProviderError::Unavailable
                });
            }
            bytes.extend_from_slice(&chunk);
        }
        metrics.response_complete = true;
        if !status.is_success() {
            let over_limit = status == reqwest::StatusCode::BAD_REQUEST
                && strict_json::parse(&bytes).is_ok_and(|value| token_limit_error(&value));
            metrics.outcome = if over_limit {
                "input_over_limit"
            } else {
                "http_error"
            };
            return Err(if over_limit {
                ProviderError::OverLimit
            } else {
                ProviderError::Unavailable
            });
        }
        metrics.outcome = "invalid_assessment";
        let value = strict_json::parse(&bytes).map_err(|_| ProviderError::Invalid)?;
        // Only accept explicit, nonnegative integer counters. Record their field
        // names as provenance; absent or unfamiliar usage remains unknown. These
        // endpoint-reported counters are not a local bytes-to-token estimate.
        if let Some(usage) = value.get("usage").and_then(Value::as_object) {
            if usage.get("output_tokens_basis").and_then(Value::as_str)
                == Some("scored_decision_positions")
            {
                metrics.output_tokens_basis = Some("scored_decision_positions");
            }
            metrics.generated_text_tokens =
                usage.get("generated_text_tokens").and_then(Value::as_u64);
            metrics.decision_positions = usage.get("decision_positions").and_then(Value::as_u64);
            metrics.discarded_sampled_tokens = usage
                .get("discarded_sampled_tokens")
                .and_then(Value::as_u64);
            for (input, output, schema) in [
                (
                    "input_tokens",
                    "output_tokens",
                    "usage.input_tokens/output_tokens",
                ),
                (
                    "prompt_tokens",
                    "completion_tokens",
                    "usage.prompt_tokens/completion_tokens",
                ),
            ] {
                if let (Some(i), Some(o)) = (
                    usage.get(input).and_then(Value::as_u64),
                    usage.get(output).and_then(Value::as_u64),
                ) {
                    metrics.input_tokens = Some(i);
                    metrics.output_tokens = Some(o);
                    metrics.usage_schema = Some(schema);
                    break;
                }
            }
        }
        if let Some(timing) = value.get("metrics").and_then(Value::as_object) {
            let milliseconds = |key| {
                timing
                    .get(key)
                    .and_then(Value::as_f64)
                    .filter(|s| s.is_finite() && *s >= 0.0 && *s <= 86400.0)
                    .map(|s| s * 1000.0)
            };
            metrics.server_total_ms = milliseconds("seconds");
            metrics.server_engine_ms = milliseconds("engine_seconds");
            metrics.server_prepare_ms = milliseconds("prepare_seconds");
            metrics.upstream_calls = timing.get("upstream_calls").and_then(Value::as_u64);
        }
        let assessment = validate_response(value, &self.expected_model, catalog)?;
        metrics.outcome = "complete";
        Ok(assessment)
    }
}

/// Recognize explicit token-limit codes without searching provider message text.
/// The public error guide names the code but does not fix its JSON envelope.
fn token_limit_error(value: &Value) -> bool {
    ["/code", "/error/code", "/detail/code", "/error", "/detail"]
        .iter()
        .any(|path| value.pointer(path).and_then(Value::as_str) == Some("max_tokens_exceeded"))
}

/// Validate native noul answers; missing answers must never become zero risk.
pub fn validate_response(
    value: Value,
    expected_model: &str,
    catalog: &Catalog,
) -> Result<Assessment, ProviderError> {
    let model = value
        .get("model")
        .and_then(Value::as_str)
        .filter(|m| *m == expected_model)
        .ok_or(ProviderError::Invalid)?;
    let answers = value
        .get("answers")
        .and_then(Value::as_object)
        .filter(|a| a.len() == catalog.rules.len())
        .ok_or(ProviderError::Invalid)?;
    let mut scores = BTreeMap::new();
    for rule in &catalog.rules {
        let answer = answers
            .get(&rule.id)
            .and_then(Value::as_object)
            .ok_or(ProviderError::Invalid)?;
        if answer.get("type").and_then(Value::as_str) != Some("noul") {
            return Err(ProviderError::Invalid);
        }
        let score = answer
            .get("noul")
            .and_then(Value::as_f64)
            .filter(|n| n.is_finite() && (0.0..=1.0).contains(n))
            .ok_or(ProviderError::Invalid)?;
        scores.insert(rule.id.clone(), score);
    }
    Ok(Assessment {
        model: model.into(),
        scores,
    })
}
