# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shell syntax facts must not act as an executable allowlist or run commands."""

import pytest

from nomos.opa.shell_analysis import (
    MAX_COMMAND_CHARS,
    MAX_COMMAND_WORDS,
    analyze_shell_command,
)


@pytest.mark.parametrize(
    ("command", "executable", "name", "argv"),
    [
        ("git status --short", "git", "git", ["status", "--short"]),
        ("python3 script.py", "python3", "python3", ["script.py"]),
        ("npm test", "npm", "npm", ["test"]),
        (
            "/workspace/project/build/test_runner --quick",
            "/workspace/project/build/test_runner",
            "test_runner",
            ["--quick"],
        ),
        ("./build/test_runner --quick", "./build/test_runner", "test_runner", ["--quick"]),
        ("../build/test_runner --quick", "../build/test_runner", "test_runner", ["--quick"]),
        (
            "/usr/local/bin/custom-tool hello",
            "/usr/local/bin/custom-tool",
            "custom-tool",
            ["hello"],
        ),
        ("/opt/tools/echo hello", "/opt/tools/echo", "echo", ["hello"]),
        ("/workspace/build/if hello", "/workspace/build/if", "if", ["hello"]),
        ("/workspace/build/time hello", "/workspace/build/time", "time", ["hello"]),
        ("/workspace/build/VAR=x hello", "/workspace/build/VAR=x", "VAR=x", ["hello"]),
        (
            "/usr/bin/time -p ./build/test_runner",
            "/usr/bin/time",
            "time",
            ["-p", "./build/test_runner"],
        ),
        (
            "'/workspace/project/build tools/test runner' 'argument with spaces'",
            "/workspace/project/build tools/test runner",
            "test runner",
            ["argument with spaces"],
        ),
        (
            r"./build\ tools/test\ runner hello",
            "./build tools/test runner",
            "test runner",
            ["hello"],
        ),
        ("printf '%s' hello", "printf", "printf", ["%s", "hello"]),
        ("runner 'a b' c\\ d -- -rf", "runner", "runner", ["a b", "c d", "--", "-rf"]),
        ("git -c color.ui=always status", "git", "git", ["-c", "color.ui=always", "status"]),
        ("cd build", "cd", "cd", ["build"]),
        (":", ":", ":", []),
    ],
)
def test_any_literal_executable_is_analysis_input(command, executable, name, argv):
    """Eligibility for policy review does not depend on name or install directory."""
    result = analyze_shell_command(command)
    assert result["fully_parsed"] is True
    assert len(result["commands"]) == 1
    parsed = result["commands"][0]
    assert parsed["executable"] == executable
    assert parsed["name"] == name
    assert parsed["argv"] == argv
    assert result["reason"] == ""


@pytest.mark.parametrize(
    "command",
    [
        "VAR=x sudo id",
        "VAR+=x sudo id",
        "VAR+= sudo id",
        "PATH=/custom/bin rm -rf output",
        "! sudo id",
        "time sudo id",
        "coproc sudo id",
        "if true",
        "then sudo id",
        "while true",
        "for item in output",
        "case item in",
        "function cleanup",
        "echo hello; sudo id",
        "echo hello && sudo id",
        "cat input | head",
        "echo hello > output",
        "echo $(id)",
        "echo `id`",
        "echo hello\nsudo id",
        '"${TOOL}" argument',
        '"$(choose_tool)" argument',
        "`choose_tool` argument",
        "/workspace/$TOOL argument",
        "'' sudo id",
    ],
)
def test_shell_structure_is_not_mistaken_for_an_arbitrary_executable(command):
    """Removing executable restrictions must not accidentally admit shell grammar."""
    assert analyze_shell_command(command)["fully_parsed"] is False


@pytest.mark.parametrize(
    ("command", "executable", "name", "argv_prefix"),
    [
        ("sudo id " + "a " * 5000, "sudo", "sudo", ["id"]),
        ("sudo " + "a" * 16_400, "sudo", "sudo", []),
        ("sudo id 'unterminated", "sudo", "sudo", ["id"]),
        ("rm -rf output " + "a " * 5000, "rm", "rm", ["-rf", "output"]),
        ("rm -rf output " + "a" * 16_400, "rm", "rm", ["-rf", "output"]),
        ("rm -rf 'unterminated", "rm", "rm", ["-rf"]),
        ("/custom/build/sudo id " + "a " * 5000, "/custom/build/sudo", "sudo", ["id"]),
        ('./build/rm -rf "$TARGET"', "./build/rm", "rm", ["-rf"]),
    ],
)
def test_incomplete_analysis_keeps_only_proven_command_facts(
    command, executable, name, argv_prefix
):
    """Padding and malformed tails cannot erase an already known hard-deny prefix."""
    result = analyze_shell_command(command)
    assert result["fully_parsed"] is False
    assert len(result["commands"]) == 1
    parsed = result["commands"][0]
    assert parsed["executable"] == executable
    assert parsed["name"] == name
    assert parsed["argv"][: len(argv_prefix)] == argv_prefix
    assert len(parsed["argv"]) <= 4096


def test_character_limit_does_not_turn_partial_word_into_a_destructive_option():
    # The bounded prefix ends at --force, but the real argument continues.
    # Treating the cut word as complete would invent a force option.
    prefix = "rm -r "
    command = prefix + " " * (16_384 - len(prefix) - len("--force")) + "--force-not-an-option"
    result = analyze_shell_command(command)
    assert result["fully_parsed"] is False
    assert result["commands"][0]["name"] == "rm"
    assert result["commands"][0]["argv"] == ["-r"]


def test_analyzing_a_literal_command_does_not_execute_it(tmp_path):
    """The proposed executable may be nonexistent; analysis has no process effects."""
    marker = tmp_path / "analysis-must-not-create-this"
    result = analyze_shell_command(f"/usr/bin/touch {marker}")
    assert result["fully_parsed"] is True
    assert not marker.exists()


def test_exact_character_limit_keeps_complete_final_argument():
    command = "runner " + "a" * (MAX_COMMAND_CHARS - len("runner "))
    result = analyze_shell_command(command)
    assert result["fully_parsed"] is True
    assert result["commands"][0]["argv"] == [command[len("runner ") :]]


def test_exact_word_limit_is_supported_but_next_word_is_incomplete_analysis():
    command = "runner " + "a " * (MAX_COMMAND_WORDS - 1)
    result = analyze_shell_command(command)
    assert result["fully_parsed"] is True
    assert len(result["commands"][0]["argv"]) == MAX_COMMAND_WORDS - 1
    beyond = analyze_shell_command(command + "a")
    assert beyond["fully_parsed"] is False
    assert beyond["commands"][0]["executable"] == "runner"


def test_character_limit_cannot_invent_a_denylisted_executable_from_partial_word():
    # At the character bound this token spells sudo, but its full spelling is
    # sudo-not-the-command. No executable word was completed before truncation.
    command = " " * (MAX_COMMAND_CHARS - len("sudo")) + "sudo-not-the-command"
    result = analyze_shell_command(command)
    assert result["fully_parsed"] is False
    assert result["commands"] == []


@pytest.mark.parametrize(
    ("command", "explanation"),
    [
        (None, "nonempty shell command"),
        ("", "nonempty shell command"),
        ('cat "$TARGET"', "Variable-expansion"),
        ('cat "${TARGET}"', "Variable-expansion"),
        ("echo $((1 + 2))", "Arithmetic-expansion"),
        ("echo $(id)", "Command-substitution"),
        ("echo `id`", "Command-substitution"),
        ("echo hi; echo there", "composition"),
        ("echo hi > out", "redirection"),
        ("cat *.txt", "Globbing"),
        ("cat ~/file", "Home-directory"),
        ("(echo hi)", "grouping"),
        ("echo hi # comment", "comment"),
        ("echo hi\nid", "Multiple lines"),
        ("echo hi\x00", "NUL bytes"),
        ("echo 'unterminated", "Unterminated quoting"),
        ("echo incomplete\\", "incomplete escape"),
        ("VAR=x runner", "assignment prefixes"),
        ("time runner", "reserved-word prefixes"),
        ("echo " + "a" * MAX_COMMAND_CHARS, "character limit"),
        ("echo " + "a " * MAX_COMMAND_WORDS, "word limit"),
    ],
)
def test_incomplete_reason_identifies_the_limitation_without_claiming_policy(command, explanation):
    result = analyze_shell_command(command)
    assert result["fully_parsed"] is False
    assert explanation in result["reason"]
    assert "approval" not in result["reason"]
    assert "forbidden" not in result["reason"]
    assert "supported" not in result


def test_analysis_reason_does_not_echo_private_command_material():
    marker = "private-argument-marker"
    result = analyze_shell_command(f'cat "$TARGET" {marker}')
    assert result["fully_parsed"] is False
    assert "Variable-expansion" in result["reason"]
    assert marker not in result["reason"]
    assert "$TARGET" not in result["reason"]
