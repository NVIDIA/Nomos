// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! Bounded authenticated access to the separate, name-keyed context service.
//!
//! Callers authenticate the OpenShell supervisor before lookup. This client does
//! not establish sandbox identity or attribute HTTP traffic to an exact tool.
use crate::strict_json;
use clap::ValueEnum;
use reqwest::{Client, Url};
use serde_json::{Value, json};
use std::{collections::BTreeSet, fmt, net::IpAddr, time::Duration};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};

/// Bound the complete lookup response independently of the smaller model-context budget.
pub const MAX_LOOKUP_RESPONSE_BYTES: usize = 512 * 1024;
const MAX_TOOL_RECORDS: usize = 20;

/// Independently configured behavior for optional context retrieval.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, ValueEnum)]
pub enum LookupMode {
    /// Preserve HTTP-only assessment without initializing a reader.
    #[default]
    Disabled,
    /// Retrieve references for audit; never add conversation content to model input.
    Audit,
    /// Add usable context and otherwise retain HTTP-only assessment.
    #[value(name = "best_effort", alias = "best-effort")]
    BestEffort,
    /// Require usable context; lookup failures become assessment failures.
    Required,
}

/// Process-owned context-service settings; token values are never formatted.
pub struct ContextReaderOptions {
    /// Reader behavior selected independently of Relay publication.
    pub mode: LookupMode,
    /// Context server root URL, with no userinfo, query or fragment.
    pub endpoint: String,
    /// Read-only bearer credential, distinct from the publisher credential.
    pub token: String,
    /// Total HTTP lookup deadline, including response reception.
    pub timeout: Duration,
    /// Optional additional private trust anchors; certificate verification stays enabled.
    pub ca_pem: Option<Vec<u8>>,
    /// Explicit isolated-development exception permitting only loopback HTTP.
    pub allow_loopback_http: bool,
}

impl fmt::Debug for ContextReaderOptions {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ContextReaderOptions")
            .field("mode", &self.mode)
            .field("token", &"[REDACTED]")
            .field("timeout", &self.timeout)
            .field("private_ca_configured", &self.ca_pem.is_some())
            .field("allow_loopback_http", &self.allow_loopback_http)
            .finish_non_exhaustive()
    }
}

/// Sanitized lookup failure categories, without server text, URLs or credentials.
#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum LookupError {
    /// The reader is disabled and must not perform I/O.
    #[error("context_lookup_disabled")]
    Disabled,
    /// The authenticated supervisor supplied no usable lookup name or correlation ID.
    #[error("context_lookup_invalid_identity")]
    InvalidIdentity,
    /// No current context exists under this sandbox name.
    #[error("context_unavailable")]
    Missing,
    /// Connection, HTTP authentication, HTTP status or response-stream failure.
    #[error("context_lookup_unavailable")]
    Unavailable,
    /// The bounded lookup deadline elapsed.
    #[error("context_lookup_timeout")]
    Timeout,
    /// The server returned too many bytes.
    #[error("context_lookup_over_limit")]
    OverLimit,
    /// The response contradicts the expected name, shape or content contract.
    #[error("context_lookup_invalid_response")]
    InvalidResponse,
    /// The current snapshot expired before the response could be used.
    #[error("context_lookup_expired")]
    Expired,
}

/// Reusable HTTP client with fixed process configuration and sanitized failures.
pub struct ContextReader {
    mode: LookupMode,
    client: Client,
    endpoint: Url,
    token: String,
    timeout: Duration,
}

impl fmt::Debug for ContextReader {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ContextReader")
            .field("mode", &self.mode)
            .field("timeout", &self.timeout)
            .finish_non_exhaustive()
    }
}

impl ContextReader {
    /// Validate local settings and construct a client without making any request.
    /// Disabled mode must instead be represented by no reader at the caller.
    pub fn new(options: ContextReaderOptions) -> Result<Self, &'static str> {
        if options.mode == LookupMode::Disabled {
            return Err("context_reader_disabled");
        }
        if options.timeout.is_zero() || options.timeout > Duration::from_secs(10) {
            return Err("invalid_context_lookup_timeout");
        }
        if !(32..=4096).contains(&options.token.len())
            || !options.token.bytes().all(|b| (33..=126).contains(&b))
        {
            return Err("invalid_context_reader_token");
        }
        let mut endpoint = Url::parse(&options.endpoint).map_err(|_| "invalid_context_endpoint")?;
        if options.endpoint.trim() != options.endpoint
            || options.endpoint.chars().any(char::is_control)
            || endpoint.host_str().is_none()
            || !endpoint.username().is_empty()
            || endpoint.password().is_some()
            || endpoint.query().is_some()
            || endpoint.fragment().is_some()
            || !matches!(endpoint.path(), "" | "/")
            || !(endpoint.scheme() == "https"
                || (endpoint.scheme() == "http"
                    && options.allow_loopback_http
                    && loopback(&endpoint)))
        {
            return Err("invalid_context_endpoint");
        }
        endpoint.set_path("/v1/context-lookups");
        let mut builder = Client::builder()
            .retry(reqwest::retry::never())
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy()
            .connect_timeout(options.timeout.min(Duration::from_millis(500)))
            .timeout(options.timeout);
        if let Some(pem) = options.ca_pem {
            let certificates =
                reqwest::Certificate::from_pem_bundle(&pem).map_err(|_| "invalid_context_ca")?;
            if certificates.is_empty() {
                return Err("invalid_context_ca");
            }
            for certificate in certificates {
                builder = builder.add_root_certificate(certificate);
            }
        }
        let client = builder.build().map_err(|_| "invalid_context_client")?;
        Ok(Self {
            mode: options.mode,
            client,
            endpoint,
            token: options.token,
            timeout: options.timeout,
        })
    }

    /// Return the configured mode without exposing other process settings.
    pub fn mode(&self) -> LookupMode {
        self.mode
    }

    /// Retrieve one bounded response for an already-authenticated sandbox name.
    /// Source expiry metadata remains in the returned value for budget-time checks.
    pub async fn lookup(
        &self,
        name: &str,
        sandbox_id: &str,
        request_id: &str,
    ) -> Result<Value, LookupError> {
        if self.mode == LookupMode::Disabled {
            return Err(LookupError::Disabled);
        }
        if !identifier(name) || !opaque_id(sandbox_id) || !opaque_id(request_id) {
            return Err(LookupError::InvalidIdentity);
        }
        tokio::time::timeout(self.timeout, self.fetch(name, sandbox_id, request_id))
            .await
            .map_err(|_| LookupError::Timeout)?
    }

    async fn fetch(
        &self,
        name: &str,
        sandbox_id: &str,
        request_id: &str,
    ) -> Result<Value, LookupError> {
        let content = matches!(self.mode, LookupMode::BestEffort | LookupMode::Required);
        let mut response = self
            .client
            .post(self.endpoint.clone())
            .bearer_auth(&self.token)
            .header("accept-encoding", "identity")
            .json(&json!({
                "sandbox_name": name,
                "observed_sandbox_id": sandbox_id,
                "http_request_id": request_id,
                "include_content": content,
            }))
            .send()
            .await
            .map_err(transport_error)?;
        if response.status() == reqwest::StatusCode::NOT_FOUND {
            return Err(LookupError::Missing);
        }
        // Do not read error bodies: they can reflect credentials or arbitrary content.
        if response.status() != reqwest::StatusCode::OK {
            return Err(LookupError::Unavailable);
        }
        if response
            .headers()
            .get("content-encoding")
            .is_some_and(|v| v.as_bytes() != b"identity")
        {
            return Err(LookupError::InvalidResponse);
        }
        if response
            .content_length()
            .is_some_and(|size| size > MAX_LOOKUP_RESPONSE_BYTES as u64)
        {
            return Err(LookupError::OverLimit);
        }
        let mut bytes = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(transport_error)? {
            if bytes.len().saturating_add(chunk.len()) > MAX_LOOKUP_RESPONSE_BYTES {
                return Err(LookupError::OverLimit);
            }
            bytes.extend_from_slice(&chunk);
        }
        let value = strict_json::parse(&bytes).map_err(|_| LookupError::InvalidResponse)?;
        validate_response(&value, name, content, OffsetDateTime::now_utc())?;
        Ok(value)
    }
}

fn loopback(url: &Url) -> bool {
    url.host_str().is_some_and(|host| {
        host == "localhost"
            || host
                .trim_start_matches('[')
                .trim_end_matches(']')
                .parse::<IpAddr>()
                .is_ok_and(|ip| ip.is_loopback())
    })
}

fn transport_error(error: reqwest::Error) -> LookupError {
    if error.is_timeout() {
        LookupError::Timeout
    } else {
        LookupError::Unavailable
    }
}

fn identifier(value: &str) -> bool {
    (1..=128).contains(&value.len())
        && value
            .bytes()
            .next()
            .is_some_and(|b| b.is_ascii_alphanumeric())
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
}

fn opaque_id(value: &str) -> bool {
    (1..=256).contains(&value.chars().count())
}

fn timestamp(value: &Value) -> Result<OffsetDateTime, LookupError> {
    value
        .as_str()
        .filter(|s| s.len() <= 64)
        .and_then(|s| OffsetDateTime::parse(s, &Rfc3339).ok())
        .ok_or(LookupError::InvalidResponse)
}

fn string_id(value: &Value) -> bool {
    value.as_str().is_some_and(identifier)
}

fn valid_review(value: &Value) -> bool {
    if value.is_null() {
        return true;
    }
    let Some(review) = value.as_object() else {
        return false;
    };
    let status = review.get("status").and_then(Value::as_str);
    matches!(status, Some("approved" | "denied" | "error"))
        && ["decision_id", "council_id"].iter().all(|key| {
            review
                .get(*key)
                .is_none_or(|v| v.is_null() || v.as_str().is_some_and(opaque_id))
        })
        && review.get("guidance").is_none_or(|v| {
            v.as_array().is_some_and(|items| {
                items.len() <= 5
                    && items.iter().all(|item| {
                        item.as_str()
                            .is_some_and(|s| (1..=1024).contains(&s.chars().count()))
                    })
            })
        })
        && review
            .get("guidance_truncated")
            .is_none_or(Value::is_boolean)
}

fn validate_response(
    value: &Value,
    name: &str,
    content: bool,
    now: OffsetDateTime,
) -> Result<(), LookupError> {
    let valid = value.is_object()
        && value["sandbox_name"] == name
        && value["association"] == "name_based_pilot"
        && string_id(&value["snapshot_ref"])
        && value["snapshot_revision"].as_u64().is_some_and(|n| n > 0)
        && value["tool_records_truncated"].is_boolean()
        && value.get("exact_tool_call_id_for_http_request") == Some(&Value::Null)
        && value["capture_coverage"] == "managed_relay_calls_only"
        && value["content_trust"] == "agent_reported_workload_mutable"
        && value["source_content_omitted"] == !content;
    if !valid {
        return Err(LookupError::InvalidResponse);
    }
    let expires = timestamp(&value["snapshot_expires_at"])?;
    if expires <= now {
        return Err(LookupError::Expired);
    }
    let refs = value["recent_tool_record_refs"]
        .as_array()
        .ok_or(LookupError::InvalidResponse)?;
    let mut ids = BTreeSet::new();
    if refs.len() > MAX_TOOL_RECORDS
        || refs
            .iter()
            .any(|v| !string_id(v) || !ids.insert(v.as_str().expect("string ID checked")))
    {
        return Err(LookupError::InvalidResponse);
    }
    if let Some(expiries) = value.get("tool_record_expires_at") {
        let map = expiries.as_object().ok_or(LookupError::InvalidResponse)?;
        if map.len() > MAX_TOOL_RECORDS {
            return Err(LookupError::InvalidResponse);
        }
        for (id, expiry) in map {
            if !ids.contains(id.as_str()) {
                return Err(LookupError::InvalidResponse);
            }
            timestamp(expiry)?;
        }
    }
    if !content {
        return if value.get("snapshot").is_some() || value.get("tool_records").is_some() {
            Err(LookupError::InvalidResponse)
        } else {
            Ok(())
        };
    }
    let snapshot = &value["snapshot"];
    if !snapshot.is_object()
        || snapshot["sandbox_name"] != name
        || snapshot["snapshot_id"] != value["snapshot_ref"]
        || !string_id(&snapshot["model_attempt_id"])
        || !snapshot["model_name"]
            .as_str()
            .is_some_and(|s| (1..=256).contains(&s.chars().count()))
        || !snapshot["context"].is_object()
        || timestamp(&snapshot["not_after"])? != expires
    {
        return Err(LookupError::InvalidResponse);
    }
    let records = value["tool_records"]
        .as_array()
        .ok_or(LookupError::InvalidResponse)?;
    if records.len() != refs.len() {
        return Err(LookupError::InvalidResponse);
    }
    for (record, reference) in records.iter().zip(refs) {
        if !record.is_object()
            || record["sandbox_name"] != name
            || record["record_id"] != *reference
            || !string_id(&record["snapshot_id"])
            || !record["tool_name"]
                .as_str()
                .is_some_and(|s| (1..=128).contains(&s.chars().count()))
            || !matches!(
                record["state"].as_str(),
                Some(
                    "requested"
                        | "denied"
                        | "execution_pending"
                        | "completed"
                        | "failed"
                        | "execution_unknown"
                )
            )
            || record
                .get("tool_call_id")
                .is_some_and(|v| !v.is_null() && !v.as_str().is_some_and(opaque_id))
            || record
                .get("arguments")
                .is_some_and(|v| !v.is_null() && !v.is_object())
            || record
                .get("council_review")
                .is_some_and(|v| !valid_review(v))
        {
            return Err(LookupError::InvalidResponse);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        Router,
        body::Body,
        extract::State,
        http::{HeaderMap, Response, StatusCode},
        routing::post,
    };
    use std::sync::{Arc, Mutex};
    use tokio::net::TcpListener;

    const TOKEN: &str = "test-only-context-reader-credential-0001";

    #[derive(Clone)]
    struct Reply {
        status: StatusCode,
        body: Vec<u8>,
        delay: Duration,
        captured: Arc<Mutex<Vec<Value>>>,
        encoding: Option<&'static str>,
    }

    struct TestServer {
        endpoint: String,
        captured: Arc<Mutex<Vec<Value>>>,
        task: tokio::task::JoinHandle<()>,
    }

    impl Drop for TestServer {
        fn drop(&mut self) {
            self.task.abort();
        }
    }

    async fn serve(
        status: StatusCode,
        body: Vec<u8>,
        delay: Duration,
        encoding: Option<&'static str>,
    ) -> TestServer {
        let captured = Arc::new(Mutex::new(Vec::new()));
        let reply = Reply {
            status,
            body,
            delay,
            captured: captured.clone(),
            encoding,
        };
        async fn handle(
            State(reply): State<Reply>,
            headers: HeaderMap,
            bytes: axum::body::Bytes,
        ) -> Response<Body> {
            assert_eq!(headers["authorization"], format!("Bearer {TOKEN}"));
            assert_eq!(headers["accept-encoding"], "identity");
            reply
                .captured
                .lock()
                .unwrap()
                .push(serde_json::from_slice(&bytes).unwrap());
            tokio::time::sleep(reply.delay).await;
            let mut response = Response::builder().status(reply.status);
            if reply.status.is_redirection() {
                response = response.header("location", "/v1/context-lookups");
            }
            if let Some(encoding) = reply.encoding {
                response = response.header("content-encoding", encoding);
            }
            response.body(Body::from(reply.body)).unwrap()
        }
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = format!("http://{}", listener.local_addr().unwrap());
        let app = Router::new()
            .route("/v1/context-lookups", post(handle))
            .with_state(reply);
        let task = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        TestServer {
            endpoint,
            captured,
            task,
        }
    }

    fn options(endpoint: &str) -> ContextReaderOptions {
        ContextReaderOptions {
            mode: LookupMode::BestEffort,
            endpoint: endpoint.to_string(),
            token: TOKEN.to_string(),
            timeout: Duration::from_millis(100),
            ca_pem: None,
            allow_loopback_http: true,
        }
    }

    fn fixture(content: bool) -> Value {
        let deadline = (OffsetDateTime::now_utc() + time::Duration::minutes(4))
            .format(&Rfc3339)
            .unwrap();
        let mut value = json!({
            "sandbox_name": "hermes-a",
            "association": "name_based_pilot",
            "snapshot_ref": "snapshot-1",
            "snapshot_revision": 2,
            "snapshot_expires_at": deadline,
            "recent_tool_record_refs": ["event-1"],
            "tool_record_expires_at": {"event-1": deadline},
            "tool_records_truncated": false,
            "exact_tool_call_id_for_http_request": null,
            "capture_coverage": "managed_relay_calls_only",
            "content_trust": "agent_reported_workload_mutable",
            "source_content_omitted": !content,
        });
        if content {
            value["snapshot"] = json!({
                "sandbox_name": "hermes-a", "snapshot_id": "snapshot-1",
                "model_attempt_id": "attempt-1", "model_name": "agent-model",
                "not_after": deadline,
                "context": {"latest_user_request": "Fictional workshop notice.", "user_request_truncated": false}
            });
            value["tool_records"] = json!([{
                "sandbox_name": "hermes-a", "record_id": "event-1", "snapshot_id": "snapshot-1",
                "tool_call_id": "call-1", "tool_name": "terminal", "state": "completed",
                "arguments": {"command": "printf fictional"},
                "council_review": {"status": "approved", "decision_id": "intent-1"}
            }]);
        }
        value
    }

    #[tokio::test]
    async fn content_lookup_uses_reader_credential_and_exact_name() {
        let expected = fixture(true);
        let server = serve(
            StatusCode::OK,
            serde_json::to_vec(&expected).unwrap(),
            Duration::ZERO,
            None,
        )
        .await;
        let reader = ContextReader::new(options(&server.endpoint)).unwrap();
        assert_eq!(reader.mode(), LookupMode::BestEffort);
        assert_eq!(
            reader
                .lookup("hermes-a", "sandbox-1", "request-1")
                .await
                .unwrap(),
            expected
        );
        let captured = server.captured.lock().unwrap();
        assert_eq!(
            captured.as_slice(),
            &[json!({
                "sandbox_name": "hermes-a", "observed_sandbox_id": "sandbox-1",
                "http_request_id": "request-1", "include_content": true
            })]
        );
    }

    #[tokio::test]
    async fn audit_requests_metadata_only_and_required_requests_content() {
        for mode in [LookupMode::Audit, LookupMode::Required] {
            let content = mode == LookupMode::Required;
            let server = serve(
                StatusCode::OK,
                serde_json::to_vec(&fixture(content)).unwrap(),
                Duration::ZERO,
                None,
            )
            .await;
            let mut config = options(&server.endpoint);
            config.mode = mode;
            let reader = ContextReader::new(config).unwrap();
            reader
                .lookup("hermes-a", "sandbox-1", "request-1")
                .await
                .unwrap();
            assert_eq!(
                server.captured.lock().unwrap()[0]["include_content"],
                content
            );
        }
    }

    #[tokio::test]
    async fn invalid_names_and_observed_ids_are_rejected_without_network() {
        let server = serve(StatusCode::OK, b"{}".to_vec(), Duration::ZERO, None).await;
        let reader = ContextReader::new(options(&server.endpoint)).unwrap();
        for name in ["", "../other", "has space"] {
            assert_eq!(
                reader
                    .lookup(name, "sandbox-1", "request-1")
                    .await
                    .unwrap_err(),
                LookupError::InvalidIdentity
            );
        }
        assert_eq!(
            reader
                .lookup("hermes-a", "", "request-1")
                .await
                .unwrap_err(),
            LookupError::InvalidIdentity
        );
        assert_eq!(
            reader
                .lookup("hermes-a", "sandbox-1", "")
                .await
                .unwrap_err(),
            LookupError::InvalidIdentity
        );
        assert!(server.captured.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn lookup_deadline_bounds_slow_server_and_keeps_error_sanitized() {
        let server = serve(
            StatusCode::OK,
            b"private-response".to_vec(),
            Duration::from_secs(1),
            None,
        )
        .await;
        let mut config = options(&server.endpoint);
        config.timeout = Duration::from_millis(20);
        let reader = ContextReader::new(config).unwrap();
        let error = reader
            .lookup("hermes-a", "sandbox-1", "request-1")
            .await
            .unwrap_err();
        assert_eq!(error, LookupError::Timeout);
        assert!(!format!("{error:?} {error}").contains("private-response"));
    }

    #[tokio::test]
    async fn http_errors_and_redirects_do_not_become_context() {
        for (status, expected) in [
            (StatusCode::NOT_FOUND, LookupError::Missing),
            (StatusCode::UNAUTHORIZED, LookupError::Unavailable),
            (StatusCode::INTERNAL_SERVER_ERROR, LookupError::Unavailable),
            (StatusCode::TEMPORARY_REDIRECT, LookupError::Unavailable),
        ] {
            let server = serve(
                status,
                b"sensitive-reflected-error".to_vec(),
                Duration::ZERO,
                None,
            )
            .await;
            let reader = ContextReader::new(options(&server.endpoint)).unwrap();
            let error = reader
                .lookup("hermes-a", "sandbox-1", "request-1")
                .await
                .unwrap_err();
            assert_eq!(error, expected);
            assert!(!format!("{error:?} {error}").contains("sensitive"));
            assert_eq!(server.captured.lock().unwrap().len(), 1);
        }
    }

    #[tokio::test]
    async fn response_bytes_compression_and_ambiguous_json_are_rejected() {
        for (body, encoding, expected) in [
            (
                vec![b' '; MAX_LOOKUP_RESPONSE_BYTES + 1],
                None,
                LookupError::OverLimit,
            ),
            (
                br#"{"sandbox_name":"a","sandbox_name":"b"}"#.to_vec(),
                None,
                LookupError::InvalidResponse,
            ),
            (
                br#"{"number":NaN}"#.to_vec(),
                None,
                LookupError::InvalidResponse,
            ),
            (b"{}".to_vec(), Some("gzip"), LookupError::InvalidResponse),
        ] {
            let server = serve(StatusCode::OK, body, Duration::ZERO, encoding).await;
            let reader = ContextReader::new(options(&server.endpoint)).unwrap();
            assert_eq!(
                reader
                    .lookup("hermes-a", "sandbox-1", "request-1")
                    .await
                    .unwrap_err(),
                expected
            );
        }
    }

    #[tokio::test]
    async fn chunked_response_is_bounded_without_a_content_length_header() {
        async fn handle() -> Response<Body> {
            let chunks = tokio_stream::iter([
                Ok::<_, std::io::Error>(vec![b'x'; MAX_LOOKUP_RESPONSE_BYTES]),
                Ok(vec![b'y'; 1]),
            ]);
            Response::new(Body::from_stream(chunks))
        }
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = format!("http://{}", listener.local_addr().unwrap());
        let app = Router::new().route("/v1/context-lookups", post(handle));
        let task = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let reader = ContextReader::new(options(&endpoint)).unwrap();
        let result = reader.lookup("hermes-a", "sandbox-1", "request-1").await;
        task.abort();
        assert_eq!(result.unwrap_err(), LookupError::OverLimit);
    }

    #[test]
    fn response_requires_matching_content_identity_and_honest_metadata() {
        let baseline = fixture(true);
        let now = OffsetDateTime::now_utc();
        assert!(validate_response(&baseline, "hermes-a", true, now).is_ok());
        for change in [
            json!({"sandbox_name": "another-name"}),
            json!({"source_content_omitted": true}),
            json!({"association": "exact_causal_binding"}),
            json!({"exact_tool_call_id_for_http_request": "claimed-tool"}),
            json!({"snapshot_revision": 0}),
            json!({"tool_records_truncated": "false"}),
            json!({"recent_tool_record_refs": ["event-1", "event-1"]}),
        ] {
            let mut value = baseline.clone();
            value
                .as_object_mut()
                .unwrap()
                .extend(change.as_object().unwrap().clone());
            assert_eq!(
                validate_response(&value, "hermes-a", true, now),
                Err(LookupError::InvalidResponse)
            );
        }
        for (path, replacement) in [
            ("/snapshot/sandbox_name", json!("another-name")),
            ("/snapshot/snapshot_id", json!("snapshot-other")),
            ("/snapshot/context", json!([])),
            ("/tool_records/0/record_id", json!("event-other")),
            ("/tool_records/0/sandbox_name", json!("another-name")),
            ("/tool_records/0/state", json!("approved")),
            ("/tool_records/0/arguments", json!("string")),
            ("/tool_records/0/council_review/status", json!("allow")),
        ] {
            let mut value = baseline.clone();
            *value.pointer_mut(path).unwrap() = replacement;
            assert_eq!(
                validate_response(&value, "hermes-a", true, now),
                Err(LookupError::InvalidResponse)
            );
        }
    }

    #[test]
    fn old_tool_records_keep_their_own_expiry_and_legacy_missing_map_is_preserved() {
        let mut value = fixture(true);
        value["tool_records"][0]["snapshot_id"] = json!("older-snapshot");
        value["tool_record_expires_at"]["event-1"] = json!("2020-01-01T00:00:00Z");
        // Budgeting drops events that expired in flight; it must not replace this date.
        assert!(validate_response(&value, "hermes-a", true, OffsetDateTime::now_utc()).is_ok());
        value
            .as_object_mut()
            .unwrap()
            .remove("tool_record_expires_at");
        assert!(validate_response(&value, "hermes-a", true, OffsetDateTime::now_utc()).is_ok());
    }

    #[test]
    fn latest_snapshot_expiry_is_checked_before_use() {
        let mut value = fixture(true);
        value["snapshot_expires_at"] = json!("2020-01-01T00:00:00Z");
        value["snapshot"]["not_after"] = value["snapshot_expires_at"].clone();
        assert_eq!(
            validate_response(&value, "hermes-a", true, OffsetDateTime::now_utc()),
            Err(LookupError::Expired)
        );
    }

    #[test]
    fn constructor_rejects_unsafe_endpoints_tokens_timeouts_and_ca_without_io() {
        for endpoint in [
            "http://127.0.0.1/path",
            "https://user@example.com",
            "https://example.com?query",
            "https://example.com#fragment",
            "ftp://example.com",
            "http://example.com",
            " https://example.com",
        ] {
            assert!(ContextReader::new(options(endpoint)).is_err(), "{endpoint}");
        }
        let mut config = options("http://127.0.0.1");
        config.allow_loopback_http = false;
        assert!(ContextReader::new(config).is_err());
        for token in [
            "",
            "too-short",
            "a token with spaces and more than thirty-two bytes",
            "has\nnewline-and-more-than-thirty-two-bytes",
        ] {
            let mut config = options("https://example.com");
            config.token = token.to_string();
            if token.contains(' ') || token.contains('\n') || token.len() < 32 {
                assert!(ContextReader::new(config).is_err());
            }
        }
        for timeout in [Duration::ZERO, Duration::from_secs(11)] {
            let mut config = options("https://example.com");
            config.timeout = timeout;
            assert!(ContextReader::new(config).is_err());
        }
        let mut config = options("https://example.com");
        config.ca_pem = Some(b"not a certificate".to_vec());
        assert!(ContextReader::new(config).is_err());
        let mut config = options("https://example.com");
        config.mode = LookupMode::Disabled;
        assert!(ContextReader::new(config).is_err());
    }

    #[test]
    fn local_options_and_client_debug_never_include_token_or_endpoint() {
        let config = options("https://private-context.example");
        let debug = format!("{config:?}");
        assert!(!debug.contains(TOKEN) && !debug.contains("private-context"));
        let reader = ContextReader::new(config).unwrap();
        let debug = format!("{reader:?}");
        assert!(!debug.contains(TOKEN) && !debug.contains("private-context"));
    }
}
