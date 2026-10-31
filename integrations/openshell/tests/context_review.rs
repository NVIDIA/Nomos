// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! Independent service-boundary checks for authenticated, budgeted context review.

use async_trait::async_trait;
use axum::{
    Router,
    body::{Body, Bytes},
    extract::State,
    http::{HeaderMap, Response, StatusCode},
    routing::post,
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use jsonwebtoken::{Algorithm, EncodingKey, Header, encode};
use nomos_request_review::{
    DEFAULT_MAX_BODY_BYTES,
    config::Catalog,
    context_budget::{BudgetLimits, MAX_CONTEXT_BYTES},
    context_reader::{ContextReader, ContextReaderOptions, LookupMode, MAX_LOOKUP_RESPONSE_BYTES},
    extension_auth::ExtensionAuth,
    openshell::middleware::v1::*,
    provider::{Assessment, DecisionProvider, ProviderError},
    service::{ReviewService, metadata},
    timeouts::Timeouts,
};
use ring::{
    rand::SystemRandom,
    signature::{Ed25519KeyPair, KeyPair},
};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use supervisor_middleware_server::SupervisorMiddleware;
use time::{OffsetDateTime, format_description::well_known::Rfc3339};
use tokio::net::TcpListener;
use tonic::{Code, Request};

const TOKEN: &str = "test-only-reader-credential-for-context-001";
const NAME: &str = "hermes-context-a";

struct Identity {
    auth: Arc<ExtensionAuth>,
    key: EncodingKey,
}

impl Identity {
    fn new() -> Self {
        let document = Ed25519KeyPair::generate_pkcs8(&SystemRandom::new()).unwrap();
        let key = Ed25519KeyPair::from_pkcs8(document.as_ref()).unwrap();
        let jwks = json!({"keys":[{"kty":"OKP","crv":"Ed25519","kid":"test-key",
            "x":URL_SAFE_NO_PAD.encode(key.public_key().as_ref()),"alg":"EdDSA","use":"sig"}]});
        Self {
            auth: Arc::new(
                ExtensionAuth::from_jwks(
                    &serde_json::to_vec(&jwks).unwrap(),
                    "pilot-gateway",
                    "urn:nomos:test",
                )
                .unwrap(),
            ),
            key: EncodingKey::from_ed_der(document.as_ref()),
        }
    }

    fn claims(&self) -> Value {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();
        json!({"iss":"openshell-gateway:pilot-gateway","aud":"urn:nomos:test",
            "sub":"spiffe://openshell/sandbox/sandbox-A","iat":now,"exp":now+300,
            "jti":"reusable-test-token","caller_kind":"supervisor","sandbox_id":"sandbox-A"})
    }

    /// Sign any RPC request with the same generated OpenShell identity.
    fn request<T>(&self, request: T, claims: Value) -> Request<T> {
        let mut header = Header::new(Algorithm::EdDSA);
        header.kid = Some("test-key".into());
        header.typ = Some("openshell-ext+jwt".into());
        let token = encode(&header, &claims, &self.key).unwrap();
        let mut request = Request::new(request);
        request
            .metadata_mut()
            .insert("authorization", format!("Bearer {token}").parse().unwrap());
        request
    }

    fn valid_request(&self, request: HttpRequestEvaluation) -> Request<HttpRequestEvaluation> {
        self.request(request, self.claims())
    }
}

/// Control-plane discovery and configuration reject unauthenticated or unknown callers.
#[tokio::test]
async fn control_rpcs_enforce_authentication_and_caller_profiles() {
    let identity = Identity::new();
    let foreign = Identity::new();
    let model = Model::new(&[]);
    let service = ReviewService::new(model.clone()).with_extension_auth(identity.auth.clone());
    let describe = || MiddlewareDescribeRequest {
        gateway: Some(metadata()),
    };
    let validate = || ValidateConfigRequest {
        config: input().config,
        middleware_name: "nomos-request-review".into(),
    };
    for scenario in ["missing", "foreign", "unknown"] {
        let mut claims = identity.claims();
        claims["caller_kind"] = json!("unknown");
        let (discovery, validation, expected) = match scenario {
            "missing" => (
                Request::new(describe()),
                Request::new(validate()),
                Code::Unauthenticated,
            ),
            "foreign" => (
                foreign.request(describe(), foreign.claims()),
                foreign.request(validate(), foreign.claims()),
                Code::Unauthenticated,
            ),
            _ => (
                identity.request(describe(), claims.clone()),
                identity.request(validate(), claims),
                Code::PermissionDenied,
            ),
        };
        assert_eq!(
            service.describe(discovery).await.unwrap_err().code(),
            expected,
            "Describe: {scenario}"
        );
        assert_eq!(
            service
                .validate_config(validation)
                .await
                .unwrap_err()
                .code(),
            expected,
            "ValidateConfig: {scenario}"
        );
    }
    assert!(model.states().is_empty());
}

/// Both native OpenShell caller profiles may discover and validate this exact audience.
#[tokio::test]
async fn control_rpcs_accept_gateway_and_supervisor_and_advertise_pinned_audience() {
    let identity = Identity::new();
    let model = Model::new(&[]);
    let service = ReviewService::new(model.clone()).with_extension_auth(identity.auth.clone());
    for kind in ["gateway", "supervisor"] {
        let mut claims = identity.claims();
        if kind == "gateway" {
            claims["caller_kind"] = json!("gateway");
            claims["sub"] = json!("openshell-gateway:pilot-gateway");
            claims.as_object_mut().unwrap().remove("sandbox_id");
        }
        let manifest = service
            .describe(identity.request(
                MiddlewareDescribeRequest {
                    gateway: Some(metadata()),
                },
                claims.clone(),
            ))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(manifest.expected_audience, "urn:nomos:test", "{kind}");
        let validation = service
            .validate_config(identity.request(
                ValidateConfigRequest {
                    config: input().config,
                    middleware_name: "nomos-request-review".into(),
                },
                claims,
            ))
            .await
            .unwrap()
            .into_inner();
        assert!(validation.valid, "{kind}: {}", validation.reason);
    }
    assert!(model.states().is_empty());
}

#[derive(Clone, Copy)]
enum Outcome {
    Score(f64),
    OverLimit,
    Unavailable,
}

struct Model {
    states: Mutex<Vec<Value>>,
    catalogs: Mutex<Vec<Value>>,
    outcomes: Mutex<VecDeque<Outcome>>,
}

impl Model {
    fn new(outcomes: &[Outcome]) -> Arc<Self> {
        Arc::new(Self {
            states: Mutex::new(Vec::new()),
            catalogs: Mutex::new(Vec::new()),
            outcomes: Mutex::new(outcomes.iter().copied().collect()),
        })
    }

    fn states(&self) -> Vec<Value> {
        self.states.lock().unwrap().clone()
    }
}

#[async_trait]
impl DecisionProvider for Model {
    async fn assess(&self, state: Value, catalog: &Catalog) -> Result<Assessment, ProviderError> {
        self.states.lock().unwrap().push(state);
        self.catalogs
            .lock()
            .unwrap()
            .push(serde_json::to_value(catalog).unwrap());
        let next = self
            .outcomes
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or(Outcome::Score(0.1));
        match next {
            Outcome::OverLimit => Err(ProviderError::OverLimit),
            Outcome::Unavailable => Err(ProviderError::Unavailable),
            Outcome::Score(score) => Ok(Assessment {
                model: "local-test-model".into(),
                scores: catalog
                    .rules
                    .iter()
                    .map(|rule| (rule.id.clone(), score))
                    .collect(),
            }),
        }
    }
}

#[derive(Clone)]
struct Reply {
    status: StatusCode,
    body: Vec<u8>,
    delay: Duration,
    requests: Arc<Mutex<Vec<Value>>>,
}

struct ContextServer {
    endpoint: String,
    requests: Arc<Mutex<Vec<Value>>>,
    task: tokio::task::JoinHandle<()>,
}

impl Drop for ContextServer {
    fn drop(&mut self) {
        self.task.abort();
    }
}

impl ContextServer {
    async fn new(status: StatusCode, body: Vec<u8>, delay: Duration) -> Self {
        let requests = Arc::new(Mutex::new(Vec::new()));
        let reply = Reply {
            status,
            body,
            delay,
            requests: requests.clone(),
        };
        async fn handle(
            State(reply): State<Reply>,
            headers: HeaderMap,
            bytes: Bytes,
        ) -> Response<Body> {
            assert_eq!(headers["authorization"], format!("Bearer {TOKEN}"));
            let request: Value = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(request["sandbox_name"], NAME);
            assert_eq!(request["sandbox_id"], "sandbox-A");
            assert_eq!(request["http_request_id"], "req-A");
            reply.requests.lock().unwrap().push(request);
            tokio::time::sleep(reply.delay).await;
            Response::builder()
                .status(reply.status)
                .body(Body::from(reply.body))
                .unwrap()
        }
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = format!("http://{}", listener.local_addr().unwrap());
        let app = Router::new()
            .route("/v1/context-lookups", post(handle))
            .with_state(reply);
        let task = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        Self {
            endpoint,
            requests,
            task,
        }
    }

    async fn content(value: Value) -> Self {
        Self::new(
            StatusCode::OK,
            serde_json::to_vec(&value).unwrap(),
            Duration::ZERO,
        )
        .await
    }

    fn captured(&self) -> Vec<Value> {
        self.requests.lock().unwrap().clone()
    }
}

fn deadline(offset: i64) -> String {
    (OffsetDateTime::now_utc() + time::Duration::seconds(offset))
        .format(&Rfc3339)
        .unwrap()
}

fn event(id: &str, parent: &str) -> Value {
    json!({"sandbox_name":NAME,"record_id":id,"snapshot_id":parent,
        "tool_call_id":"provider-call-B","tool_name":"terminal","state":"completed",
        "arguments":{"command":"printf fictional"},"arguments_incomplete":false,
        "council_review":{"status":"approved","decision_id":"intent-example","guidance_truncated":false}})
}

fn fixture(content: bool) -> Value {
    let expiry = deadline(240);
    let mut value = json!({
        "sandbox_name":NAME,"association":"name_based_pilot","snapshot_ref":"snapshot-current",
        "snapshot_revision":3,"snapshot_expires_at":expiry,"recent_tool_record_refs":["event-B"],
        "tool_record_expires_at":{"event-B":expiry},"tool_records_truncated":false,
        "exact_tool_call_id_for_http_request":null,"capture_coverage":"managed_relay_calls_only",
        "content_trust":"agent_reported_workload_mutable","source_content_omitted":!content,
    });
    if content {
        value["snapshot"] = json!({"sandbox_name":NAME,"snapshot_id":"snapshot-current",
            "model_attempt_id":"attempt-current","model_name":"agent-model","not_after":expiry,
            "context":{"latest_user_request":"Publish the fictional workshop announcement.","user_request_truncated":false}});
        value["tool_records"] = json!([event("event-B", "snapshot-current")]);
    }
    value
}

fn proto(value: Value) -> prost_types::Value {
    use prost_types::value::Kind;
    let kind = match value {
        Value::Null => Kind::NullValue(0),
        Value::Bool(value) => Kind::BoolValue(value),
        Value::Number(value) => Kind::NumberValue(value.as_f64().unwrap()),
        Value::String(value) => Kind::StringValue(value),
        Value::Array(values) => Kind::ListValue(prost_types::ListValue {
            values: values.into_iter().map(proto).collect(),
        }),
        Value::Object(values) => Kind::StructValue(prost_types::Struct {
            fields: values
                .into_iter()
                .map(|(key, value)| (key, proto(value)))
                .collect(),
        }),
    };
    prost_types::Value { kind: Some(kind) }
}

fn input() -> HttpRequestEvaluation {
    let prost_types::value::Kind::StructValue(config)=proto(json!({
        "catalog_version":"context-test","rules":[{"id":"disclosure","question":"Does this disclose confidential information?","threshold":0.7}],
    })).kind.unwrap() else {unreachable!()};
    HttpRequestEvaluation {
        phase: SupervisorMiddlewarePhase::PreCredentials as i32,
        context: Some(RequestContext {
            request_id: "req-A".into(),
            sandbox_id: "sandbox-A".into(),
            sandbox: NAME.into(),
            workspace: "pilot".into(),
            ..Default::default()
        }),
        config: Some(config),
        target: Some(HttpRequestTarget {
            scheme: "https".into(),
            host: "destination.example".into(),
            port: 443,
            method: "POST".into(),
            path: "/publish".into(),
            query: String::new(),
        }),
        headers: vec![HttpHeader {
            name: "content-type".into(),
            value: "application/json".into(),
        }],
        body: br#"{ "message": "Fictional community workshop" }"#.to_vec(),
        middleware_name: "nomos-request-review".into(),
    }
}

fn reader(endpoint: &str, mode: LookupMode, timeout: Duration) -> Arc<ContextReader> {
    Arc::new(
        ContextReader::new(ContextReaderOptions {
            mode,
            endpoint: endpoint.into(),
            token: TOKEN.into(),
            timeout,
            ca_pem: None,
            allow_loopback_http: true,
        })
        .unwrap(),
    )
}

fn service(
    model: Arc<Model>,
    identity: &Identity,
    server: &ContextServer,
    mode: LookupMode,
) -> ReviewService {
    ReviewService::with_limits(model, Timeouts::default(), DEFAULT_MAX_BODY_BYTES)
        .with_extension_auth(identity.auth.clone())
        .with_context_reader(
            reader(&server.endpoint, mode, Duration::from_millis(150)),
            BudgetLimits::default(),
            vec!["pilot".into()],
        )
        .unwrap()
}

/// Valid supervisor identity enriches the model without rewriting HTTP or questions.
#[tokio::test]
async fn successful_lookup_preserves_http_and_policy_and_adds_nested_context() {
    let identity = Identity::new();
    let server = ContextServer::content(fixture(true)).await;
    let model = Model::new(&[]);
    let service = service(model.clone(), &identity, &server, LookupMode::BestEffort);
    let response = service
        .evaluate_http_request(identity.valid_request(input()))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(response.decision, Decision::Allow as i32);
    assert!(!response.has_body && response.body.is_empty() && response.header_mutations.is_empty());
    let states = model.states();
    let state = &states[0];
    assert_eq!(
        state["body"],
        json!({"message":"Fictional community workshop"})
    );
    assert_eq!(state["method"], "POST");
    assert_eq!(state["host"], "destination.example");
    let context = &state["conversation_context"];
    assert_eq!(
        context["retained_context"]["latest_user_request"],
        "Publish the fictional workshop announcement."
    );
    assert_eq!(
        context["recent_tool_records"][0]["council_review"]["decision_id"],
        "intent-example"
    );
    assert_eq!(context["context_incomplete"], false);
    assert_eq!(context["publication_completeness"], "not_attested");
    assert_eq!(server.captured()[0]["include_content"], true);
    let catalog = &model.catalogs.lock().unwrap()[0];
    assert_eq!(
        catalog["rules"][0]["question"],
        "Does this disclose confidential information?"
    );
    assert_eq!(catalog["rules"][0]["threshold"], 0.7);
}

/// Best-effort mode permits one HTTP-only reassessment only for enriched-size rejection.
#[tokio::test]
async fn enriched_over_limit_reassesses_unchanged_http_only_exactly_once() {
    let identity = Identity::new();
    let server = ContextServer::content(fixture(true)).await;
    let model = Model::new(&[Outcome::OverLimit, Outcome::Score(0.1)]);
    let service = service(model.clone(), &identity, &server, LookupMode::BestEffort);
    assert!(
        service
            .evaluate_http_request(identity.valid_request(input()))
            .await
            .is_ok()
    );
    let states = model.states();
    assert_eq!(states.len(), 2);
    assert!(states[0].get("conversation_context").is_some());
    let mut original = states[0].clone();
    original
        .as_object_mut()
        .unwrap()
        .remove("conversation_context");
    assert_eq!(states[1], original);
    let catalogs = model.catalogs.lock().unwrap();
    assert_eq!(catalogs[0], catalogs[1]);
    assert_eq!(server.captured().len(), 1);
}

/// Generic model failures cannot silently replace the assessment with a second input.
#[tokio::test]
async fn unavailable_model_does_not_trigger_http_only_reassessment() {
    let identity = Identity::new();
    let server = ContextServer::content(fixture(true)).await;
    let model = Model::new(&[Outcome::Unavailable, Outcome::Score(0.1)]);
    let service = service(model.clone(), &identity, &server, LookupMode::BestEffort);
    assert_eq!(
        service
            .evaluate_http_request(identity.valid_request(input()))
            .await
            .unwrap_err()
            .code(),
        Code::Unavailable
    );
    assert_eq!(model.states().len(), 1);
}

/// A valid denial is the final reviewer outcome, never an invitation to omit context.
#[tokio::test]
async fn valid_deny_is_not_reassessed_without_context() {
    let identity = Identity::new();
    let server = ContextServer::content(fixture(true)).await;
    let model = Model::new(&[Outcome::Score(0.9), Outcome::Score(0.1)]);
    let service = service(model.clone(), &identity, &server, LookupMode::BestEffort);
    assert_eq!(
        service
            .evaluate_http_request(identity.valid_request(input()))
            .await
            .unwrap()
            .into_inner()
            .decision,
        Decision::Deny as i32
    );
    assert_eq!(model.states().len(), 1);
}

/// Required context cannot be replaced by an HTTP-only retry after size rejection.
#[tokio::test]
async fn required_mode_over_limit_has_no_fallback_model_call() {
    let identity = Identity::new();
    let server = ContextServer::content(fixture(true)).await;
    let model = Model::new(&[Outcome::OverLimit, Outcome::Score(0.1)]);
    let service = service(model.clone(), &identity, &server, LookupMode::Required);
    assert_eq!(
        service
            .evaluate_http_request(identity.valid_request(input()))
            .await
            .unwrap_err()
            .code(),
        Code::ResourceExhausted
    );
    assert_eq!(model.states().len(), 1);
}

/// Missing or failed optional context leaves the original model observation intact.
#[tokio::test]
async fn best_effort_lookup_errors_do_not_skip_the_http_assessment() {
    for (status, body, delay) in [
        (StatusCode::NOT_FOUND, Vec::new(), Duration::ZERO),
        (
            StatusCode::SERVICE_UNAVAILABLE,
            b"do not expose this error".to_vec(),
            Duration::ZERO,
        ),
        (
            StatusCode::OK,
            vec![b'x'; MAX_LOOKUP_RESPONSE_BYTES + 1],
            Duration::ZERO,
        ),
        (StatusCode::OK, b"not-json".to_vec(), Duration::ZERO),
        (
            StatusCode::OK,
            serde_json::to_vec(&fixture(true)).unwrap(),
            Duration::from_millis(250),
        ),
    ] {
        let identity = Identity::new();
        let server = ContextServer::new(status, body, delay).await;
        let model = Model::new(&[]);
        let service = service(model.clone(), &identity, &server, LookupMode::BestEffort);
        assert!(
            service
                .evaluate_http_request(identity.valid_request(input()))
                .await
                .is_ok()
        );
        let states = model.states();
        assert_eq!(states.len(), 1);
        assert!(states[0].get("conversation_context").is_none());
        assert_eq!(
            states[0]["body"],
            json!({"message":"Fictional community workshop"})
        );
    }
}

/// An unreachable context server falls back without granting or skipping HTTP review.
#[tokio::test]
async fn closed_context_endpoint_preserves_http_only_assessment() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("http://{}", listener.local_addr().unwrap());
    drop(listener);
    let identity = Identity::new();
    let model = Model::new(&[]);
    let service = ReviewService::new(model.clone())
        .with_extension_auth(identity.auth.clone())
        .with_context_reader(
            reader(&endpoint, LookupMode::BestEffort, Duration::from_millis(50)),
            BudgetLimits::default(),
            vec!["pilot".into()],
        )
        .unwrap();
    assert!(
        service
            .evaluate_http_request(identity.valid_request(input()))
            .await
            .is_ok()
    );
    let states = model.states();
    assert_eq!(states.len(), 1);
    assert!(states[0].get("conversation_context").is_none());
    assert_eq!(
        states[0]["body"],
        json!({"message":"Fictional community workshop"})
    );
}

/// Required lookup failure is an assessment error before any model invocation.
#[tokio::test]
async fn required_missing_context_never_calls_model() {
    let identity = Identity::new();
    let server = ContextServer::new(StatusCode::NOT_FOUND, Vec::new(), Duration::ZERO).await;
    let model = Model::new(&[]);
    let service = service(model.clone(), &identity, &server, LookupMode::Required);
    assert_eq!(
        service
            .evaluate_http_request(identity.valid_request(input()))
            .await
            .unwrap_err()
            .code(),
        Code::Unavailable
    );
    assert!(model.states().is_empty());
}

/// Audit requests metadata only and never places context content into model state.
#[tokio::test]
async fn audit_mode_looks_up_metadata_but_runs_http_only_model_review() {
    let identity = Identity::new();
    let server = ContextServer::content(fixture(false)).await;
    let model = Model::new(&[]);
    let service = service(model.clone(), &identity, &server, LookupMode::Audit);
    assert!(
        service
            .evaluate_http_request(identity.valid_request(input()))
            .await
            .is_ok()
    );
    assert_eq!(server.captured()[0]["include_content"], false);
    assert!(model.states()[0].get("conversation_context").is_none());
}

/// Authentication and deployment-scope errors reject before contacting context or model.
#[tokio::test]
async fn invalid_supervisor_or_workspace_cannot_retrieve_context() {
    let identity = Identity::new();
    let server = ContextServer::content(fixture(true)).await;
    let model = Model::new(&[]);
    let service = service(model.clone(), &identity, &server, LookupMode::BestEffort);
    assert_eq!(
        service
            .evaluate_http_request(Request::new(input()))
            .await
            .unwrap_err()
            .code(),
        Code::Unauthenticated
    );
    let other_identity = Identity::new();
    assert_eq!(
        service
            .evaluate_http_request(other_identity.valid_request(input()))
            .await
            .unwrap_err()
            .code(),
        Code::Unauthenticated
    );
    let mut other = input();
    other.context.as_mut().unwrap().sandbox_id = "sandbox-other".into();
    assert_eq!(
        service
            .evaluate_http_request(identity.valid_request(other))
            .await
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
    let mut claims = identity.claims();
    claims["caller_kind"] = json!("gateway");
    assert_eq!(
        service
            .evaluate_http_request(identity.request(input(), claims))
            .await
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
    let mut request = input();
    request.context.as_mut().unwrap().workspace = "other-workspace".into();
    assert_eq!(
        service
            .evaluate_http_request(identity.valid_request(request))
            .await
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
    assert!(model.states().is_empty());
    assert!(server.captured().is_empty());
}

/// An already HTTP-only oversized model input gets no pointless second assessment.
#[tokio::test]
async fn http_only_over_limit_is_not_retried() {
    let identity = Identity::new();
    let server = ContextServer::new(StatusCode::NOT_FOUND, Vec::new(), Duration::ZERO).await;
    let model = Model::new(&[Outcome::OverLimit, Outcome::Score(0.1)]);
    let service = service(model.clone(), &identity, &server, LookupMode::BestEffort);
    assert_eq!(
        service
            .evaluate_http_request(identity.valid_request(input()))
            .await
            .unwrap_err()
            .code(),
        Code::ResourceExhausted
    );
    assert_eq!(model.states().len(), 1);
}

/// A base body can consume all estimated headroom without being truncated or rejected.
#[tokio::test]
async fn insufficient_context_headroom_preserves_complete_large_http_body() {
    let identity = Identity::new();
    let server = ContextServer::content(fixture(true)).await;
    let model = Model::new(&[]);
    let service = service(model.clone(), &identity, &server, LookupMode::BestEffort);
    let text = "x".repeat(40_000);
    let body = json!({"message":text});
    let mut request = input();
    request.body = serde_json::to_vec(&body).unwrap();
    assert!(
        service
            .evaluate_http_request(identity.valid_request(request))
            .await
            .is_ok()
    );
    let states = model.states();
    assert_eq!(states[0]["body"], body);
    assert!(states[0].get("conversation_context").is_none());
}

/// Whole newest records win over older states when context cannot hold the full history.
#[tokio::test]
async fn context_budget_omits_whole_records_and_tells_model_what_is_missing() {
    let identity = Identity::new();
    let mut value = fixture(true);
    let mut completed = event("event-completed", "snapshot-current");
    completed["arguments"] = json!({"content":"x".repeat(16_000)});
    let mut pending = event("event-pending", "snapshot-current");
    pending["state"] = json!("execution_pending");
    pending["arguments"] = json!({"content":"y".repeat(16_000)});
    value["recent_tool_record_refs"] = json!(["event-completed", "event-pending"]);
    value["tool_records"] = json!([completed.clone(), pending]);
    value["tool_record_expires_at"] =
        json!({"event-completed":deadline(200),"event-pending":deadline(200)});
    let server = ContextServer::content(value).await;
    let model = Model::new(&[]);
    let service = service(model.clone(), &identity, &server, LookupMode::BestEffort);
    assert!(
        service
            .evaluate_http_request(identity.valid_request(input()))
            .await
            .is_ok()
    );
    let states = model.states();
    let context = &states[0]["conversation_context"];
    assert!(serde_json::to_vec(context).unwrap().len() <= MAX_CONTEXT_BYTES);
    assert_eq!(context["recent_tool_records"], json!([completed]));
    assert_eq!(context["omitted_returned_records"], 1);
    assert_eq!(context["context_incomplete"], true);
}

/// Earlier-parent tools require their own still-live deadline, not the current parent's expiry.
#[tokio::test]
async fn older_parent_tool_expiry_is_checked_before_model_input() {
    let identity = Identity::new();
    let mut value = fixture(true);
    let mut old = event("event-old", "snapshot-previous");
    old["state"] = json!("denied");
    value["recent_tool_record_refs"] = json!(["event-old", "event-B"]);
    value["tool_records"] = json!([old, event("event-B", "snapshot-current")]);
    value["tool_record_expires_at"] = json!({"event-old":deadline(-1),"event-B":deadline(200)});
    let server = ContextServer::content(value).await;
    let model = Model::new(&[]);
    let service = service(model.clone(), &identity, &server, LookupMode::BestEffort);
    assert!(
        service
            .evaluate_http_request(identity.valid_request(input()))
            .await
            .is_ok()
    );
    let states = model.states();
    let context = &states[0]["conversation_context"];
    assert_eq!(context["recent_tool_records"].as_array().unwrap().len(), 1);
    assert_eq!(context["recent_tool_records"][0]["record_id"], "event-B");
    assert_eq!(context["omitted_returned_records"], 1);
}

/// Disabled context preserves the existing unauthenticated isolated-lab assessment path.
#[tokio::test]
async fn disabled_context_retains_legacy_http_only_behavior() {
    let model = Model::new(&[]);
    let service = ReviewService::new(model.clone());
    assert!(
        service
            .evaluate_http_request(Request::new(input()))
            .await
            .is_ok()
    );
    assert!(model.states()[0].get("conversation_context").is_none());
}

/// Enabling context requires pinned authentication and an explicit workspace scope.
#[test]
fn context_configuration_cannot_bypass_authentication_or_workspace_validation() {
    let identity = Identity::new();
    let model = Model::new(&[]);
    let reader = reader(
        "http://127.0.0.1:1",
        LookupMode::BestEffort,
        Duration::from_millis(50),
    );
    assert!(
        ReviewService::new(model.clone())
            .with_context_reader(
                reader.clone(),
                BudgetLimits::default(),
                vec!["pilot".into()]
            )
            .is_err()
    );
    assert!(
        ReviewService::new(model)
            .with_extension_auth(identity.auth)
            .with_context_reader(reader, BudgetLimits::default(), Vec::new())
            .is_err()
    );
}
