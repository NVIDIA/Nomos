# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""Tests for the council-side shell command policy helpers."""

from nomos.level_1.policy.shell import extract_shell_command_words, shell_path_policy_error


class TestCommandSeparators:
    def test_semicolon_is_a_separator(self):
        assert extract_shell_command_words("/bin/true; rm -rf /tmp/x") == ["/bin/true", "rm"]
        assert "rm" in shell_path_policy_error("/bin/true; rm -rf /tmp/x")

    def test_newline_is_a_separator(self):
        """A newline starts a new command in sh -c; the policy must see it."""
        assert extract_shell_command_words("/bin/true\nrm -rf /tmp/x") == ["/bin/true", "rm"]
        assert "rm" in shell_path_policy_error("/bin/true\nrm -rf /tmp/x")

    def test_crlf_is_a_separator(self):
        assert extract_shell_command_words("/bin/true\r\nrm -rf /tmp/x") == ["/bin/true", "rm"]

    def test_dollar_paren_substitution_still_caught(self):
        assert "bash" in shell_path_policy_error("/bin/echo $(bash -c id)")

    def test_multi_stage_pipeline_unchanged(self):
        words = extract_shell_command_words("/bin/a | /bin/b && /bin/c || /bin/d")
        assert words == ["/bin/a", "/bin/b", "/bin/c", "/bin/d"]
        assert shell_path_policy_error("/bin/a | /bin/b && /bin/c || /bin/d") == ""

    def test_env_assignment_prefix_unchanged(self):
        assert extract_shell_command_words("FOO=1 /bin/env") == ["/bin/env"]
