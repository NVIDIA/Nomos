// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! Boundary tests exercise actual gRPC and HTTP transports, not model quality.
use async_trait::async_trait;
use nomos_request_review::{
    config::{Catalog, DenyWhen},
    openshell::middleware::v1::*,
    provider::{Assessment, DecisionProvider, ProviderError, SystemOneProvider},
    service::{ReviewService, metadata},
    strict_json,
    timeouts::Timeouts,
};
use serde_json::json;
use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};
use supervisor_middleware_server::SupervisorMiddleware;
use tonic::{Code, Request};

#[derive(Clone, Default)]
struct CapturedLogs(Arc<Mutex<Vec<u8>>>);

impl std::io::Write for CapturedLogs {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl CapturedLogs {
    fn failures(&self) -> Vec<serde_json::Value> {
        String::from_utf8(self.0.lock().unwrap().clone())
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str::<serde_json::Value>(line).unwrap())
            .filter(|event| event["fields"]["message"] == "review_failed")
            .map(|event| event["fields"].clone())
            .collect()
    }
}

fn capture_logs() -> (CapturedLogs, tracing::subscriber::DefaultGuard) {
    let logs = CapturedLogs::default();
    let writer = logs.clone();
    let subscriber = tracing_subscriber::fmt()
        .json()
        .with_ansi(false)
        .with_writer(move || writer.clone())
        .finish();
    (logs, tracing::subscriber::set_default(subscriber))
}

fn config() -> prost_types::Struct {
    prost_types::Struct {fields:[
        ("catalog_version".into(),prost_types::Value {kind:Some(prost_types::value::Kind::StringValue("test-v1".into()))}),
        ("rules".into(),prost_types::Value {kind:Some(prost_types::value::Kind::ListValue(prost_types::ListValue {values:vec![prost_types::Value {kind:Some(prost_types::value::Kind::StructValue(prost_types::Struct {fields:[
            ("id".into(),prost_types::Value {kind:Some(prost_types::value::Kind::StringValue("disclosure".into()))}),
            ("question".into(),prost_types::Value {kind:Some(prost_types::value::Kind::StringValue("Does this disclose confidential information?".into()))}),
            ("threshold".into(),prost_types::Value {kind:Some(prost_types::value::Kind::NumberValue(0.7))}),
        ].into()}))}]}))}),
    ].into()}
}
fn request() -> HttpRequestEvaluation {
    HttpRequestEvaluation {
        phase: SupervisorMiddlewarePhase::PreCredentials as i32,
        context: Some(RequestContext {
            request_id: "req-1".into(),
            sandbox_id: "sandbox-1".into(),
            ..Default::default()
        }),
        config: Some(config()),
        target: Some(HttpRequestTarget {
            scheme: "https".into(),
            host: "fixture.example".into(),
            port: 443,
            method: "POST".into(),
            path: "/publish".into(),
            query: String::new(),
        }),
        headers: vec![HttpHeader {
            name: "content-type".into(),
            value: "application/json".into(),
        }],
        body: br#"{ "publication": "public text" }"#.to_vec(),
        middleware_name: "nomos-request-review".into(),
    }
}
struct Fake {
    score: f64,
    calls: AtomicUsize,
    delay: Duration,
}
#[async_trait]
impl DecisionProvider for Fake {
    async fn assess(
        &self,
        _state: serde_json::Value,
        catalog: &Catalog,
    ) -> Result<Assessment, ProviderError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        tokio::time::sleep(self.delay).await;
        Ok(Assessment {
            model: "fake".into(),
            scores: catalog
                .rules
                .iter()
                .map(|r| (r.id.clone(), self.score))
                .collect(),
        })
    }
}
fn fake(score: f64) -> Arc<Fake> {
    Arc::new(Fake {
        score,
        calls: AtomicUsize::new(0),
        delay: Duration::ZERO,
    })
}

#[tokio::test]
async fn inclusive_threshold_and_no_mutations() {
    for (score, decision) in [
        (0.69, Decision::Allow),
        (0.7, Decision::Deny),
        (1.0, Decision::Deny),
    ] {
        let service = ReviewService::new(fake(score));
        let result = service
            .evaluate_http_request(Request::new(request()))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(result.decision, decision as i32);
        assert!(!result.has_body);
        assert!(result.body.is_empty());
        assert!(result.header_mutations.is_empty());
    }
}

/// Set one policy rule through the actual protobuf configuration accepted by gRPC.
fn configured_request(deny_when: Option<&str>, threshold: f64) -> HttpRequestEvaluation {
    use prost_types::{Value, value::Kind};
    let mut request = request();
    let Some(Kind::ListValue(rules)) = request
        .config
        .as_mut()
        .unwrap()
        .fields
        .get_mut("rules")
        .unwrap()
        .kind
        .as_mut()
    else {
        unreachable!();
    };
    let Some(Kind::StructValue(rule)) = rules.values[0].kind.as_mut() else {
        unreachable!();
    };
    rule.fields.insert(
        "threshold".into(),
        Value {
            kind: Some(Kind::NumberValue(threshold)),
        },
    );
    if let Some(answer) = deny_when {
        rule.fields.insert(
            "deny_when".into(),
            Value {
                kind: Some(Kind::StringValue(answer.into())),
            },
        );
    }
    request
}

/// Both directions classify the original yes score; equality is on the yes side.
#[tokio::test]
async fn deny_when_defaults_to_yes_and_no_denies_below_the_same_threshold() {
    for (direction, threshold, score, expected) in [
        (None, 0.7, 0.699999, Decision::Allow),
        (None, 0.7, 0.7, Decision::Deny),
        (Some("yes"), 0.7, 0.699999, Decision::Allow),
        (Some("yes"), 0.7, 0.7, Decision::Deny),
        (Some("no"), 0.7, 0.3, Decision::Deny),
        (Some("no"), 0.7, 0.5, Decision::Deny),
        (Some("no"), 0.7, 0.699999, Decision::Deny),
        (Some("no"), 0.7, 0.7, Decision::Allow),
        (Some("no"), 0.7, 0.700001, Decision::Allow),
        (Some("no"), 0.1, 0.099999, Decision::Deny),
        (Some("no"), 0.1, 0.1, Decision::Allow),
        (Some("no"), 0.1, 0.100001, Decision::Allow),
        (Some("no"), 0.9, 0.899999, Decision::Deny),
        (Some("no"), 0.9, 0.9, Decision::Allow),
        (Some("no"), 0.9, 0.900001, Decision::Allow),
        (Some("yes"), 0.0, 0.0, Decision::Deny),
        (Some("yes"), 1.0, 0.999999, Decision::Allow),
        (Some("yes"), 1.0, 1.0, Decision::Deny),
        (Some("no"), 0.0, 0.0, Decision::Allow),
        (Some("no"), 0.0, 1.0, Decision::Allow),
        (Some("no"), 1.0, 0.0, Decision::Deny),
        (Some("no"), 1.0, 0.999999, Decision::Deny),
        (Some("no"), 1.0, 1.0, Decision::Allow),
    ] {
        let result = ReviewService::new(fake(score))
            .evaluate_http_request(Request::new(configured_request(direction, threshold)))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(
            result.decision, expected as i32,
            "{direction:?}, threshold={threshold}, yes={score}"
        );
        assert!(!result.has_body);
        assert!(result.header_mutations.is_empty());
    }
}

/// Either direction can independently deny; invalid scores never become an allow.
#[tokio::test]
async fn mixed_deny_directions_preserve_or_and_score_validation() {
    use prost_types::{Value, value::Kind};
    let mut request = configured_request(Some("yes"), 0.8);
    let Some(Kind::ListValue(rules)) = request
        .config
        .as_mut()
        .unwrap()
        .fields
        .get_mut("rules")
        .unwrap()
        .kind
        .as_mut()
    else {
        unreachable!();
    };
    let mut second = rules.values[0].clone();
    let Some(Kind::StructValue(rule)) = second.kind.as_mut() else {
        unreachable!();
    };
    rule.fields.insert(
        "id".into(),
        Value {
            kind: Some(Kind::StringValue("second".into())),
        },
    );
    rule.fields.insert(
        "deny_when".into(),
        Value {
            kind: Some(Kind::StringValue("no".into())),
        },
    );
    rule.fields.insert(
        "threshold".into(),
        Value {
            kind: Some(Kind::NumberValue(0.3)),
        },
    );
    rules.values.push(second);
    for (score, expected) in [
        (0.1, Decision::Deny),
        (0.3, Decision::Allow),
        (0.5, Decision::Allow),
        (0.8, Decision::Deny),
        (0.9, Decision::Deny),
    ] {
        let result = ReviewService::new(fake(score))
            .evaluate_http_request(Request::new(request.clone()))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(result.decision, expected as i32);
    }
    for invalid_request in [request, configured_request(Some("no"), 0.7)] {
        for score in [f64::NAN, f64::INFINITY, -0.1, 1.1] {
            let error = ReviewService::new(fake(score))
                .evaluate_http_request(Request::new(invalid_request.clone()))
                .await
                .unwrap_err();
            assert_eq!(error.code(), Code::Unavailable);
        }
    }
}

/// Only the documented string values are valid, and absence selects the legacy mode.
#[test]
fn deny_when_configuration_is_strict_and_defaults_to_yes() {
    let omitted = Catalog::from_proto(configured_request(None, 0.7).config).unwrap();
    assert_eq!(omitted.rules[0].deny_when, DenyWhen::Yes);
    for (value, expected) in [("yes", DenyWhen::Yes), ("no", DenyWhen::No)] {
        let catalog = Catalog::from_proto(configured_request(Some(value), 0.7).config).unwrap();
        assert_eq!(catalog.rules[0].deny_when, expected);
    }
    for invalid in [
        json!("maybe"),
        json!("YES"),
        json!(true),
        json!(false),
        json!(null),
        json!(1),
    ] {
        assert!(
            serde_json::from_value::<Catalog>(json!({
                "catalog_version":"test", "rules":[{
                    "id":"r", "question":"q", "threshold":0.7, "deny_when":invalid
                }]
            }))
            .is_err()
        );
    }
}

/// Invalid policy directions are rejected before any model work begins.
#[tokio::test]
async fn invalid_deny_direction_never_calls_the_model() {
    let provider = fake(0.1);
    let error = ReviewService::new(provider.clone())
        .evaluate_http_request(Request::new(configured_request(Some("maybe"), 0.7)))
        .await
        .unwrap_err();
    assert_eq!(error.code(), Code::InvalidArgument);
    assert_eq!(provider.calls.load(Ordering::SeqCst), 0);
}
#[tokio::test]
async fn different_methods_paths_and_json_fields_reach_assessment() {
    struct RecordingProvider(Mutex<Vec<serde_json::Value>>);
    #[async_trait]
    impl DecisionProvider for RecordingProvider {
        async fn assess(
            &self,
            state: serde_json::Value,
            catalog: &Catalog,
        ) -> Result<Assessment, ProviderError> {
            let score = if state["body"]["sensitive"] == true {
                0.9
            } else {
                0.1
            };
            self.0.lock().unwrap().push(state);
            Ok(Assessment {
                model: "fake".into(),
                scores: catalog
                    .rules
                    .iter()
                    .map(|rule| (rule.id.clone(), score))
                    .collect(),
            })
        }
    }
    let provider = Arc::new(RecordingProvider(Mutex::new(Vec::new())));
    let service = ReviewService::new(provider.clone());
    for (method, path) in [
        ("POST", "/messages"),
        ("PUT", "/documents/123"),
        ("PATCH", "/v1/notes/a%20b"),
        ("GET", "/search"),
        ("CUSTOM", "/extensions"),
    ] {
        for sensitive in [false, true] {
            let mut input = request();
            let target = input.target.as_mut().unwrap();
            target.method = method.into();
            target.path = path.into();
            target.query = "revision=2".into();
            let body = json!({"message": "Fictional JSON content", "sensitive": sensitive});
            input.body = serde_json::to_vec(&body).unwrap();
            let result = service
                .evaluate_http_request(Request::new(input))
                .await
                .unwrap()
                .into_inner();
            assert_eq!(
                result.decision,
                if sensitive {
                    Decision::Deny
                } else {
                    Decision::Allow
                } as i32
            );
            assert!(!result.has_body);
            assert!(result.body.is_empty());
            assert!(result.header_mutations.is_empty());
            let observed = provider.0.lock().unwrap();
            let state = observed.last().unwrap();
            assert_eq!(state["method"], method);
            assert_eq!(state["path"], path);
            assert_eq!(state["query"], "revision=2");
            assert_eq!(state["body"], body);
        }
    }
    assert_eq!(provider.0.lock().unwrap().len(), 10);
}

/// Check the exact rejection reason for each independent input guard, without model I/O.
#[tokio::test]
async fn invalid_input_never_calls_provider() {
    let provider = fake(0.0);
    let service = ReviewService::new(provider.clone());
    let mut cases = Vec::new();
    for (name, body, code, reason) in [
        (
            "duplicate root key",
            br#"{"a":1,"a":2}"#.to_vec(),
            Code::InvalidArgument,
            "invalid_json",
        ),
        (
            "duplicate nested key",
            br#"{"a":{"b":1,"b":2}}"#.to_vec(),
            Code::InvalidArgument,
            "invalid_json",
        ),
        (
            "array body",
            b"[]".to_vec(),
            Code::InvalidArgument,
            "object_body_required",
        ),
        (
            "null body",
            b"null".to_vec(),
            Code::InvalidArgument,
            "object_body_required",
        ),
        (
            "empty body",
            Vec::new(),
            Code::InvalidArgument,
            "invalid_json",
        ),
        (
            "trailing JSON",
            b"{}{}".to_vec(),
            Code::InvalidArgument,
            "invalid_json",
        ),
        (
            "invalid UTF-8",
            vec![0xff],
            Code::InvalidArgument,
            "invalid_json",
        ),
        (
            "oversized body",
            vec![b' '; nomos_request_review::DEFAULT_MAX_BODY_BYTES as usize + 1],
            Code::ResourceExhausted,
            "body_over_limit",
        ),
    ] {
        let mut r = request();
        r.body = body;
        cases.push((name, r, code, reason));
    }
    let mut r = request();
    r.headers.push(r.headers[0].clone());
    cases.push((
        "duplicate content type",
        r,
        Code::InvalidArgument,
        "unsupported_encoding",
    ));
    let mut r = request();
    r.headers.clear();
    cases.push((
        "missing content type",
        r,
        Code::InvalidArgument,
        "unsupported_encoding",
    ));
    let mut r = request();
    r.headers[0].value = "text/plain".into();
    cases.push((
        "non-JSON content type",
        r,
        Code::InvalidArgument,
        "unsupported_encoding",
    ));
    let mut r = request();
    r.headers.push(HttpHeader {
        name: "content-encoding".into(),
        value: "gzip".into(),
    });
    cases.push((
        "compressed body",
        r,
        Code::InvalidArgument,
        "unsupported_encoding",
    ));
    for method in ["", "POST GET", "POST\r\nInjected: yes", &"A".repeat(65)] {
        let mut r = request();
        r.target.as_mut().unwrap().method = method.into();
        cases.push((
            "invalid method",
            r,
            Code::InvalidArgument,
            "unsupported_request",
        ));
    }
    for path in [
        "",
        "relative",
        "/with space",
        "/line\n",
        "/p?q=1",
        "/p#fragment",
        &format!("/{}", "x".repeat(8192)),
    ] {
        let mut r = request();
        r.target.as_mut().unwrap().path = path.into();
        cases.push((
            "invalid path",
            r,
            Code::InvalidArgument,
            "unsupported_request",
        ));
    }
    let mut r = request();
    r.target.as_mut().unwrap().scheme = "ftp".into();
    cases.push((
        "unsupported scheme",
        r,
        Code::InvalidArgument,
        "unsupported_request",
    ));
    for port in [0, 65536] {
        let mut r = request();
        r.target.as_mut().unwrap().port = port;
        cases.push((
            "invalid port",
            r,
            Code::InvalidArgument,
            "unsupported_request",
        ));
    }
    let mut r = request();
    r.target.as_mut().unwrap().host.clear();
    cases.push((
        "empty host",
        r,
        Code::InvalidArgument,
        "unsupported_request",
    ));
    let mut r = request();
    r.target.as_mut().unwrap().query = "x".repeat(2049);
    cases.push((
        "oversized query",
        r,
        Code::InvalidArgument,
        "unsupported_request",
    ));
    let mut r = request();
    r.phase = 0;
    cases.push((
        "unsupported phase",
        r,
        Code::InvalidArgument,
        "unsupported_phase",
    ));
    let mut r = request();
    r.context = None;
    cases.push((
        "missing context",
        r,
        Code::InvalidArgument,
        "missing_context",
    ));
    let mut r = request();
    r.context.as_mut().unwrap().request_id.clear();
    cases.push((
        "invalid context",
        r,
        Code::InvalidArgument,
        "invalid_context",
    ));
    let mut r = request();
    r.target = None;
    cases.push(("missing target", r, Code::InvalidArgument, "missing_target"));
    let mut r = request();
    r.config = None;
    cases.push(("missing config", r, Code::InvalidArgument, "invalid_config"));
    for (name, r, code, reason) in cases {
        let error = service
            .evaluate_http_request(Request::new(r))
            .await
            .unwrap_err();
        assert_eq!(error.code(), code, "{name}");
        assert_eq!(error.message(), reason, "{name}");
    }
    assert_eq!(provider.calls.load(Ordering::SeqCst), 0);
}

/// An explicit identity content encoding must still reach the provider and allow.
#[tokio::test]
async fn identity_encoding_is_accepted() {
    let provider = fake(0.0);
    let service = ReviewService::new(provider.clone());
    let mut input = request();
    input.headers.push(HttpHeader {
        name: "content-encoding".into(),
        value: "identity".into(),
    });
    let result = service
        .evaluate_http_request(Request::new(input))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(result.decision, Decision::Allow as i32);
    assert_eq!(provider.calls.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn describe_negotiates_request_only_without_model_call() {
    let provider = fake(0.0);
    let service = ReviewService::new(provider.clone());
    assert_eq!(
        service
            .describe(Request::new(MiddlewareDescribeRequest { gateway: None }))
            .await
            .unwrap_err()
            .code(),
        Code::FailedPrecondition
    );
    let mut peer = metadata();
    peer.protocol_version.as_mut().unwrap().major = 2;
    assert!(
        service
            .describe(Request::new(MiddlewareDescribeRequest {
                gateway: Some(peer)
            }))
            .await
            .is_err()
    );
    let mut peer = metadata();
    peer.required_capabilities.push("future.feature".into());
    assert!(
        service
            .describe(Request::new(MiddlewareDescribeRequest {
                gateway: Some(peer)
            }))
            .await
            .is_err()
    );
    let manifest = service
        .describe(Request::new(MiddlewareDescribeRequest {
            gateway: Some(metadata()),
        }))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(manifest.bindings.len(), 1);
    assert_eq!(
        manifest.bindings[0].operation,
        SupervisorMiddlewareOperation::HttpRequest as i32
    );
    assert_eq!(provider.calls.load(Ordering::SeqCst), 0);
}
/// Reject ambiguous or unusable operator policies before they can control an assessment.
#[test]
fn configuration_rejects_unknown_fields_duplicate_ids_and_bad_thresholds() {
    for value in [
        json!({"catalog_version":"test","rules":[]}),
        json!({"catalog_version":"test","rules":[],"extra":true}),
        json!({"catalog_version":"test","rules":[{"id":"r","question":"q","threshold":1.1}]}),
        json!({"catalog_version":"test","rules":[{"id":"r","question":"q","threshold":0.8},{"id":"r","question":"q","threshold":0.8}]}),
    ] {
        let parsed = serde_json::from_value::<Catalog>(value);
        assert!(parsed.is_err() || parsed.unwrap().validate().is_err());
    }
}
/// Accept larger catalogs while still requiring one valid model answer for every rule.
#[test]
fn configuration_accepts_more_than_eight_questions() {
    for count in [9, 11, 64] {
        let catalog: Catalog = serde_json::from_value(json!({
            "catalog_version": "test",
            "rules": (0..count).map(|i| json!({
                "id": format!("rule_{i}"),
                "question": "Does this request disclose confidential information?",
                "threshold": 0.7,
            })).collect::<Vec<_>>(),
        }))
        .unwrap();
        catalog.validate().unwrap();

        let answers: serde_json::Map<String, serde_json::Value> = catalog
            .rules
            .iter()
            .map(|rule| (rule.id.clone(), json!({"type": "noul", "noul": 0.1})))
            .collect();
        let assessment = nomos_request_review::provider::validate_response(
            json!({"model": "m", "answers": answers}),
            "m",
            &catalog,
        )
        .unwrap();
        assert_eq!(assessment.scores.len(), count);
    }
}
#[test]
fn provider_rejects_incomplete_wrong_type_out_of_range_and_wrong_model() {
    let catalog = Catalog::from_proto(Some(config())).unwrap();
    for value in [
        json!({"model":"m","answers":{}}),
        json!({"model":"m","answers":{"disclosure":{"type":"choice","noul":0.1}}}),
        json!({"model":"m","answers":{"disclosure":{"type":"noul","noul":1.1}}}),
        json!({"model":"other","answers":{"disclosure":{"type":"noul","noul":0.1}}}),
        json!({"model":"m","answers":{"disclosure":{"type":"noul","noul":0.1},"extra":{"type":"noul","noul":0.1}}}),
    ] {
        assert!(nomos_request_review::provider::validate_response(value, "m", &catalog).is_err());
    }
    assert!(strict_json::parse(br#"{"answers":{"r":1,"r":0}}"#).is_err());
}
#[tokio::test(flavor = "current_thread")]
async fn failures_log_only_valid_correlation_ids_without_payloads() {
    struct Unavailable;
    #[async_trait]
    impl DecisionProvider for Unavailable {
        async fn assess(
            &self,
            _: serde_json::Value,
            _: &Catalog,
        ) -> Result<Assessment, ProviderError> {
            Err(ProviderError::Unavailable)
        }
    }
    let (logs, _guard) = capture_logs();
    let service = ReviewService::new(Arc::new(Unavailable));
    let mut inputs = vec![request(); 6];
    inputs[0].body = br#"{"message":"DO_NOT_LOG_BODY"}"#.to_vec();
    inputs[0].target.as_mut().unwrap().query = "token=DO_NOT_LOG_QUERY".into();
    inputs[0].headers.push(HttpHeader {
        name: "x-private".into(),
        value: "DO_NOT_LOG_HEADER".into(),
    });
    inputs[1].config = None;
    inputs[2].context.as_mut().unwrap().request_id = "DO_NOT_LOG_ID\n".into();
    inputs[3].context.as_mut().unwrap().sandbox_id = "X".repeat(129);
    inputs[4].context.as_mut().unwrap().request_id.clear();
    inputs[5].context = None;
    for input in inputs {
        assert!(
            service
                .evaluate_http_request(Request::new(input))
                .await
                .is_err()
        );
    }
    let failures = logs.failures();
    assert_eq!(failures.len(), 6);
    for event in &failures[..2] {
        assert_eq!(event["request_id"], "req-1");
        assert_eq!(event["sandbox_id"], "sandbox-1");
    }
    assert_eq!(failures[0]["code"], "Unavailable");
    assert_eq!(failures[1]["code"], "InvalidArgument");
    for index in [2, 4] {
        assert!(failures[index].get("request_id").is_none());
        assert_eq!(failures[index]["sandbox_id"], "sandbox-1");
    }
    assert_eq!(failures[3]["request_id"], "req-1");
    assert!(failures[3].get("sandbox_id").is_none());
    assert!(failures[5].get("request_id").is_none());
    assert!(failures[5].get("sandbox_id").is_none());
    let text = String::from_utf8(logs.0.lock().unwrap().clone()).unwrap();
    assert!(!text.contains("DO_NOT_LOG"));
    assert!(!text.contains(&"X".repeat(129)));
}

#[tokio::test(flavor = "current_thread")]
async fn capacity_rejects_excess_work_and_deadline_releases_permits() {
    let (logs, _guard) = capture_logs();
    let provider = Arc::new(Fake {
        score: 0.1,
        calls: AtomicUsize::new(0),
        delay: Duration::from_secs(10),
    });
    // Exercise capacity/deadline behavior with a short explicit test budget;
    // production defaults are checked independently in the timeout module.
    let service = Arc::new(ReviewService::with_timeouts(
        provider.clone(),
        Timeouts::from_millis(50, 100).unwrap(),
    ));
    let mut jobs = Vec::new();
    for _ in 0..4 {
        let s = service.clone();
        jobs.push(tokio::spawn(async move {
            s.evaluate_http_request(Request::new(request())).await
        }));
    }
    tokio::time::timeout(Duration::from_secs(1), async {
        while provider.calls.load(Ordering::SeqCst) < 4 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        service
            .evaluate_http_request(Request::new(request()))
            .await
            .unwrap_err()
            .code(),
        Code::ResourceExhausted
    );
    for job in jobs {
        assert_eq!(
            job.await.unwrap().unwrap_err().code(),
            Code::DeadlineExceeded
        );
    }
    assert_eq!(
        service
            .evaluate_http_request(Request::new(request()))
            .await
            .unwrap_err()
            .code(),
        Code::DeadlineExceeded
    );
    let failures = logs.failures();
    assert_eq!(failures.len(), 6);
    assert_eq!(
        failures
            .iter()
            .filter(|event| event["code"] == "ResourceExhausted")
            .count(),
        1
    );
    assert_eq!(
        failures
            .iter()
            .filter(|event| event["code"] == "DeadlineExceeded")
            .count(),
        5
    );
    for event in failures {
        assert_eq!(event["request_id"], "req-1");
        assert_eq!(event["sandbox_id"], "sandbox-1");
        assert!(event["total_ms"].is_u64());
    }
}

/// Exercise the OpenShell gRPC to native SystemOne HTTP boundary using local transports.
#[tokio::test]
async fn real_grpc_to_native_http_wire_contract() {
    use axum::{Json, Router, routing::post};
    let http = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let http_addr = http.local_addr().unwrap();
    let calls = Arc::new(AtomicUsize::new(0));
    let captured = calls.clone();
    let app = Router::new().route(
        "/v1/systemone",
        post(move |Json(v): Json<serde_json::Value>| {
            let c = captured.clone();
            async move {
                c.fetch_add(1, Ordering::SeqCst);
                assert_eq!(v["model"], "example-model");
                assert_eq!(v["questions"]["disclosure"]["type"], "noul");
                assert_eq!(v["state"]["body"]["publication"], "public text");
                Json(json!({"model":"example-model-v1","answers":{"disclosure":{"type":"noul","noul":0.9}}}))
            }
        }),
    );
    let http_job = tokio::spawn(async move { axum::serve(http, app).await.unwrap() });
    let provider = SystemOneProvider::new(
        &format!("http://{http_addr}/v1/systemone"),
        "example-model".into(),
        "example-model-v1".into(),
        None,
        true,
    )
    .unwrap();
    let grpc = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let grpc_addr = grpc.local_addr().unwrap();
    let grpc_job = tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(
                supervisor_middleware_server::SupervisorMiddlewareServer::new(ReviewService::new(
                    Arc::new(provider),
                )),
            )
            .serve_with_incoming(tokio_stream::wrappers::TcpListenerStream::new(grpc))
            .await
            .unwrap()
    });
    let mut client = supervisor_middleware_client::SupervisorMiddlewareClient::connect(format!(
        "http://{grpc_addr}"
    ))
    .await
    .unwrap();
    client
        .describe(MiddlewareDescribeRequest {
            gateway: Some(metadata()),
        })
        .await
        .unwrap();
    assert!(
        client
            .validate_config(ValidateConfigRequest {
                config: Some(config()),
                middleware_name: "nomos-request-review".into()
            })
            .await
            .unwrap()
            .into_inner()
            .valid
    );
    let result = client
        .evaluate_http_request(request())
        .await
        .unwrap()
        .into_inner();
    assert_eq!(result.decision, Decision::Deny as i32);
    assert_eq!(result.reason_code, "risk_detected");
    assert!(!result.has_body);
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    grpc_job.abort();
    http_job.abort();
}

#[tokio::test]
async fn http_response_limits_redirects_and_parse_errors_fail() {
    use axum::{Router, http::StatusCode, response::IntoResponse, routing::post};
    let catalog = Catalog::from_proto(Some(config())).unwrap();
    for body in [
        "not json".to_string(),
        "x".repeat(65537),
        r#"{"model":"m","answers":{"disclosure":{"type":"noul","noul":0.1,"noul":0.9}}}"#.into(),
    ] {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let app = Router::new().route(
            "/v1/systemone",
            post(move || {
                let b = body.clone();
                async move { (StatusCode::OK, b).into_response() }
            }),
        );
        let job = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let provider = SystemOneProvider::new(
            &format!("http://{addr}/v1/systemone"),
            "m".into(),
            "m".into(),
            None,
            true,
        )
        .unwrap();
        assert!(provider.assess(json!({}), &catalog).await.is_err());
        job.abort();
    }
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = Router::new().route(
        "/v1/systemone",
        post(|| async {
            (
                StatusCode::TEMPORARY_REDIRECT,
                [("location", "/should-not-follow")],
            )
        }),
    );
    let job = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let provider = SystemOneProvider::new(
        &format!("http://{addr}/v1/systemone"),
        "m".into(),
        "m".into(),
        None,
        true,
    )
    .unwrap();
    assert!(matches!(
        provider.assess(json!({}), &catalog).await,
        Err(ProviderError::Unavailable)
    ));
    job.abort();
}

#[tokio::test]
async fn configured_evaluation_deadline_cancels_assessment_and_releases_capacity() {
    let provider = Arc::new(Fake {
        score: 0.1,
        calls: AtomicUsize::new(0),
        delay: Duration::from_millis(200),
    });
    let short =
        ReviewService::with_timeouts(provider.clone(), Timeouts::from_millis(20, 40).unwrap());
    for _ in 0..5 {
        assert_eq!(
            short
                .evaluate_http_request(Request::new(request()))
                .await
                .unwrap_err()
                .code(),
            Code::DeadlineExceeded
        );
    }
    // Five consecutive expirations must not consume the four permits forever.
    let long =
        ReviewService::with_timeouts(provider.clone(), Timeouts::from_millis(1000, 2000).unwrap());
    let result = long
        .evaluate_http_request(Request::new(request()))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(result.decision, Decision::Allow as i32);
    assert!(!result.has_body);
    assert!(result.header_mutations.is_empty());
    assert_eq!(provider.calls.load(Ordering::SeqCst), 6);
}

#[tokio::test]
async fn configured_model_timeout_bounds_http_assessment() {
    use axum::{Json, Router, routing::post};
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = Router::new().route(
        "/v1/systemone",
        post(|| async {
            tokio::time::sleep(Duration::from_millis(200)).await;
            Json(json!({"model":"m", "answers":{"disclosure":{"type":"noul","noul":0.1}}}))
        }),
    );
    let job = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let catalog = Catalog::from_proto(Some(config())).unwrap();
    for (budget, should_succeed) in [(40, false), (2000, true)] {
        let provider = SystemOneProvider::with_timeouts(
            &format!("http://{addr}/v1/systemone"),
            "m".into(),
            "m".into(),
            None,
            true,
            Timeouts::from_millis(budget, budget + 1000).unwrap(),
        )
        .unwrap();
        let result = provider
            .assess(json!({"publication":"fictional"}), &catalog)
            .await;
        if should_succeed {
            assert_eq!(result.unwrap().scores["disclosure"], 0.1);
        } else {
            assert!(matches!(result, Err(ProviderError::Unavailable)));
        }
    }
    job.abort();
}

#[tokio::test(flavor = "current_thread")]
async fn model_telemetry_is_correlated_bounded_and_survives_cancellation() {
    use tracing::Instrument;
    let (logs, _guard) = capture_logs();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let app = axum::Router::new().route(
        "/v1/systemone",
        axum::routing::post(|| async {
            tokio::time::sleep(Duration::from_millis(100)).await;
            axum::Json(
                json!({"model":"m", "answers":{"disclosure":{"type":"noul","noul":0.1}},
            "usage":{"input_tokens":13,"output_tokens":2,"output_tokens_basis":"scored_decision_positions","generated_text_tokens":0,"decision_positions":2}, "metrics":{"seconds":0.01,"engine_seconds":0.008,"prepare_seconds":0.002,"upstream_calls":1}, "private":"DO_NOT_LOG_RESPONSE"}),
            )
        }),
    );
    let job = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let provider = SystemOneProvider::new(
        &format!("http://{address}/v1/systemone"),
        "m".into(),
        "m".into(),
        None,
        true,
    )
    .unwrap();
    let catalog = Catalog::from_proto(Some(config())).unwrap();
    provider
        .assess(json!({"text":"DO_NOT_LOG_OBSERVATION"}), &catalog)
        .instrument(tracing::info_span!(
            "review",
            request_id = "telemetry-complete",
            sandbox_id = "sandbox-1"
        ))
        .await
        .unwrap();
    assert!(
        tokio::time::timeout(
            Duration::from_millis(10),
            provider
                .assess(json!({}), &catalog)
                .instrument(tracing::info_span!(
                    "review",
                    request_id = "telemetry-cancelled",
                    sandbox_id = "sandbox-1"
                ))
        )
        .await
        .is_err()
    );
    let text = String::from_utf8(logs.0.lock().unwrap().clone()).unwrap();
    assert!(!text.contains("DO_NOT_LOG"));
    let events: Vec<serde_json::Value> = text
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    let calls: Vec<_> = events
        .iter()
        .filter(|e| e["fields"]["message"] == "model_call_finished")
        .collect();
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[0]["span"]["request_id"], "telemetry-complete");
    assert_eq!(calls[0]["fields"]["model_outcome"], "complete");
    assert_eq!(calls[0]["fields"]["input_tokens"], 13);
    assert_eq!(calls[0]["fields"]["output_tokens"], 2);
    assert_eq!(
        calls[0]["fields"]["output_tokens_basis"],
        "scored_decision_positions"
    );
    assert_eq!(calls[0]["fields"]["generated_text_tokens"], 0);
    assert_eq!(calls[0]["fields"]["server_engine_ms"].as_f64(), Some(8.0));
    assert_eq!(calls[0]["fields"]["upstream_calls"], 1);
    assert_eq!(calls[0]["fields"]["model_completion_observed"], true);
    assert_eq!(calls[1]["span"]["request_id"], "telemetry-cancelled");
    assert_eq!(calls[1]["fields"]["model_outcome"], "cancelled");
    assert_eq!(calls[1]["fields"]["model_completion_observed"], false);
    assert!(calls[1]["fields"]["model_wait_ms"].as_f64().unwrap() >= 5.0);
    assert!(calls[1]["fields"].get("input_tokens").is_none());
    job.abort();
}

#[tokio::test(flavor = "current_thread")]
async fn telemetry_does_not_promote_missing_or_invalid_usage_to_token_counts() {
    let (logs, _guard) = capture_logs();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let app = axum::Router::new().route(
        "/v1/systemone",
        axum::routing::post(|| async {
            axum::Json(
                json!({"model":"m", "answers":{"disclosure":{"type":"noul","noul":0.1}},
            "usage":{"input_tokens":-1,"output_tokens":"2"}, "private":"DO_NOT_LOG_USAGE"}),
            )
        }),
    );
    let job = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let provider = SystemOneProvider::new(
        &format!("http://{address}/v1/systemone"),
        "m".into(),
        "m".into(),
        None,
        true,
    )
    .unwrap();
    provider
        .assess(json!({}), &Catalog::from_proto(Some(config())).unwrap())
        .await
        .unwrap();
    let text = String::from_utf8(logs.0.lock().unwrap().clone()).unwrap();
    assert!(!text.contains("DO_NOT_LOG"));
    let event = text
        .lines()
        .map(|l| serde_json::from_str::<serde_json::Value>(l).unwrap())
        .find(|v| v["fields"]["message"] == "model_call_finished")
        .unwrap();
    assert!(event["fields"].get("input_tokens").is_none());
    assert!(event["fields"].get("output_tokens").is_none());
    assert!(event["fields"].get("usage_schema").is_none());
    assert_eq!(event["fields"]["model_outcome"], "complete");
    job.abort();
}

/// Build a valid JSON object with an exact original byte length for boundary checks.
fn body_of_size(size: usize) -> Vec<u8> {
    let mut body = br#"{"message":""#.to_vec();
    body.extend(std::iter::repeat_n(b'x', size - body.len() - 2));
    body.extend_from_slice(br#""}"#);
    assert_eq!(body.len(), size);
    body
}

/// Check inclusive body acceptance, custom discovery limits and transport rejection before inference.
#[tokio::test]
async fn grpc_and_advertised_body_limits_are_independent() {
    use nomos_request_review::{DEFAULT_MAX_BODY_BYTES, DEFAULT_MAX_GRPC_MESSAGE_BYTES};
    let provider = fake(0.1);
    let service = ReviewService::with_limits(
        provider.clone(),
        Timeouts::default(),
        DEFAULT_MAX_BODY_BYTES,
    );
    let grpc = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = grpc.local_addr().unwrap();
    let job = tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(
                supervisor_middleware_server::SupervisorMiddlewareServer::new(service)
                    .max_decoding_message_size(DEFAULT_MAX_GRPC_MESSAGE_BYTES as usize)
                    .max_encoding_message_size(DEFAULT_MAX_GRPC_MESSAGE_BYTES as usize),
            )
            .serve_with_incoming(tokio_stream::wrappers::TcpListenerStream::new(grpc))
            .await
            .unwrap();
    });
    let mut client =
        supervisor_middleware_client::SupervisorMiddlewareClient::connect(format!("http://{addr}"))
            .await
            .unwrap();
    let manifest = client
        .describe(MiddlewareDescribeRequest {
            gateway: Some(metadata()),
        })
        .await
        .unwrap()
        .into_inner();
    assert_eq!(
        manifest.bindings[0].max_payload_bytes,
        u64::from(DEFAULT_MAX_BODY_BYTES)
    );
    for size in [DEFAULT_MAX_BODY_BYTES - 1, DEFAULT_MAX_BODY_BYTES] {
        let mut input = request();
        input.body = body_of_size(size as usize);
        assert_eq!(
            client
                .evaluate_http_request(input)
                .await
                .unwrap()
                .into_inner()
                .decision,
            Decision::Allow as i32
        );
    }
    let mut input = request();
    input.body = body_of_size(DEFAULT_MAX_BODY_BYTES as usize + 1);
    assert_eq!(
        client
            .evaluate_http_request(input)
            .await
            .unwrap_err()
            .code(),
        Code::ResourceExhausted
    );
    let mut input = request();
    input.headers.push(HttpHeader {
        name: "x-large".into(),
        value: "x".repeat(DEFAULT_MAX_GRPC_MESSAGE_BYTES as usize),
    });
    // Tonic 0.14 returns OutOfRange from the receive decoder, before the handler.
    assert_eq!(
        client
            .evaluate_http_request(input)
            .await
            .unwrap_err()
            .code(),
        Code::OutOfRange
    );
    assert_eq!(provider.calls.load(Ordering::SeqCst), 2);
    job.abort();

    let custom = ReviewService::with_limits(provider.clone(), Timeouts::default(), 1024);
    let manifest = custom
        .describe(Request::new(MiddlewareDescribeRequest {
            gateway: Some(metadata()),
        }))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(manifest.bindings[0].max_payload_bytes, 1024);
    let mut input = request();
    input.body = body_of_size(1025);
    assert_eq!(
        custom
            .evaluate_http_request(Request::new(input))
            .await
            .unwrap_err()
            .code(),
        Code::ResourceExhausted
    );
    assert_eq!(provider.calls.load(Ordering::SeqCst), 2);
}

// Pretty JSON and escaped strings must preserve the text assessed by the model.
/// Preserve large string content through compact model serialization without an assessment cap.
#[tokio::test]
async fn large_model_request_preserves_text_without_separate_assessment_cap() {
    use axum::{Json, Router, routing::post};
    let text = format!(
        "{}\n\tQuoted: \"中文\" and backslash \\",
        "x".repeat(32 * 1024)
    );
    let mut input = request();
    input.body = serde_json::to_vec_pretty(&json!({"message": text})).unwrap();
    let expected = text.clone();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = Router::new().route(
        "/v1/systemone",
        post(move |Json(value): Json<serde_json::Value>| {
            let expected = expected.clone();
            async move {
                assert_eq!(value["state"]["body"]["message"], expected);
                Json(json!({"model":"m","answers":{"disclosure":{"type":"noul","noul":0.1}}}))
            }
        }),
    );
    let job = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let provider = SystemOneProvider::new(
        &format!("http://{addr}/v1/systemone"),
        "m".into(),
        "m".into(),
        None,
        true,
    )
    .unwrap();
    let service = ReviewService::new(Arc::new(provider));
    assert_eq!(
        service
            .evaluate_http_request(Request::new(input))
            .await
            .unwrap()
            .into_inner()
            .decision,
        Decision::Allow as i32
    );
    job.abort();
}

/// Preserve size-error classification without leaking upstream bodies or conflating schema errors.
#[tokio::test]
async fn upstream_size_errors_are_sanitized_and_distinguished_from_schema_errors() {
    use axum::{Router, http::StatusCode, routing::post};
    let cases = [
        (
            400,
            r#"{"code":"max_tokens_exceeded"}"#.into(),
            Code::ResourceExhausted,
        ),
        (
            400,
            r#"{"detail":{"code":"max_tokens_exceeded"}}"#.into(),
            Code::ResourceExhausted,
        ),
        (
            400,
            r#"{"code":"other","code":"max_tokens_exceeded"}"#.into(),
            Code::Unavailable,
        ),
        (
            400,
            r#"{"error":{"code":"max_tokens_exceeded","message":"DO_NOT_LOG_ERROR"}}"#.to_string(),
            Code::ResourceExhausted,
        ),
        (
            400,
            r#"{"detail":"max_tokens_exceeded"}"#.to_string(),
            Code::ResourceExhausted,
        ),
        (
            400,
            r#"{"error":{"message":"DO_NOT_LOG_ERROR max_tokens_exceeded"}}"#.to_string(),
            Code::Unavailable,
        ),
        (400, "not json".into(), Code::Unavailable),
        (400, "x".repeat(65537), Code::Unavailable),
        (
            422,
            r#"{"detail":[{"type":"missing","msg":"DO_NOT_LOG_ERROR"}]}"#.into(),
            Code::Unavailable,
        ),
        (
            422,
            r#"{"error":{"code":"max_tokens_exceeded"}}"#.into(),
            Code::Unavailable,
        ),
        (
            401,
            r#"{"code":"max_tokens_exceeded"}"#.into(),
            Code::Unavailable,
        ),
        (
            500,
            r#"{"code":"max_tokens_exceeded"}"#.into(),
            Code::Unavailable,
        ),
        (413, "".into(), Code::ResourceExhausted),
        (
            413,
            "DO_NOT_LOG_ERROR".repeat(5000),
            Code::ResourceExhausted,
        ),
    ];
    let (logs, _guard) = capture_logs();
    for (status, body, expected) in cases {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let app = Router::new().route(
            "/v1/systemone",
            post(move || {
                let body = body.clone();
                async move { (StatusCode::from_u16(status).unwrap(), body) }
            }),
        );
        let job = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let provider = SystemOneProvider::new(
            &format!("http://{addr}/v1/systemone"),
            "m".into(),
            "m".into(),
            None,
            true,
        )
        .unwrap();
        let service = ReviewService::new(Arc::new(provider));
        let error = service
            .evaluate_http_request(Request::new(request()))
            .await
            .unwrap_err();
        assert_eq!(error.code(), expected, "upstream status {status}");
        assert!(!error.message().contains("DO_NOT_LOG_ERROR"));
        job.abort();
    }
    let text = String::from_utf8(logs.0.lock().unwrap().clone()).unwrap();
    assert!(!text.contains("DO_NOT_LOG_ERROR"));
    assert!(text.contains("input_over_limit"));
}

/// Preserve HTTP-failure telemetry when an error body exceeds the response bound.
#[tokio::test]
async fn oversized_model_responses_distinguish_http_errors_from_invalid_answers() {
    use axum::{Router, http::StatusCode, routing::post};

    let (logs, _guard) = capture_logs();
    for (status, expected_outcome) in [
        (200, "invalid_assessment"),
        (400, "http_error"),
        (503, "http_error"),
    ] {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let app = Router::new().route(
            "/v1/systemone",
            post(move || async move {
                (
                    StatusCode::from_u16(status).unwrap(),
                    "DO_NOT_LOG".repeat(7000),
                )
            }),
        );
        let job = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let provider = SystemOneProvider::new(
            &format!("http://{addr}/v1/systemone"),
            "example-model".into(),
            "example-model".into(),
            None,
            true,
        )
        .unwrap();
        let catalog = Catalog::from_proto(Some(config())).unwrap();
        let error = provider
            .assess(json!({"body": {}}), &catalog)
            .await
            .err()
            .unwrap();
        assert!(
            matches!((&error, status), (ProviderError::Invalid, 200))
                || matches!((&error, status), (ProviderError::Unavailable, 400 | 503)),
            "status {status} returned {error:?}",
        );
        let text = String::from_utf8(logs.0.lock().unwrap().clone()).unwrap();
        let event = text
            .lines()
            .map(|line| serde_json::from_str::<serde_json::Value>(line).unwrap())
            .rfind(|event| event["fields"]["message"] == "model_call_finished")
            .unwrap();
        assert_eq!(event["fields"]["model_outcome"], expected_outcome);
        assert_eq!(event["fields"]["model_completion_observed"], false);
        assert!(!text.contains("DO_NOT_LOG"));
        job.abort();
    }
}
