"""Open Policy Agent integration for front-door intent filtering."""

from .client import (
    OPAEvaluationError,
    OPAHttpClient,
    OPAPolicyDecision,
    build_intent_filter_input,
    parse_opa_decision,
)

__all__ = [
    "OPAEvaluationError",
    "OPAHttpClient",
    "OPAPolicyDecision",
    "build_intent_filter_input",
    "parse_opa_decision",
]
