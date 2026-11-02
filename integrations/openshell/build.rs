// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! Generate bindings from the vendored, revision-pinned OpenShell contract.
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut config = tonic_prost_build::Config::new();
    config.protoc_executable(protoc_bin_vendored::protoc_bin_path()?);
    tonic_prost_build::configure().compile_with_config(
        config,
        &[
            std::path::PathBuf::from("proto/supervisor_middleware.proto"),
            std::path::PathBuf::from("proto/extension.proto"),
        ],
        &[
            std::path::PathBuf::from("proto"),
            protoc_bin_vendored::include_path()?,
        ],
    )?;
    println!("cargo:rerun-if-changed=proto");
    Ok(())
}
