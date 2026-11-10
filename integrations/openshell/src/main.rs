// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! Isolated-lab executable. Inbound service authentication is intentionally absent.
use clap::Parser;
use nomos_request_review::{
    DEFAULT_MAX_BODY_BYTES, DEFAULT_MAX_GRPC_MESSAGE_BYTES,
    openshell::middleware::v1::supervisor_middleware_server::SupervisorMiddlewareServer,
    provider::SystemOneProvider,
    service::ReviewService,
    timeouts::{DEFAULT_EVALUATION_TIMEOUT_MS, DEFAULT_MODEL_TIMEOUT_MS, Timeouts},
};
use std::{net::SocketAddr, sync::Arc};

#[derive(Parser)]
#[command(
    version,
    about = "Experimental OpenShell disclosure-review middleware; isolated lab only"
)]
struct Args {
    /// gRPC listener. Nonloopback exposure requires explicit lab opt-in.
    #[arg(long, default_value = "127.0.0.1:50051")]
    bind: SocketAddr,
    /// Permit unauthenticated inbound calls over a nonloopback lab interface.
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
    /// Model HTTP request/response budget in ms (includes network I/O, not just inference).
    /// Must be shorter than the whole-evaluation budget.
    #[arg(long, env = "NOMOS_MODEL_TIMEOUT_MS", default_value_t = DEFAULT_MODEL_TIMEOUT_MS,
          value_parser = clap::value_parser!(u64).range(1..=30_000))]
    model_timeout_ms: u64,
    /// Whole admitted assessment budget in ms: validation, model call, answer and decision.
    /// Keep below OpenShell's separately configured middleware RPC timeout.
    #[arg(long, env = "NOMOS_EVALUATION_TIMEOUT_MS", default_value_t = DEFAULT_EVALUATION_TIMEOUT_MS,
          value_parser = clap::value_parser!(u64).range(1..=30_000))]
    evaluation_timeout_ms: u64,
    /// Cap the original inspected HTTP body, in bytes; advertised by Describe.
    /// This is a byte limit, not a model token allowance.
    #[arg(long, default_value_t = DEFAULT_MAX_BODY_BYTES,
          value_parser = clap::value_parser!(u32).range(1..))]
    max_body_bytes: u32,
    /// Cap each incoming and outgoing protobuf message; defaults to 512 KiB.
    /// Valid range: 1–4,294,967,295 bytes (the uncompressed gRPC wire ceiling).
    #[arg(long, default_value_t = DEFAULT_MAX_GRPC_MESSAGE_BYTES,
          value_parser = clap::value_parser!(u32).range(1..))]
    max_grpc_message_bytes: u32,
}

/// Validate process settings and start the bounded gRPC listener without calling the model.
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = Args::parse();
    if !args.bind.ip().is_loopback() && !args.allow_insecure_lab {
        return Err("nonloopback listener requires --allow-insecure-lab".into());
    }
    if args.max_body_bytes >= args.max_grpc_message_bytes {
        return Err("body cap must be smaller than the complete gRPC message cap".into());
    }
    let timeouts = Timeouts::from_millis(args.model_timeout_ms, args.evaluation_timeout_ms)?;
    let provider = SystemOneProvider::with_timeouts(
        &args.model_endpoint,
        args.model_id.clone(),
        args.expected_model_id.unwrap_or(args.model_id),
        std::env::var("NOMOS_MODEL_TOKEN").ok(),
        args.allow_http_model,
        timeouts,
    )?;
    tracing_subscriber::fmt().json().with_target(false).init();
    tracing::warn!(bind=%args.bind, model_timeout_ms=args.model_timeout_ms,
        evaluation_timeout_ms=args.evaluation_timeout_ms,"isolated_lab_no_inbound_authentication");
    let service = SupervisorMiddlewareServer::new(ReviewService::with_limits(
        Arc::new(provider),
        timeouts,
        args.max_body_bytes,
    ))
    .max_decoding_message_size(args.max_grpc_message_bytes as usize)
    .max_encoding_message_size(args.max_grpc_message_bytes as usize);
    tonic::transport::Server::builder()
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
