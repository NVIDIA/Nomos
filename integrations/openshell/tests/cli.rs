// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! Verify explicit model selection without relying on the caller's environment.
use std::process::Command;

/// Run the executable with model-selection environment variables removed.
fn command() -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_nomos-request-review"));
    for name in [
        "NOMOS_MODEL_ENDPOINT",
        "NOMOS_MODEL_ID",
        "NOMOS_EXPECTED_MODEL_ID",
        "NOMOS_MODEL_TOKEN",
    ] {
        command.env_remove(name);
    }
    command
}

/// A public build must not silently select a provider-specific default model.
#[test]
fn model_id_and_endpoint_are_required() {
    for (arguments, missing) in [
        (
            vec!["--model-endpoint", "https://model.example/v1/systemone"],
            "--model-id",
        ),
        (vec!["--model-id", "example-model"], "--model-endpoint"),
    ] {
        let output = command().args(arguments).output().unwrap();
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains(missing));
    }
}

/// Invalid listener/body settings must fail before any model network operation.
#[test]
fn unsafe_listener_and_invalid_limit_order_fail_at_startup() {
    for (arguments, reason) in [
        (
            vec!["--bind", "0.0.0.0:50051"],
            "nonloopback listener requires",
        ),
        (
            vec!["--max-body-bytes", "524288"],
            "body cap must be smaller",
        ),
    ] {
        let output = command()
            .args([
                "--model-endpoint",
                "https://model.example/v1/systemone",
                "--model-id",
                "example-model",
            ])
            .args(arguments)
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains(reason));
    }
}
