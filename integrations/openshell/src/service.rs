// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! OpenShell service implementation. Errors are sanitized and fail closed in policy.
use crate::{
    DEFAULT_MAX_BODY_BYTES,
    config::{Catalog, DenyWhen},
    context_budget::{BudgetLimits, budget_context},
    context_reader::{ContextReader, LookupMode},
    extension_auth::ExtensionAuth,
    openshell::{
        extension::v1::{PeerMetadata, ProtocolVersion},
        middleware::v1::*,
    },
    provider::{DecisionProvider, ProviderError},
    strict_json,
    timeouts::Timeouts,
};
use sha2::{Digest, Sha256};
use std::{
    pin::Pin,
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::Semaphore;
use tonic::{Request, Response, Status};
use tracing::Instrument;

/// Base capability mandated by the pinned supervisor middleware protocol.
pub const CONTRACT: &str = "openshell.supervisor-middleware.contract";

/// Request-only service. A semaphore bounds active work; excess work is rejected.
pub struct ReviewService {
    provider: Arc<dyn DecisionProvider>,
    permits: Arc<Semaphore>,
    // Independent outer budget around the complete admitted assessment.
    evaluation_timeout: Duration,
    max_body_bytes: u32,
    extension_auth: Option<Arc<ExtensionAuth>>,
    context_reader: Option<Arc<ContextReader>>,
    context_budget: BudgetLimits,
    context_workspaces: Vec<String>,
}
impl ReviewService {
    /// Construct with the default 4.5-second deadline, four permits and no queue.
    pub fn new(provider: Arc<dyn DecisionProvider>) -> Self {
        Self::with_timeouts(provider, Timeouts::default())
    }

    /// Construct with a validated evaluation deadline, four permits and no work queue.
    /// Configure the provider with the same `Timeouts` so its HTTP cap is shorter.
    pub fn with_timeouts(provider: Arc<dyn DecisionProvider>, timeouts: Timeouts) -> Self {
        Self::with_limits(provider, timeouts, DEFAULT_MAX_BODY_BYTES)
    }

    /// Construct with an explicit body cap, also advertised to OpenShell by Describe.
    /// The listener must separately enforce its complete protobuf-message cap.
    pub fn with_limits(
        provider: Arc<dyn DecisionProvider>,
        timeouts: Timeouts,
        max_body_bytes: u32,
    ) -> Self {
        Self {
            provider,
            permits: Arc::new(Semaphore::new(4)),
            evaluation_timeout: timeouts.evaluation(),
            max_body_bytes,
            extension_auth: None,
            context_reader: None,
            context_budget: BudgetLimits::default(),
            context_workspaces: Vec::new(),
        }
    }

    /// Authenticate gateway/supervisor calls using operator-pinned extension keys.
    /// The executable must also configure TLS before exposing authenticated calls.
    pub fn with_extension_auth(mut self, auth: Arc<ExtensionAuth>) -> Self {
        self.extension_auth = Some(auth);
        self
    }

    /// Enable explicit context lookup only behind verified supervisor identity.
    /// Workspace values are exact authenticated-supervisor metadata, not name prefixes.
    pub fn with_context_reader(
        mut self,
        reader: Arc<ContextReader>,
        budget: BudgetLimits,
        workspaces: Vec<String>,
    ) -> Result<Self, &'static str> {
        budget.validate()?;
        if self.extension_auth.is_none()
            || reader.mode() == LookupMode::Disabled
            || workspaces.is_empty()
            || workspaces
                .iter()
                .any(|v| v.is_empty() || v.len() > 256 || v.chars().any(char::is_control))
        {
            return Err("context_lookup_requires_authentication_and_explicit_workspaces");
        }
        self.context_reader = Some(reader);
        self.context_budget = budget;
        self.context_workspaces = workspaces;
        Ok(self)
    }

    async fn enrich_context(
        &self,
        request: &HttpRequestEvaluation,
        base: &serde_json::Value,
        catalog: &Catalog,
    ) -> Result<(serde_json::Value, bool), Status> {
        let Some(reader) = &self.context_reader else {
            return Ok((base.clone(), false));
        };
        let context = request
            .context
            .as_ref()
            .expect("observation validated context");
        if !self
            .context_workspaces
            .iter()
            .any(|v| v == &context.workspace)
        {
            return Err(Status::permission_denied("context_workspace_not_allowed"));
        }
        let started = Instant::now();
        match reader
            .lookup(&context.sandbox, &context.sandbox_id, &context.request_id)
            .await
        {
            Ok(lookup) if reader.mode() == LookupMode::Audit => {
                tracing::info!(
                    snapshot_revision = lookup["snapshot_revision"].as_u64(),
                    lookup_ms = started.elapsed().as_millis() as u64,
                    "context_lookup_audit"
                );
                Ok((base.clone(), false))
            }
            Ok(lookup) => {
                let budget = budget_context(base, catalog, &lookup, self.context_budget);
                tracing::info!(
                    included_records = budget.included_records,
                    omitted_returned_records = budget.omitted_returned_records,
                    reason = budget.reason,
                    lookup_ms = started.elapsed().as_millis() as u64,
                    "context_budget_complete"
                );
                match budget.context {
                    Some(context) => {
                        let mut state = base.clone();
                        state
                            .as_object_mut()
                            .expect("observation is object")
                            .insert("conversation_context".into(), context);
                        Ok((state, true))
                    }
                    None if reader.mode() == LookupMode::Required => Err(Status::unavailable(
                        "required_context_unavailable_or_over_budget",
                    )),
                    None => Ok((base.clone(), false)),
                }
            }
            Err(error) => {
                tracing::warn!(reason=%error,lookup_ms=started.elapsed().as_millis() as u64,
                    "context_lookup_failed");
                if reader.mode() == LookupMode::Required {
                    Err(Status::unavailable("required_context_unavailable"))
                } else {
                    Ok((base.clone(), false))
                }
            }
        }
    }
}

/// Metadata advertised during bilateral protocol negotiation.
pub fn metadata() -> PeerMetadata {
    PeerMetadata {
        protocol_version: Some(ProtocolVersion { major: 1, minor: 0 }),
        implementation_name: "nomos-request-review".into(),
        implementation_version: env!("CARGO_PKG_VERSION").into(),
        supported_capabilities: vec![CONTRACT.into()],
        required_capabilities: vec![CONTRACT.into()],
    }
}

fn negotiate(peer: Option<PeerMetadata>) -> Result<(), Status> {
    let peer = peer.ok_or_else(|| Status::failed_precondition("missing_protocol_metadata"))?;
    let valid_text =
        |s: &str| !s.trim().is_empty() && s.len() <= 128 && !s.chars().any(char::is_control);
    let valid_caps = |caps: &Vec<String>| {
        caps.len() <= 128
            && caps.iter().all(|s| valid_text(s))
            && caps.iter().collect::<std::collections::BTreeSet<_>>().len() == caps.len()
    };
    if peer.protocol_version.is_none_or(|v| v.major != 1)
        || !valid_text(&peer.implementation_name)
        || !valid_text(&peer.implementation_version)
        || !valid_caps(&peer.supported_capabilities)
        || !valid_caps(&peer.required_capabilities)
        || !peer.supported_capabilities.iter().any(|c| c == CONTRACT)
        || peer.required_capabilities.iter().any(|c| c != CONTRACT)
    {
        return Err(Status::failed_precondition("incompatible_protocol"));
    }
    Ok(())
}

fn valid_correlation_id(value: &str) -> bool {
    !value.is_empty() && value.len() <= 128 && !value.chars().any(char::is_control)
}

fn log_review_failure(request: &HttpRequestEvaluation, error: &Status, started: Instant) {
    // Context can be absent or invalid on early failures. Log each ID only
    // after applying the same bounds as request validation; never log raw errors.
    let context = request.context.as_ref();
    let request_id = context
        .map(|c| c.request_id.as_str())
        .filter(|id| valid_correlation_id(id));
    let sandbox_id = context
        .map(|c| c.sandbox_id.as_str())
        .filter(|id| valid_correlation_id(id));
    tracing::warn!(request_id, sandbox_id, code=?error.code(),
        total_ms=started.elapsed().as_millis() as u64, "review_failed");
}

/// Validate one observed JSON HTTP request without altering its original bytes.
pub fn observation(
    request: &HttpRequestEvaluation,
    max_body_bytes: u32,
) -> Result<serde_json::Value, Status> {
    if request.phase != SupervisorMiddlewarePhase::PreCredentials as i32 {
        return Err(Status::invalid_argument("unsupported_phase"));
    }
    let context = request
        .context
        .as_ref()
        .ok_or_else(|| Status::invalid_argument("missing_context"))?;
    // IDs are only correlation fields, not authorization claims or model input.
    if [context.request_id.as_str(), context.sandbox_id.as_str()]
        .iter()
        .any(|s| !valid_correlation_id(s))
    {
        return Err(Status::invalid_argument("invalid_context"));
    }
    let target = request
        .target
        .as_ref()
        .ok_or_else(|| Status::invalid_argument("missing_target"))?;
    // OpenShell owns method/path authorization. Check only syntax and bounds
    // here so the same service can inspect different policy-permitted APIs.
    if target.method.len() > 64
        || reqwest::Method::from_bytes(target.method.as_bytes()).is_err()
        || !target.path.starts_with('/')
        || target.path.len() > 8192
        || target
            .path
            .bytes()
            .any(|b| b <= b' ' || b >= 127 || b == b'?' || b == b'#')
        || !["https", "http"].contains(&target.scheme.as_str())
        || target.host.is_empty()
        || target.host.len() > 253
        || !(1..=65535).contains(&target.port)
        || target.query.len() > 2048
        || request.headers.len() > 128
    {
        return Err(Status::invalid_argument("unsupported_request"));
    }
    let mut content_types = Vec::new();
    let mut encodings = Vec::new();
    for h in &request.headers {
        if h.name.eq_ignore_ascii_case("content-type") {
            content_types.push(h.value.trim().to_ascii_lowercase());
        }
        if h.name.eq_ignore_ascii_case("content-encoding") {
            encodings.push(h.value.trim().to_ascii_lowercase());
        }
    }
    if content_types.len() != 1
        || ![
            "application/json",
            "application/json; charset=utf-8",
            "application/json;charset=utf-8",
        ]
        .contains(&content_types[0].as_str())
        || encodings.len() > 1
        || encodings.first().is_some_and(|s| s != "identity")
    {
        return Err(Status::invalid_argument("unsupported_encoding"));
    }
    if request.body.len() > max_body_bytes as usize {
        return Err(Status::resource_exhausted("body_over_limit"));
    }
    let body =
        strict_json::parse(&request.body).map_err(|_| Status::invalid_argument("invalid_json"))?;
    if !body.is_object() {
        return Err(Status::invalid_argument("object_body_required"));
    }
    let state = serde_json::json!({
        "observation_kind":"untrusted_http_request",
        "method":target.method,"scheme":target.scheme,"host":target.host,"port":target.port,
        "path":target.path,"query":target.query,"content_type":content_types[0],"body":body,
    });
    Ok(state)
}

#[tonic::async_trait]
impl supervisor_middleware_server::SupervisorMiddleware for ReviewService {
    type EvaluateWebSocketSessionStream = Pin<
        Box<dyn tokio_stream::Stream<Item = Result<WebSocketSessionEventResult, Status>> + Send>,
    >;

    /// Advertise the configured body limit after validating peer protocol compatibility.
    async fn describe(
        &self,
        request: Request<MiddlewareDescribeRequest>,
    ) -> Result<Response<MiddlewareManifest>, Status> {
        if let Some(auth) = &self.extension_auth {
            auth.authorize_control_rpc(request.metadata())?;
        }
        negotiate(request.into_inner().gateway)?;
        Ok(Response::new(MiddlewareManifest {
            name: "nomos-request-review".into(),
            service_version: env!("CARGO_PKG_VERSION").into(),
            bindings: vec![MiddlewareBinding {
                operation: SupervisorMiddlewareOperation::HttpRequest as i32,
                phase: SupervisorMiddlewarePhase::PreCredentials as i32,
                max_payload_bytes: self.max_body_bytes as u64,
                request_timeout: None,
            }],
            expected_audience: self
                .extension_auth
                .as_ref()
                .map(|auth| auth.audience().to_string())
                .unwrap_or_default(),
            extension: Some(metadata()),
        }))
    }

    async fn validate_config(
        &self,
        request: Request<ValidateConfigRequest>,
    ) -> Result<Response<ValidateConfigResponse>, Status> {
        if let Some(auth) = &self.extension_auth {
            auth.authorize_control_rpc(request.metadata())?;
        }
        let result = Catalog::from_proto(request.into_inner().config);
        Ok(Response::new(ValidateConfigResponse {
            valid: result.is_ok(),
            reason: result.err().unwrap_or_default().into(),
        }))
    }

    /// Bound admitted work, assess validated content and return an unmodified allow or deny.
    async fn evaluate_http_request(
        &self,
        request: Request<HttpRequestEvaluation>,
    ) -> Result<Response<HttpRequestResult>, Status> {
        let started = Instant::now();
        if let Some(auth) = &self.extension_auth {
            let sandbox_id = request
                .get_ref()
                .context
                .as_ref()
                .map(|context| context.sandbox_id.as_str())
                .unwrap_or_default();
            auth.authorize_supervisor(request.metadata(), sandbox_id)?;
        }
        let request = request.into_inner();
        let context = request.context.as_ref();
        let request_id = context
            .map(|c| c.request_id.as_str())
            .filter(|id| valid_correlation_id(id));
        let sandbox_id = context
            .map(|c| c.sandbox_id.as_str())
            .filter(|id| valid_correlation_id(id));
        let span = tracing::info_span!("review", request_id, sandbox_id);
        let _permit = self.permits.try_acquire().map_err(|_| {
            let error = Status::resource_exhausted("capacity_exhausted");
            log_review_failure(&request, &error, started);
            error
        })?;
        // Admission above is immediate. This outer deadline covers policy/input
        // validation through the final decision, including the model await. On
        // expiry, dropping that future cancels pending work and releases _permit
        // when the handler returns. It cannot preempt synchronous CPU work or
        // extend OpenShell's independent caller-side RPC deadline.
        let result = tokio::time::timeout(self.evaluation_timeout,async {
            let catalog = Catalog::from_proto(request.config.clone()).map_err(Status::invalid_argument)?;
            let base_state = observation(&request, self.max_body_bytes)?;
            let (state, enriched) = self.enrich_context(&request, &base_state, &catalog).await?;
            tracing::info!(catalog=%catalog.catalog_version, question_count=catalog.rules.len(),
                body_sha256=%format!("{:x}",Sha256::digest(&request.body)),
                "review_input_validated");
            let model_started=Instant::now();
            let first = self.provider.assess(state,&catalog).await;
            // Only explicit input-size rejection permits one reduced-input assessment.
            // Never retry a valid denial, a generic error, or an HTTP-only rejection.
            let assessed = if enriched
                && self.context_reader.as_ref().is_some_and(|reader|reader.mode()==LookupMode::BestEffort)
                && matches!(&first,Err(ProviderError::OverLimit))
            {
                tracing::warn!(reason="enriched_input_over_limit","context_http_only_reassessment");
                self.provider.assess(base_state,&catalog).await
            } else { first };
            let assessment = assessed.map_err(|error| match error {
                ProviderError::OverLimit => Status::resource_exhausted("model_input_over_limit"),
                _ => Status::unavailable("assessment_unavailable"),
            })?;
            // Defend the provider abstraction too: alternative adapters cannot
            // silently omit a rule or inject an invalid score.
            if assessment.scores.len()!=catalog.rules.len() {return Err(Status::unavailable("invalid_assessment"));}
            let mut matched=Vec::new();
            for rule in &catalog.rules {
                let score=assessment.scores.get(&rule.id).filter(|s|s.is_finite() && (0.0..=1.0).contains(*s))
                    .ok_or_else(||Status::unavailable("invalid_assessment"))?;
                let matches = match rule.deny_when {
                    DenyWhen::Yes => *score >= rule.threshold,
                    DenyWhen::No => *score < rule.threshold,
                };
                if matches {matched.push(rule.id.clone());}
            }
            let deny = !matched.is_empty();
            let ctx=request.context.as_ref().expect("observation validated context");
            tracing::info!(request_id=%ctx.request_id,sandbox_id=%ctx.sandbox_id,catalog=%catalog.catalog_version,
                model=%assessment.model,scores=?assessment.scores,matched_rules=?matched,
                model_ms=model_started.elapsed().as_millis() as u64,total_ms=started.elapsed().as_millis() as u64,
                outcome=if deny {"deny"} else {"allow"},"review_complete");
            Ok(Response::new(HttpRequestResult {decision:if deny {Decision::Deny as i32} else {Decision::Allow as i32},
                reason_code:if deny {"risk_detected".into()} else {String::new()},..Default::default()}))
        }.instrument(span)).await.unwrap_or_else(|_|Err(Status::deadline_exceeded("assessment_timeout")));
        if let Err(error) = &result {
            log_review_failure(&request, error, started);
        }
        result
    }

    async fn evaluate_web_socket_session(
        &self,
        _request: Request<tonic::Streaming<WebSocketSessionEvent>>,
    ) -> Result<Response<Self::EvaluateWebSocketSessionStream>, Status> {
        Err(Status::unimplemented("unsupported_operation"))
    }
}
