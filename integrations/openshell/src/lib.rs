// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! Request-only OpenShell middleware for isolated decision-model experiments.
//!
//! The service never forwards workload traffic or grants network permissions.
//! OpenShell owns enforcement; this crate returns decisions without mutations.
pub mod config;
pub mod context_budget;
pub mod context_reader;
pub mod extension_auth;
pub mod provider;
pub mod service;
pub mod strict_json;
pub mod timeouts;

/// Vendored OpenShell wire types; see `proto/UPSTREAM.md` for provenance.
// Preserve upstream generated message layout; these variants are not used by
// the request-only implementation. Do not suppress lints on handwritten code.
#[allow(missing_docs, clippy::large_enum_variant)]
pub mod openshell {
    pub mod extension {
        pub mod v1 {
            tonic::include_proto!("openshell.extension.v1");
        }
    }
    pub mod middleware {
        pub mod v1 {
            tonic::include_proto!("openshell.middleware.v1");
        }
    }
}

/// Default accepted original request body size; independent of model token limits.
pub const DEFAULT_MAX_BODY_BYTES: u32 = 256 * 1024;
/// Default cap on each incoming and outgoing protobuf message.
pub const DEFAULT_MAX_GRPC_MESSAGE_BYTES: u32 = 512 * 1024;
