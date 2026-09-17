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


class TestCommandSubstitution:
    def test_double_quoted_substitution_is_extracted(self):
        """Double quotes do not stop $(...) from running; the inner tool must be seen."""
        assert extract_shell_command_words('/bin/echo "$(uname -s)"') == ["/bin/echo", "uname"]
        assert "uname" in shell_path_policy_error('/bin/echo "$(uname -s)"')

    def test_nested_double_quoted_substitution_is_extracted(self):
        words = extract_shell_command_words('/bin/echo "$(echo $(uname -s))"')
        assert words == ["/bin/echo", "echo", "uname"]

    def test_single_quoted_substitution_is_literal(self):
        assert extract_shell_command_words("/bin/echo '$(uname -s)'") == ["/bin/echo"]
        assert shell_path_policy_error("/bin/echo '$(uname -s)'") == ""

    def test_unquoted_substitution_still_extracted(self):
        assert extract_shell_command_words("/bin/echo $(uname -s)") == ["/bin/echo", "uname"]

    def test_unbalanced_substitution_runs_to_end(self):
        assert extract_shell_command_words('/bin/echo "$(uname -s') == ["/bin/echo", "uname"]

    def test_apostrophe_inside_double_quotes_is_literal(self):
        """An apostrophe inside double quotes does not open a single-quoted run."""
        words = extract_shell_command_words('/bin/echo "it\'s $(uname -s)"')
        assert words == ["/bin/echo", "uname"]
        words = extract_shell_command_words("/bin/echo \"'$(uname -s)'\"")
        assert words == ["/bin/echo", "uname"]

    def test_quoted_close_paren_does_not_end_body(self):
        command = '/bin/echo $(/bin/printf ")"; uname -s)'
        assert extract_shell_command_words(command) == ["/bin/echo", "/bin/printf", "uname"]
        assert extract_shell_command_words("/bin/echo $(echo ')')") == ["/bin/echo", "echo"]

    def test_nested_substitution_keeps_its_own_quote_context(self):
        """A quote or ``)`` inside a nested ``$(...)`` cannot end the enclosing body."""
        command = '/bin/echo "$(/bin/echo "$(/bin/printf ")")"; uname -s)"'
        assert extract_shell_command_words(command) == [
            "/bin/echo",
            "/bin/echo",
            "uname",
            "/bin/printf",
        ]
        assert "uname" in shell_path_policy_error(command)
        words = extract_shell_command_words('/bin/echo $(/bin/echo "$(uname)" x) y')
        assert words == ["/bin/echo", "/bin/echo", "uname"]

    def test_arithmetic_expansion_runs_no_command(self):
        for command in ("/bin/echo $((1+2))", '/bin/echo "$((1+2))"', "/bin/echo $(( (1+2)*3 ))"):
            assert extract_shell_command_words(command) == ["/bin/echo"], command
            assert shell_path_policy_error(command) == "", command

    def test_quotes_inside_arithmetic_do_not_hide_substitutions(self):
        """An arithmetic expression is treated as double-quoted by the shell, so
        single quotes inside it are ordinary text and ``$(...)`` still runs."""
        for command in (
            "/bin/echo \"$((1 + '$(uname)'))\"",
            "/bin/echo $((1 + '$(uname)'))",
            "/bin/echo \"$((1 + ${unset_var:-'$(uname -s >&2)'}))\"",
        ):
            assert extract_shell_command_words(command) == ["/bin/echo", "uname"], command

    def test_substitution_nested_in_arithmetic_is_extracted(self):
        assert extract_shell_command_words("/bin/echo $(( $(uname -s) ))") == ["/bin/echo", "uname"]

    def test_substitution_supplying_the_command_word_fails_closed(self):
        """When the executable name comes from a substitution nobody can inspect it."""
        words = extract_shell_command_words('/bin/echo "$( $(/bin/echo uname) )"')
        assert words == ["/bin/echo", "$(...)", "/bin/echo"]
        assert "$(...)" in shell_path_policy_error('/bin/echo "$( $(/bin/echo uname) )"')
        assert extract_shell_command_words("$(/bin/echo uname)") == ["$(...)", "/bin/echo"]
        assert extract_shell_command_words("/bin/echo$(uname)") == ["$(...)", "uname"]

    def test_process_substitution_body_is_extracted(self):
        assert extract_shell_command_words("/bin/cat <(uname -s)") == ["/bin/cat", "uname"]
        words = extract_shell_command_words("/bin/tee >(uname -s) </dev/null")
        assert words == ["/bin/tee", "uname"]
        assert extract_shell_command_words('/bin/echo "<(uname)"') == ["/bin/echo"]

    def test_escaped_dollar_is_literal(self):
        assert extract_shell_command_words('/bin/echo "\\$(uname -s)"') == ["/bin/echo"]

    def test_existing_pipeline_with_substitution_unchanged(self):
        command = "/bin/cat /tmp/a | /bin/grep x && /bin/echo $(id -u)"
        assert extract_shell_command_words(command) == ["/bin/cat", "/bin/grep", "/bin/echo", "id"]
        assert "id" in shell_path_policy_error(command)
