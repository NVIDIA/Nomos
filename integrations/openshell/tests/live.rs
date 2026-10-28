// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! Manually run connectivity test against a configured decision-model endpoint, not a quality benchmark.
use nomos_request_review::{
    config::{Catalog, Rule},
    provider::{DecisionProvider, SystemOneProvider},
};

#[tokio::test]
#[ignore = "requires explicitly configured live inference endpoint and credentials"]
async fn native_endpoint_returns_complete_noul_assessment() {
    let endpoint = std::env::var("NOMOS_MODEL_ENDPOINT")
        .expect("set NOMOS_MODEL_ENDPOINT to a native SystemOne API URL");
    let model = std::env::var("NOMOS_MODEL_ID").expect("set NOMOS_MODEL_ID explicitly");
    let expected_model = std::env::var("NOMOS_EXPECTED_MODEL_ID").unwrap_or_else(|_| model.clone());
    let provider = SystemOneProvider::new(
        &endpoint,
        model,
        expected_model,
        std::env::var("NOMOS_MODEL_TOKEN").ok(),
        false,
    )
    .expect("valid HTTPS endpoint configuration");
    let catalog = Catalog {
        catalog_version: "live-smoke-v1".into(),
        rules: vec![Rule {
            id: "confidential_disclosure".into(),
            question: "Does this publication reveal non-public business information?".into(),
            threshold: 0.7,
            deny_when: Default::default(),
        }],
    };
    let result=provider.assess(serde_json::json!({"publication":"This is a synthetic public announcement about a fictional company."}),&catalog).await.expect("native endpoint must return a complete valid response");
    assert_eq!(result.scores.len(), 1);
    assert!((0.0..=1.0).contains(&result.scores["confidential_disclosure"]));
}
