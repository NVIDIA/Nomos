// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! Operator-controlled policy configuration, separate from workload content.
use crate::openshell::middleware::v1::ValidateConfigRequest;
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

/// One bounded, versioned yes/no question catalog attached by policy.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Catalog {
    /// Stable operator catalog revision, included in sanitized diagnostics.
    pub catalog_version: String,
    /// One or more questions evaluated together, subject to byte-size limits.
    pub rules: Vec<Rule>,
}
/// Which side of the model's yes-score threshold triggers denial.
#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum DenyWhen {
    /// Deny when the yes score meets or exceeds the threshold; the default.
    #[default]
    Yes,
    /// Deny when the yes score is strictly below the threshold.
    No,
}

/// An operator-controlled yes/no assessment and its denial direction.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Rule {
    /// Unique lowercase machine identifier, suitable for diagnostic records.
    pub id: String,
    /// Assessment question, never supplied by the workload.
    pub question: String,
    /// Cutoff applied directly to the returned yes score, tuned on independent examples.
    pub threshold: f64,
    /// Thresholded answer that triggers denial; defaults to yes when omitted.
    #[serde(default)]
    pub deny_when: DenyWhen,
}

/// Check a bounded identifier without accepting control characters or payload text.
pub fn identifier(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && s.bytes().next().is_some_and(|b| b.is_ascii_lowercase())
        && s.bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-')
}

impl Catalog {
    /// Parse protobuf configuration without accepting missing or unknown fields.
    pub fn from_proto(config: Option<prost_types::Struct>) -> Result<Self, &'static str> {
        use prost_types::value::Kind;
        fn value(v: prost_types::Value) -> Result<serde_json::Value, &'static str> {
            Ok(match v.kind.ok_or("invalid_config")? {
                Kind::NullValue(_) => serde_json::Value::Null,
                Kind::NumberValue(n) => serde_json::Number::from_f64(n)
                    .map(serde_json::Value::Number)
                    .ok_or("invalid_config")?,
                Kind::StringValue(s) => s.into(),
                Kind::BoolValue(b) => b.into(),
                Kind::StructValue(s) => object(s)?,
                Kind::ListValue(l) => serde_json::Value::Array(
                    l.values.into_iter().map(value).collect::<Result<_, _>>()?,
                ),
            })
        }
        fn object(s: prost_types::Struct) -> Result<serde_json::Value, &'static str> {
            Ok(serde_json::Value::Object(
                s.fields
                    .into_iter()
                    .map(|(k, v)| value(v).map(|value| (k, value)))
                    .collect::<Result<_, _>>()?,
            ))
        }
        let config = config.ok_or("invalid_config")?;
        if prost::Message::encoded_len(&config) > 16 * 1024 {
            return Err("invalid_config");
        }
        let out: Self = serde_json::from_value(object(config)?).map_err(|_| "invalid_config")?;
        out.validate()?;
        Ok(out)
    }
    /// Enforce catalog shape and limits before discovery/evaluation work proceeds.
    pub fn validate(&self) -> Result<(), &'static str> {
        let mut ids = BTreeSet::new();
        if !identifier(&self.catalog_version) || self.rules.is_empty() {
            return Err("invalid_config");
        }
        for rule in &self.rules {
            if !identifier(&rule.id)
                || !ids.insert(&rule.id)
                || rule.question.trim().is_empty()
                || rule.question.len() > 1024
                || !rule.threshold.is_finite()
                || !(0.0..=1.0).contains(&rule.threshold)
            {
                return Err("invalid_config");
            }
        }
        Ok(())
    }
}

/// Validate a named policy attachment; the registration identity is operator owned.
pub fn validate_request(request: ValidateConfigRequest) -> Result<Catalog, &'static str> {
    Catalog::from_proto(request.config)
}
