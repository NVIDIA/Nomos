"""
Token storage backends.

Provides pluggable storage for capability tokens.
"""

from abc import ABC, abstractmethod

from nomos.level_0.types import CapabilityStatus, CapabilityToken


class TokenStore(ABC):
    """Abstract storage for capability tokens."""

    @abstractmethod
    def save(self, token: CapabilityToken) -> None:
        """Save or update a token."""
        pass

    @abstractmethod
    def get(self, token_id: str) -> CapabilityToken | None:
        """Get token by ID."""
        pass

    @abstractmethod
    def get_by_intent(self, intent_id: str) -> list[CapabilityToken]:
        """Get all tokens for an intent."""
        pass

    @abstractmethod
    def get_by_agent(self, agent_id: str) -> list[CapabilityToken]:
        """Get all tokens for an agent."""
        pass

    @abstractmethod
    def get_active(self) -> list[CapabilityToken]:
        """Get all active tokens."""
        pass

    @abstractmethod
    def delete(self, token_id: str) -> bool:
        """Delete a token."""
        pass


class InMemoryTokenStore(TokenStore):
    """In-memory token storage for testing and development."""

    def __init__(self):
        self._tokens: dict[str, CapabilityToken] = {}
        self._by_intent: dict[str, list[str]] = {}
        self._by_agent: dict[str, list[str]] = {}

    def save(self, token: CapabilityToken) -> None:
        self._tokens[token.token_id] = token

        if token.intent_id not in self._by_intent:
            self._by_intent[token.intent_id] = []
        if token.token_id not in self._by_intent[token.intent_id]:
            self._by_intent[token.intent_id].append(token.token_id)

        if token.bearer_agent_id not in self._by_agent:
            self._by_agent[token.bearer_agent_id] = []
        if token.token_id not in self._by_agent[token.bearer_agent_id]:
            self._by_agent[token.bearer_agent_id].append(token.token_id)

    def get(self, token_id: str) -> CapabilityToken | None:
        return self._tokens.get(token_id)

    def get_by_intent(self, intent_id: str) -> list[CapabilityToken]:
        token_ids = self._by_intent.get(intent_id, [])
        return [self._tokens[tid] for tid in token_ids if tid in self._tokens]

    def get_by_agent(self, agent_id: str) -> list[CapabilityToken]:
        token_ids = self._by_agent.get(agent_id, [])
        return [self._tokens[tid] for tid in token_ids if tid in self._tokens]

    def get_active(self) -> list[CapabilityToken]:
        return [t for t in self._tokens.values() if t.status == CapabilityStatus.ACTIVE]

    def delete(self, token_id: str) -> bool:
        token = self._tokens.pop(token_id, None)
        if token:
            if token.intent_id in self._by_intent:
                self._by_intent[token.intent_id] = [
                    tid for tid in self._by_intent[token.intent_id] if tid != token_id
                ]
            if token.bearer_agent_id in self._by_agent:
                self._by_agent[token.bearer_agent_id] = [
                    tid for tid in self._by_agent[token.bearer_agent_id] if tid != token_id
                ]
            return True
        return False

    def clear(self) -> None:
        """Clear all tokens (for testing)."""
        self._tokens.clear()
        self._by_intent.clear()
        self._by_agent.clear()
