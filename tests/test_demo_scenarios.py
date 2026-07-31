# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import base64
import hashlib

from demo.server.scenarios import scenarios_by_id, scenarios_by_silo


def test_packaged_script_scenario_carries_valid_attachment():
    scenario = scenarios_by_id()["s8-packaged-script"]

    intent = scenario.to_intent()
    packaged_files = intent["packaged_files"]

    assert scenario.command == "/usr/local/bin/python3 scripts/packaged_code_review.py"
    assert len(packaged_files) == 1

    packaged_file = packaged_files[0]
    content = base64.b64decode(packaged_file["content_base64"], validate=True)

    assert packaged_file["path"] == "scripts/packaged_code_review.py"
    assert packaged_file["path"] in scenario.command
    assert packaged_file["sha256"] == hashlib.sha256(content).hexdigest()
    assert packaged_file["mode"] == "0600"
    assert b"demo-artifacts" in content
    assert b"subprocess" not in content


def test_packaged_script_scenario_is_published_in_sysadmin_silo():
    sysadmin = next(silo for silo in scenarios_by_silo() if silo["name"] == "SysAdmin")
    scenario_ids = [scenario["id"] for scenario in sysadmin["scenarios"]]

    assert "s8-packaged-script" in scenario_ids


def test_db_pool_scenario_followup_requires_certified_evidence():
    scenario = scenarios_by_id()["s7-iterate"]

    assert scenario.followup is not None
    assert scenario.followup.requires_certified_evidence is True
    assert scenario.followup.evidence_probe is not None
    assert "Three rounds" in scenario.subtitle
    assert "round 2" in scenario.rationale
    assert "round 3" in scenario.rationale
    assert "updated justification" in scenario.rationale
    assert "updated blast radius" in scenario.rationale
    assert "CERTIFIED EXECUTOR EVIDENCE" in scenario.followup.justification
    assert "certified_evidence[0]" in scenario.rationale
    assert "CERTIFIED EXECUTOR EVIDENCE" in scenario.rationale
    assert "simulated demo data" in scenario.rationale
    assert "deployment_max_unavailable=0" in scenario.followup.justification
    assert "deployment_max_surge=1" in scenario.followup.justification
    assert "pod_disruption_budget=checkout-service-pdb" in scenario.followup.justification
    assert "rollout_status_preflight" in scenario.followup.justification
    assert "maxUnavailable=0" in scenario.followup.blast_radius
    assert "maxSurge=1" in scenario.followup.blast_radius
    assert "PDB with minAvailable=7" in scenario.followup.blast_radius
    assert "Round 2 ran" in scenario.followup.revision_note
    assert "Round 3 returns to the original pool-increase command" in (
        scenario.followup.revision_note
    )
    assert "certified_evidence[0]" in scenario.followup.revision_note
    assert "CERTIFIED EXECUTOR EVIDENCE" in scenario.followup.certified_evidence_note
    assert "rollout-safety values are simulated demo data" in (
        scenario.followup.certified_evidence_note
    )
    assert "signed stdout/stderr evidence" in scenario.followup.evidence_probe.justification
    assert "maxUnavailable/maxSurge" in scenario.followup.evidence_probe.rationale
    assert "rollout-status preflight" in scenario.followup.evidence_probe.rationale
    assert len(scenario.followup.evidence_probe.packaged_files) == 1
    packaged_script = base64.b64decode(
        scenario.followup.evidence_probe.packaged_files[0]["content_base64"],
        validate=True,
    ).decode()
    assert "CERTIFIED DB POOL EVIDENCE REPORT" in packaged_script
    assert "deployment_max_unavailable" in packaged_script
    assert "deployment_max_surge" in packaged_script
    assert "readiness_probe" in packaged_script
    assert "pod_disruption_budget" in packaged_script
    assert "rollout_status_preflight" in packaged_script
    assert "deployment_ready_replicas" in packaged_script
    assert "python3" in scenario.followup.evidence_probe.command
