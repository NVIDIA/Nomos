// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! Explicit isolated-lab defaults, with authenticated TLS required for context lookup.
use clap::Parser;
use nomos_request_review::{
    DEFAULT_MAX_BODY_BYTES, DEFAULT_MAX_GRPC_MESSAGE_BYTES,
    context_budget::BudgetLimits,
    context_reader::{ContextReader, ContextReaderOptions, LookupMode},
    extension_auth::ExtensionAuth,
    openshell::middleware::v1::supervisor_middleware_server::SupervisorMiddlewareServer,
    provider::SystemOneProvider,
    service::ReviewService,
    timeouts::{DEFAULT_EVALUATION_TIMEOUT_MS, DEFAULT_MODEL_TIMEOUT_MS, Timeouts},
};
use std::{
    fs::File,
    io::Read,
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tonic::transport::{Identity, ServerTlsConfig};

#[derive(Parser)]
#[command(
    version,
    about = "Experimental OpenShell disclosure-review middleware with optional authenticated context lookup"
)]
struct Args {
    /// gRPC listener; remote exposure needs authenticated TLS or explicit lab opt-in.
    #[arg(long, default_value = "127.0.0.1:50051")]
    bind: SocketAddr,
    /// Permit unauthenticated nonloopback lab calls; never bypasses context-reader authentication.
    #[arg(long)]
    allow_insecure_lab: bool,
    /// Complete native SystemOne endpoint URL; never supplied by sandbox policy.
    #[arg(long, env = "NOMOS_MODEL_ENDPOINT")]
    model_endpoint: String,
    /// Request model alias; may differ from the returned versioned model name.
    #[arg(long, env = "NOMOS_MODEL_ID")]
    model_id: String,
    /// Exact required response model name. Defaults to the request model alias.
    #[arg(long, env = "NOMOS_EXPECTED_MODEL_ID")]
    expected_model_id: Option<String>,
    /// Explicitly permit plaintext inference transport in an isolated lab.
    #[arg(long)]
    allow_http_model: bool,
    /// Total model HTTP budget in ms; shorter than the whole-evaluation budget.
    #[arg(long, env = "NOMOS_MODEL_TIMEOUT_MS", default_value_t = DEFAULT_MODEL_TIMEOUT_MS,
          value_parser = clap::value_parser!(u64).range(1..=30_000))]
    model_timeout_ms: u64,
    /// Whole-assessment budget in ms; keep below OpenShell's separately configured RPC timeout.
    #[arg(long, env = "NOMOS_EVALUATION_TIMEOUT_MS", default_value_t = DEFAULT_EVALUATION_TIMEOUT_MS,
          value_parser = clap::value_parser!(u64).range(1..=30_000))]
    evaluation_timeout_ms: u64,
    /// Original HTTP body byte cap; advertised by Describe, not a model token allowance.
    #[arg(long, default_value_t = DEFAULT_MAX_BODY_BYTES,
          value_parser = clap::value_parser!(u32).range(1..))]
    max_body_bytes: u32,
    /// Incoming/outgoing protobuf cap; defaults to 512 KiB (wire ceiling: 4,294,967,295).
    #[arg(long, default_value_t = DEFAULT_MAX_GRPC_MESSAGE_BYTES,
          value_parser = clap::value_parser!(u32).range(1..))]
    max_grpc_message_bytes: u32,
    /// Pinned public gateway Ed25519 JWKS; no online key discovery.
    #[arg(long)]
    extension_jwks_file: Option<PathBuf>,
    /// Expected gateway ID, used as issuer openshell-gateway:<gateway_id>.
    #[arg(long)]
    extension_gateway_id: Option<String>,
    /// Exact extension audience, also advertised by Describe.
    #[arg(long)]
    extension_audience: Option<String>,
    /// Native gRPC server certificate chain PEM; required with extension authentication.
    #[arg(long)]
    tls_cert_file: Option<PathBuf>,
    /// Native gRPC server private-key PEM; never supplied on the command line.
    #[arg(long)]
    tls_key_file: Option<PathBuf>,
    /// Context lookup is independent of Relay publication and disabled by default.
    #[arg(long, value_enum, default_value = "disabled")]
    context_lookup_mode: LookupMode,
    /// Context-service server-root URL; required only when lookup is enabled.
    #[arg(long)]
    context_service_url: Option<String>,
    /// File containing the context service's distinct read-only bearer token.
    #[arg(long)]
    context_service_token_file: Option<PathBuf>,
    /// Optional additional context-service trust-anchor PEM; peer/hostname verification stays enabled.
    #[arg(long)]
    context_ca_cert_file: Option<PathBuf>,
    /// Total context lookup deadline; must leave the full model budget inside evaluation.
    #[arg(long, default_value_t = 250, value_parser = clap::value_parser!(u64).range(1..=5_000))]
    context_lookup_timeout_ms: u64,
    /// Explicit development exception allowing only loopback HTTP to the context service.
    #[arg(long)]
    allow_http_context: bool,
    /// Allowed OpenShell workspace name; repeat for each permitted workspace.
    #[arg(long = "context-allowed-workspace")]
    context_allowed_workspaces: Vec<String>,
    /// Model state-plus-largest-question window; Jev standard default, not tokenizer-derived admission.
    #[arg(long, default_value_t = 32_000, value_parser = clap::value_parser!(u32).range(1..))]
    context_model_per_question_tokens: u32,
    /// Model state-plus-all-questions window; configure for the selected model, not its alias spelling.
    #[arg(long, default_value_t = 64_000, value_parser = clap::value_parser!(u32).range(1..))]
    context_model_per_request_tokens: u32,
    /// Reserve for framing and estimation uncertainty; local estimates are not actual token counts.
    #[arg(long, default_value_t = 2_048, value_parser = clap::value_parser!(u32).range(1..))]
    context_model_safety_margin_tokens: u32,
}

struct RuntimeSettings {
    timeouts: Timeouts,
    auth: Option<Arc<ExtensionAuth>>,
    reader: Option<Arc<ContextReader>>,
    budget: BudgetLimits,
    allowed_workspaces: Vec<String>,
    tls: Option<ServerTlsConfig>,
}

/// Read only regular configured files with a streaming cap and sanitized errors.
/// Symlinks remain usable for ordinary projected Secret/certificate mounts.
fn read_bounded(path: &Path, limit: usize, reason: &'static str) -> Result<Vec<u8>, &'static str> {
    let metadata = std::fs::metadata(path).map_err(|_| reason)?;
    if !metadata.is_file() || metadata.len() > limit as u64 {
        return Err(reason);
    }
    let file = File::open(path).map_err(|_| reason)?;
    if !file.metadata().map_err(|_| reason)?.is_file() {
        return Err(reason);
    }
    let mut bytes = Vec::new();
    file.take(limit as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| reason)?;
    if bytes.len() > limit {
        return Err(reason);
    }
    Ok(bytes)
}

impl Args {
    /// Validate settings before file access; disabled context never reads its token/CA files.
    fn runtime_settings(&self) -> Result<RuntimeSettings, &'static str> {
        if self.max_body_bytes >= self.max_grpc_message_bytes {
            return Err("body cap must be smaller than the complete gRPC message cap");
        }
        let timeouts = Timeouts::from_millis(self.model_timeout_ms, self.evaluation_timeout_ms)?;
        let auth_parts = [
            self.extension_jwks_file.is_some(),
            self.extension_gateway_id.is_some(),
            self.extension_audience.is_some(),
        ];
        let has_auth = auth_parts.iter().all(|present| *present);
        if auth_parts.iter().any(|present| *present) && !has_auth {
            return Err("extension authentication requires JWKS file, gateway ID and audience");
        }
        let has_tls = self.tls_cert_file.is_some() && self.tls_key_file.is_some();
        if self.tls_cert_file.is_some() != self.tls_key_file.is_some() {
            return Err("inbound TLS requires both certificate and key files");
        }
        if has_auth && !has_tls {
            return Err("extension authentication requires native inbound TLS");
        }
        let enabled = self.context_lookup_mode != LookupMode::Disabled;
        if enabled {
            if !has_auth || !has_tls {
                return Err(
                    "context lookup requires pinned extension authentication and native inbound TLS",
                );
            }
            if self.context_service_url.is_none() || self.context_service_token_file.is_none() {
                return Err("context lookup requires a service URL and reader token file");
            }
            if self.context_allowed_workspaces.is_empty()
                || self.context_allowed_workspaces.iter().any(|name| {
                    name.trim().is_empty() || name.len() > 256 || name.chars().any(char::is_control)
                })
            {
                return Err("context lookup requires explicit nonempty allowed workspace names");
            }
            let gap = timeouts.evaluation().saturating_sub(timeouts.model());
            if Duration::from_millis(self.context_lookup_timeout_ms) >= gap {
                return Err(
                    "context lookup deadline must be shorter than evaluation minus model timeout",
                );
            }
        }
        if !(self.bind.ip().is_loopback() || self.allow_insecure_lab || has_auth && has_tls) {
            return Err("nonloopback listener requires --allow-insecure-lab or authenticated TLS");
        }
        let budget = BudgetLimits {
            per_question_tokens: self.context_model_per_question_tokens as usize,
            per_request_tokens: self.context_model_per_request_tokens as usize,
            safety_margin_tokens: self.context_model_safety_margin_tokens as usize,
        };
        if enabled {
            budget.validate()?;
        }
        let tls = if has_tls {
            let cert = read_bounded(
                self.tls_cert_file.as_ref().unwrap(),
                1024 * 1024,
                "inbound TLS certificate could not be read",
            )?;
            let key = read_bounded(
                self.tls_key_file.as_ref().unwrap(),
                1024 * 1024,
                "inbound TLS key could not be read",
            )?;
            Some(ServerTlsConfig::new().identity(Identity::from_pem(cert, key)))
        } else {
            None
        };
        let auth = if has_auth {
            let bytes = read_bounded(
                self.extension_jwks_file.as_ref().unwrap(),
                65_536,
                "extension JWKS could not be read",
            )?;
            Some(Arc::new(ExtensionAuth::from_jwks(
                &bytes,
                self.extension_gateway_id.as_deref().unwrap(),
                self.extension_audience.as_deref().unwrap(),
            )?))
        } else {
            None
        };
        let reader = if enabled {
            let mut bytes = read_bounded(
                self.context_service_token_file.as_ref().unwrap(),
                4097,
                "context reader token could not be read",
            )?;
            if bytes.last() == Some(&b'\n') {
                bytes.pop();
            }
            let token = String::from_utf8(bytes).map_err(|_| "invalid context reader token")?;
            let ca_pem = self
                .context_ca_cert_file
                .as_ref()
                .map(|path| read_bounded(path, 1024 * 1024, "context CA could not be read"))
                .transpose()?;
            Some(Arc::new(ContextReader::new(ContextReaderOptions {
                mode: self.context_lookup_mode,
                endpoint: self.context_service_url.clone().unwrap(),
                token,
                timeout: Duration::from_millis(self.context_lookup_timeout_ms),
                ca_pem,
                allow_loopback_http: self.allow_http_context,
            })?))
        } else {
            None
        };
        Ok(RuntimeSettings {
            timeouts,
            auth,
            reader,
            budget,
            allowed_workspaces: if enabled {
                self.context_allowed_workspaces.clone()
            } else {
                Vec::new()
            },
            tls,
        })
    }
}

/// Validate process settings and start the bounded gRPC listener without calling the model.
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = Args::parse();
    let runtime = args.runtime_settings()?;
    let provider = SystemOneProvider::with_timeouts(
        &args.model_endpoint,
        args.model_id.clone(),
        args.expected_model_id
            .clone()
            .unwrap_or_else(|| args.model_id.clone()),
        std::env::var("NOMOS_MODEL_TOKEN").ok(),
        args.allow_http_model,
        runtime.timeouts,
    )?;
    tracing_subscriber::fmt().json().with_target(false).init();
    if runtime.auth.is_none() {
        tracing::warn!(bind=%args.bind, model_timeout_ms=args.model_timeout_ms,
            evaluation_timeout_ms=args.evaluation_timeout_ms,"isolated_lab_no_inbound_authentication");
    } else {
        tracing::info!(bind=%args.bind, context_mode=?args.context_lookup_mode,
            "authenticated_middleware_listener");
    }
    let mut review =
        ReviewService::with_limits(Arc::new(provider), runtime.timeouts, args.max_body_bytes);
    if let Some(auth) = runtime.auth {
        review = review.with_extension_auth(auth);
    }
    if let Some(reader) = runtime.reader {
        review = review.with_context_reader(reader, runtime.budget, runtime.allowed_workspaces)?;
    }
    let service = SupervisorMiddlewareServer::new(review)
        .max_decoding_message_size(args.max_grpc_message_bytes as usize)
        .max_encoding_message_size(args.max_grpc_message_bytes as usize);
    let mut server = tonic::transport::Server::builder();
    if let Some(tls) = runtime.tls {
        server = server
            .tls_config(tls)
            .map_err(|_| "invalid inbound TLS configuration")?;
    }
    server
        .add_service(service)
        .serve_with_shutdown(args.bind, async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Parse test CLI arguments with a fixed endpoint and no model-network activity.
    fn parse(extra: &[&str]) -> Result<Args, clap::Error> {
        Args::try_parse_from(
            [
                "nomos-request-review",
                "--model-endpoint",
                "https://model.example/v1/systemone",
                "--model-id",
                "example-model",
            ]
            .into_iter()
            .chain(extra.iter().copied()),
        )
    }

    /// Disabled context does not require names, credentials, trust files or local reads.
    #[test]
    fn context_disabled_ignores_unused_files_and_keeps_legacy_lab_mode() {
        let args = parse(&[
            "--context-service-token-file",
            "/missing/unused-token",
            "--context-ca-cert-file",
            "/missing/unused-ca",
        ])
        .unwrap();
        let settings = args.runtime_settings().unwrap();
        assert!(settings.reader.is_none());
        assert!(settings.auth.is_none());
    }

    /// Context cannot be enabled through the historical unauthenticated lab escape hatch.
    #[test]
    fn context_requires_authentication_tls_workspace_and_model_headroom() {
        for flags in [
            vec!["--context-lookup-mode", "best_effort"],
            vec![
                "--context-lookup-mode",
                "best_effort",
                "--allow-insecure-lab",
            ],
            vec!["--extension-jwks-file", "/missing/key"],
            vec!["--tls-cert-file", "/missing/certificate"],
        ] {
            assert!(parse(&flags).unwrap().runtime_settings().is_err());
        }
    }

    /// Provide syntactically complete, deliberately unreadable paths to test pre-I/O validation.
    fn enabled_args() -> Args {
        parse(&[
            "--context-lookup-mode",
            "best_effort",
            "--extension-jwks-file",
            "/missing/jwks",
            "--extension-gateway-id",
            "gateway-test",
            "--extension-audience",
            "urn:openshell:extension:middleware:nomos",
            "--tls-cert-file",
            "/missing/certificate",
            "--tls-key-file",
            "/missing/key",
            "--context-service-url",
            "https://context.example",
            "--context-service-token-file",
            "/missing/token",
            "--context-allowed-workspace",
            "default",
        ])
        .unwrap()
    }

    /// Reader preparation cannot consume the model's reserved time or use implicit workspace scope.
    #[test]
    fn context_budget_workspace_and_deadline_checks_precede_file_access() {
        let mut args = enabled_args();
        args.context_allowed_workspaces.clear();
        assert_eq!(
            args.runtime_settings().err().unwrap(),
            "context lookup requires explicit nonempty allowed workspace names"
        );
        let mut args = enabled_args();
        args.context_allowed_workspaces = vec!["  ".into()];
        assert!(args.runtime_settings().err().unwrap().contains("workspace"));
        let mut args = enabled_args();
        args.context_lookup_timeout_ms = 500;
        assert_eq!(
            args.runtime_settings().err().unwrap(),
            "context lookup deadline must be shorter than evaluation minus model timeout"
        );
        let mut args = enabled_args();
        args.context_model_safety_margin_tokens = 32_000;
        assert_eq!(
            args.runtime_settings().err().unwrap(),
            "invalid_context_model_budget"
        );
        let mut args = enabled_args();
        args.context_model_per_request_tokens = 31_999;
        assert_eq!(
            args.runtime_settings().err().unwrap(),
            "invalid_context_model_budget"
        );
        // With valid settings, the next failure is a file read, not a false auth/budget error.
        assert_eq!(
            enabled_args().runtime_settings().err().unwrap(),
            "inbound TLS certificate could not be read"
        );
    }

    /// Native authenticated TLS, not the historical lab flag, permits a remote reader listener.
    #[test]
    fn context_flags_accept_explicit_modes_windows_and_repeated_workspaces() {
        let defaults = parse(&[]).unwrap();
        assert_eq!(defaults.context_lookup_mode, LookupMode::Disabled);
        assert_eq!(defaults.context_lookup_timeout_ms, 250);
        assert_eq!(defaults.context_model_per_question_tokens, 32_000);
        assert_eq!(defaults.context_model_per_request_tokens, 64_000);
        for mode in ["audit", "best_effort", "best-effort", "required"] {
            assert!(parse(&["--context-lookup-mode", mode]).is_ok());
        }
        assert!(parse(&["--context-lookup-mode", "guess"]).is_err());
        let args = parse(&[
            "--context-allowed-workspace",
            "workspace-a",
            "--context-allowed-workspace",
            "workspace-b",
        ])
        .unwrap();
        assert_eq!(
            args.context_allowed_workspaces,
            ["workspace-a", "workspace-b"]
        );
        let mut remote = enabled_args();
        remote.bind = "0.0.0.0:50051".parse().unwrap();
        assert_eq!(
            remote.runtime_settings().err().unwrap(),
            "inbound TLS certificate could not be read"
        );
    }

    /// Token, JWKS and key material must never be returned in local file errors.
    #[test]
    fn bounded_file_reads_limit_memory_and_sanitize_failures() {
        let unique = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path =
            std::env::temp_dir().join(format!("nomos-cli-bounded-{}-{unique}", std::process::id()));
        std::fs::write(&path, b"test-only-private-material").unwrap();
        assert_eq!(
            read_bounded(&path, 4, "configured file could not be read").unwrap_err(),
            "configured file could not be read"
        );
        assert_eq!(
            read_bounded(&path, 128, "configured file could not be read").unwrap(),
            b"test-only-private-material"
        );
        std::fs::remove_file(&path).unwrap();
        assert_eq!(
            read_bounded(&path, 128, "configured file could not be read").unwrap_err(),
            "configured file could not be read"
        );
    }

    /// Keep the enclosing transport allowance larger than the default inspected body.
    #[test]
    fn payload_limits_default_to_512_kib_grpc_and_256_kib_body() {
        assert_eq!(parse(&[]).unwrap().max_grpc_message_bytes, 524_288);
        assert_eq!(parse(&[]).unwrap().max_body_bytes, 262_144);
    }

    /// Accept explicit byte counts through the gRPC framing ceiling at argument parsing.
    #[test]
    fn grpc_message_limit_accepts_explicit_values_through_the_wire_ceiling() {
        for (input, expected) in [
            ("1", 1),
            ("16777216", 16 * 1024 * 1024),
            ("4294967295", u32::MAX),
        ] {
            assert_eq!(
                parse(&["--max-grpc-message-bytes", input])
                    .unwrap()
                    .max_grpc_message_bytes,
                expected,
            );
        }
    }

    /// Reject invalid transport counts before the server can allocate message buffers.
    #[test]
    fn grpc_message_limit_rejects_zero_and_values_above_the_wire_ceiling() {
        for input in ["0", "-1", "4294967296", "18446744073709551615", "invalid"] {
            assert!(parse(&["--max-grpc-message-bytes", input]).is_err());
        }
    }
    /// Check explicit body-size parsing independently of cross-setting startup validation.
    #[test]
    fn body_limit_accepts_explicit_bytes_and_rejects_invalid_counts() {
        assert_eq!(
            parse(&["--max-body-bytes", "65536"])
                .unwrap()
                .max_body_bytes,
            65536
        );
        for input in ["0", "-1", "4294967296", "invalid"] {
            assert!(parse(&["--max-body-bytes", input]).is_err());
        }
    }
}
