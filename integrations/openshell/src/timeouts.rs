// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! Validated process-level assessment budgets, independent of sandbox policy.
//!
//! Defaults leave 500 ms between the model request (4 s), full evaluation
//! (4.5 s), and the example OpenShell middleware RPC budget (5 s). OpenShell's
//! budget is configured separately; these values cannot extend its RPC deadline.
use std::time::Duration;

/// Default total model HTTP request/response budget in milliseconds.
pub const DEFAULT_MODEL_TIMEOUT_MS: u64 = 4000;
/// Default complete evaluation budget in milliseconds.
pub const DEFAULT_EVALUATION_TIMEOUT_MS: u64 = 4500;

/// Finite model and evaluation budgets. OpenShell's RPC timeout is configured separately.
#[derive(Clone, Copy, Debug)]
pub struct Timeouts {
    // Inner cap: connecting, sending the model request and receiving its body.
    model: Duration,
    // Outer cap: validation, model I/O, answer validation and decision processing.
    evaluation: Duration,
}

impl Default for Timeouts {
    fn default() -> Self {
        Self {
            model: Duration::from_millis(DEFAULT_MODEL_TIMEOUT_MS),
            evaluation: Duration::from_millis(DEFAULT_EVALUATION_TIMEOUT_MS),
        }
    }
}

impl Timeouts {
    /// Require 1 <= model_ms < evaluation_ms <= 30,000; zero never disables a deadline.
    ///
    /// Ordering leaves room for work around model I/O. The 30-second ceiling
    /// matches OpenShell's configurable per-RPC maximum, but the operator must
    /// still choose an actual RPC timeout above the evaluation budget.
    pub fn from_millis(model_ms: u64, evaluation_ms: u64) -> Result<Self, &'static str> {
        if model_ms == 0 || model_ms >= evaluation_ms || evaluation_ms > 30_000 {
            return Err("timeouts require 1 <= model_timeout_ms < evaluation_timeout_ms <= 30000");
        }
        Ok(Self {
            model: Duration::from_millis(model_ms),
            evaluation: Duration::from_millis(evaluation_ms),
        })
    }

    /// Total HTTP request/response budget for the model client.
    pub fn model(self) -> Duration {
        self.model
    }

    /// Budget for validation, model assessment, answer validation and decision processing.
    pub fn evaluation(self) -> Duration {
        self.evaluation
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn budgets_are_finite_ordered_and_leave_default_rpc_headroom() {
        let defaults = Timeouts::default();
        assert_eq!(defaults.model(), Duration::from_millis(4000));
        assert_eq!(defaults.evaluation(), Duration::from_millis(4500));
        for pair in [
            (0, 1500),
            (1400, 0),
            (1500, 1500),
            (1600, 1500),
            (1, 30001),
            (u64::MAX, u64::MAX),
        ] {
            assert!(Timeouts::from_millis(pair.0, pair.1).is_err());
        }
        for pair in [(1, 2), (3000, 3500), (29999, 30000)] {
            assert!(Timeouts::from_millis(pair.0, pair.1).is_ok());
        }
    }
}
