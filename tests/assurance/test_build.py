# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Local fixture deployment preserves trust and reader credentials across rebuilds."""

from scripts import assurance_poc


def test_fixture_credentials_survive_rebuilds_and_add_reader_token(tmp_path, monkeypatch):
    monkeypatch.setattr(assurance_poc, "STATE", tmp_path)
    initial = assurance_poc.credentials()
    original_key = (tmp_path / "signing.key").read_bytes()
    assert initial["API_READ_TOKEN"]
    assert assurance_poc.credentials() == initial
    # Upgrade an older fixture deployment without replacing its signing material.
    legacy = {key: value for key, value in initial.items() if key != "API_READ_TOKEN"}
    (tmp_path / "access.env").write_text("\n".join(f"{k}={v}" for k, v in legacy.items()))
    upgraded = assurance_poc.credentials()
    assert upgraded["API_READ_TOKEN"]
    assert {k: v for k, v in upgraded.items() if k != "API_READ_TOKEN"} == legacy
    assert (tmp_path / "signing.key").read_bytes() == original_key
    assert assurance_poc.credentials() == upgraded
    assert (tmp_path / "access.env").stat().st_mode & 0o777 == 0o600
