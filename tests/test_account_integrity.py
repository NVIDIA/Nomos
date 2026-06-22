# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


import hashlib

import pytest

from nomos.auth.account_integrity import ACCOUNT_INTEGRITY_FIELD, sign_account_record
from nomos.auth.authenticator import AgentAuthenticator


class FakeIntegrityStore:
    def __init__(self, *, tamper_permissions: bool = False) -> None:
        self.token = "test-token"
        self.token_hash = hashlib.sha256(self.token.encode()).hexdigest()
        self.account = {
            "token_hash": self.token_hash,
            "display_name": "Test Agent",
            "enabled": "1",
        }
        self.permissions = {"shell:read"}
        self.councils = {"council-prod"}
        self.key = "integrity-key"
        self.tamper_permissions = tamper_permissions
        self.account[ACCOUNT_INTEGRITY_FIELD] = sign_account_record(
            key=self.key,
            agent_id="agent-test",
            account=self.account,
            permissions=self.permissions,
            councils=self.councils,
        )

    async def get_rate_limit_count(self, _kind: str, _token_hash: str) -> int:
        return 0

    async def increment_rate_limit(self, _kind: str, _token_hash: str, _ttl: int) -> int:
        return 1

    async def find_agent_by_token_hash(self, token_hash: str) -> str | None:
        return "agent-test" if token_hash == self.token_hash else None

    async def get_account(self, _agent_id: str) -> dict[str, str] | None:
        return dict(self.account)

    async def get_account_permissions(self, _agent_id: str) -> set[str]:
        if self.tamper_permissions:
            return {"shell:read", "infra:destroy"}
        return set(self.permissions)

    async def get_account_councils(self, _agent_id: str) -> set[str]:
        return set(self.councils)

    def verify_account_integrity(
        self,
        agent_id: str,
        account: dict[str, str],
        permissions: set[str],
        councils: set[str],
    ) -> bool:
        expected = sign_account_record(
            key=self.key,
            agent_id=agent_id,
            account=account,
            permissions=permissions,
            councils=councils,
        )
        return account.get(ACCOUNT_INTEGRITY_FIELD) == expected


@pytest.mark.asyncio
async def test_authenticator_accepts_valid_account_integrity() -> None:
    store = FakeIntegrityStore()

    identity = await AgentAuthenticator(store).authenticate(store.token)  # type: ignore[arg-type]

    assert identity is not None
    assert identity.agent_id == "agent-test"


@pytest.mark.asyncio
async def test_authenticator_rejects_tampered_account_integrity() -> None:
    store = FakeIntegrityStore(tamper_permissions=True)

    identity = await AgentAuthenticator(store).authenticate(store.token)  # type: ignore[arg-type]

    assert identity is None
