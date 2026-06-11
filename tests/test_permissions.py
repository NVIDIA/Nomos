# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for PermissionScope and categorize_intent — Phase 1.2.
"""

from nomos.auth.permissions import PermissionScope, categorize_intent
from nomos.level_0.types import Intent


def _intent_with_command(command: str, action_type: str = "system.info") -> Intent:
    """Create a minimal intent with a command in parameters."""
    return Intent.create(
        agent_id="test-agent",
        action_type=action_type,
        description="Test intent for permission categorization",
        target_resource="/tmp",
        justification="Testing",
        parameters={"command": command},
    )


def _intent_with_top_level_command(command: str, action_type: str = "system.exec") -> Intent:
    """Create a NAT-shaped intent with command on the top-level field."""
    return Intent.create(
        agent_id="test-agent",
        action_type=action_type,
        description="Test intent for permission categorization",
        target_resource="/tmp",
        command=command,
        working_directory="/tmp",
        justification="Testing",
    )


class TestReadOnlyCommands:
    def test_ls(self):
        perms = categorize_intent(_intent_with_command("ls /tmp"))
        assert PermissionScope.SHELL_READ in perms
        assert PermissionScope.SHELL_WRITE not in perms

    def test_cat(self):
        perms = categorize_intent(_intent_with_command("cat /etc/hosts"))
        assert PermissionScope.SHELL_READ in perms
        assert PermissionScope.FILE_READ in perms

    def test_ps(self):
        perms = categorize_intent(_intent_with_command("ps aux"))
        assert PermissionScope.SHELL_READ in perms

    def test_date(self):
        perms = categorize_intent(_intent_with_command("date"))
        assert PermissionScope.SHELL_READ in perms

    def test_echo(self):
        perms = categorize_intent(_intent_with_command("echo hello"))
        assert PermissionScope.SHELL_READ in perms

    def test_grep(self):
        perms = categorize_intent(_intent_with_command("grep error /var/log/syslog"))
        assert PermissionScope.FILE_READ in perms


class TestFileDeleteCommands:
    def test_rm(self):
        perms = categorize_intent(_intent_with_command("rm /tmp/somefile"))
        assert PermissionScope.FILE_DELETE in perms
        assert PermissionScope.SHELL_WRITE in perms

    def test_rm_rf(self):
        perms = categorize_intent(_intent_with_command("rm -rf /var/data"))
        assert PermissionScope.FILE_DELETE in perms
        assert PermissionScope.SHELL_WRITE in perms

    def test_rmdir(self):
        perms = categorize_intent(_intent_with_command("rmdir /tmp/empty"))
        assert PermissionScope.FILE_DELETE in perms


class TestFileWriteCommands:
    def test_redirect(self):
        perms = categorize_intent(_intent_with_command("echo data > /tmp/out.txt"))
        assert PermissionScope.FILE_WRITE in perms

    def test_stderr_redirect_to_dev_null_is_not_file_write(self):
        perms = categorize_intent(
            _intent_with_command(
                "grep -rn MissingSessionEntryReservedSlotKeys /workspace/src 2>/dev/null | head -20"
            )
        )
        assert PermissionScope.FILE_WRITE not in perms

    def test_stderr_redirect_to_file_is_file_write(self):
        perms = categorize_intent(_intent_with_command("grep -rn ERROR /workspace 2>/tmp/errs"))
        assert PermissionScope.FILE_WRITE in perms

    def test_append_redirect(self):
        perms = categorize_intent(_intent_with_command("echo data >> /tmp/out.txt"))
        assert PermissionScope.FILE_WRITE in perms

    def test_touch(self):
        perms = categorize_intent(_intent_with_command("touch /tmp/newfile"))
        assert PermissionScope.FILE_WRITE in perms

    def test_tee(self):
        perms = categorize_intent(_intent_with_command("echo data | tee /tmp/out"))
        assert PermissionScope.FILE_WRITE in perms


class TestShellWriteCommands:
    def test_mkdir(self):
        perms = categorize_intent(_intent_with_command("mkdir /tmp/newdir"))
        assert PermissionScope.SHELL_WRITE in perms

    def test_cp(self):
        perms = categorize_intent(_intent_with_command("cp file1 file2"))
        assert PermissionScope.SHELL_WRITE in perms

    def test_mv(self):
        perms = categorize_intent(_intent_with_command("mv old new"))
        assert PermissionScope.SHELL_WRITE in perms

    def test_chmod(self):
        perms = categorize_intent(_intent_with_command("chmod 755 /app/bin"))
        assert PermissionScope.SHELL_WRITE in perms


class TestInfraCommands:
    def test_kubectl_apply(self):
        perms = categorize_intent(_intent_with_command("kubectl apply -f deploy.yaml"))
        assert PermissionScope.INFRA_DEPLOY in perms

    def test_kubectl_delete(self):
        perms = categorize_intent(_intent_with_command("kubectl delete pod my-pod"))
        assert PermissionScope.INFRA_DESTROY in perms

    def test_terraform_apply(self):
        perms = categorize_intent(_intent_with_command("terraform apply"))
        assert PermissionScope.INFRA_DEPLOY in perms

    def test_terraform_destroy(self):
        perms = categorize_intent(_intent_with_command("terraform destroy"))
        assert PermissionScope.INFRA_DESTROY in perms

    def test_helm_install(self):
        perms = categorize_intent(_intent_with_command("helm install my-release ./chart"))
        assert PermissionScope.INFRA_DEPLOY in perms

    def test_docker_run(self):
        perms = categorize_intent(_intent_with_command("docker run nginx"))
        assert PermissionScope.INFRA_DEPLOY in perms


class TestEdgeCases:
    def test_sudo_prefix_stripped(self):
        perms = categorize_intent(_intent_with_command("sudo ls /root"))
        assert PermissionScope.SHELL_READ in perms

    def test_unknown_command_conservative(self):
        """Unknown commands should get broad permissions."""
        perms = categorize_intent(_intent_with_command("custom-tool --do-stuff"))
        assert PermissionScope.SHELL_READ in perms
        assert PermissionScope.SHELL_WRITE in perms

    def test_empty_command_fallback_to_action_type(self):
        intent = Intent.create(
            agent_id="test",
            action_type="file.delete",
            description="Delete something for test categorization",
            target_resource="/tmp",
            justification="test",
        )
        perms = categorize_intent(intent)
        assert PermissionScope.FILE_DELETE in perms

    def test_action_type_read(self):
        intent = Intent.create(
            agent_id="test",
            action_type="file.read",
            description="Read something for test categorization",
            target_resource="/tmp",
            justification="test",
        )
        perms = categorize_intent(intent)
        assert PermissionScope.FILE_READ in perms

    def test_action_type_deploy(self):
        intent = Intent.create(
            agent_id="test",
            action_type="infra.deploy",
            description="Deploy something for test categorization",
            target_resource="/tmp",
            justification="test",
        )
        perms = categorize_intent(intent)
        assert PermissionScope.INFRA_DEPLOY in perms

    def test_multiple_permissions_single_command(self):
        """A compound command can require multiple permissions."""
        perms = categorize_intent(_intent_with_command("rm -rf /var/data"))
        assert PermissionScope.SHELL_WRITE in perms
        assert PermissionScope.FILE_DELETE in perms

    def test_top_level_command_is_primary_signal(self):
        """NAT-created intents put command on Intent.command, not parameters."""
        perms = categorize_intent(_intent_with_top_level_command("rm /tmp/somefile"))
        assert PermissionScope.SHELL_WRITE in perms
        assert PermissionScope.FILE_DELETE in perms
        assert PermissionScope.SHELL_READ not in perms

    def test_top_level_command_takes_precedence_over_legacy_parameters(self):
        intent = _intent_with_top_level_command("cat /etc/hosts")
        intent.parameters["command"] = "rm -rf /tmp/legacy"

        perms = categorize_intent(intent)

        assert PermissionScope.SHELL_READ in perms
        assert PermissionScope.FILE_READ in perms
        assert PermissionScope.FILE_DELETE not in perms
