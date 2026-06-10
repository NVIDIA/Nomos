"""Shared helpers for trusted executor implementations."""

from .evidence import (
    ExecutorIdentity,
    ProviderExecutorIdentity,
    certify_execution_result,
    executor_identity_from_signer,
    load_or_create_executor_identity,
)

__all__ = [
    "ExecutorIdentity",
    "ProviderExecutorIdentity",
    "certify_execution_result",
    "executor_identity_from_signer",
    "load_or_create_executor_identity",
]
