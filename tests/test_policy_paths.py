# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


import pytest

from nomos.level_1.policy.paths import (
    normalized_glob_references,
    normalized_path_references,
    references_sensitive_path,
)


def test_normalizes_duplicate_and_dot_segments() -> None:
    refs = normalized_path_references("/bin/cat /app//keys/./council.pub")

    assert "/app/keys/council.pub" in refs


def test_resolves_relative_command_paths_against_working_directory() -> None:
    refs = normalized_path_references(
        "/bin/cat council.key",
        working_directory="/app/./keys",
    )

    assert "/app/keys" in refs
    assert "/app/keys/council.key" in refs


def test_detects_sensitive_path_references() -> None:
    assert references_sensitive_path("/bin/cat /app//keys/council.key")
    assert references_sensitive_path("/bin/cat council.key", working_directory="/app/./keys")


def test_double_slash_prefix_matches_sensitive() -> None:
    # POSIX lets normpath keep exactly two leading slashes; the kernel resolves
    # //app/keys as /app/keys, so the deny list must see it the same way.
    assert references_sensitive_path("/bin/cat //app/keys/council.key")
    assert references_sensitive_path("/bin/cat ///etc/shadow")
    assert "/app/keys/council.key" in normalized_path_references("/bin/cat //app/keys/council.key")
    assert references_sensitive_path("/bin/cat council.key", working_directory="//app/keys")


def test_glob_token_matches_sensitive_prefix() -> None:
    # The executor runs metacharacter commands under sh -c, so a glob that can
    # expand to a sensitive path must count as a reference to it.
    assert references_sensitive_path("/bin/cat /etc/shado?")
    assert references_sensitive_path("/bin/ls /app/ke*")
    assert references_sensitive_path("/bin/cat /app/keys/*")
    assert references_sensitive_path("/bin/ls /root/.ss*")
    assert references_sensitive_path("/bin/cat /e*/shadow")


def test_glob_token_outside_sensitive_roots_is_allowed() -> None:
    assert not references_sensitive_path("/bin/ls /tmp/*")
    assert not references_sensitive_path("/bin/ls /var/log/*.log")
    assert not references_sensitive_path("/bin/ls /home/user/*")


def test_bare_relative_glob_resolves_against_working_directory() -> None:
    # `shado?` under /etc expands to /etc/shadow; the word has no slash or dot,
    # so it must be extracted on the strength of its metacharacter alone.
    assert references_sensitive_path("/bin/cat shado?", working_directory="/etc")
    assert references_sensitive_path("/bin/grep -r . ke*", working_directory="/app")
    globs = normalized_glob_references("/bin/cat shado?", working_directory="/etc")
    assert globs == ["/etc/shado?"]
    assert not references_sensitive_path("/bin/ls *.log", working_directory="/tmp/work")


def test_relative_glob_falls_back_to_absolute_target_resource() -> None:
    assert references_sensitive_path("/bin/cat shado?", target_resource="/etc")
    assert normalized_glob_references("/bin/ls *", target_resource="/etc") == ["/etc/*"]


def test_unresolved_relative_glob_is_dropped() -> None:
    # Without a directory to resolve against, a relative pattern cannot be
    # compared with absolute roots; `*` must not match `/etc/shadow`.
    for command in ("/bin/ls ./*", "/bin/ls */shadow", "/bin/ls ?etc/shadow", "/bin/ls ./../*"):
        assert normalized_glob_references(command) == []
        assert not references_sensitive_path(command)


def test_quoted_and_escaped_metacharacters_are_literal() -> None:
    assert not references_sensitive_path("/bin/grep '.*' README.md", working_directory="/tmp")
    assert not references_sensitive_path('/bin/echo "/etc/shado?"', working_directory="/tmp")
    assert not references_sensitive_path("/bin/echo '.ss*'", working_directory="/tmp")
    assert not references_sensitive_path("/bin/echo /etc/shado\\?", working_directory="/tmp")
    literals = normalized_path_references("/bin/grep '.*' README.md", working_directory="/tmp")
    assert normalized_glob_references("/bin/grep '.*' README.md", working_directory="/tmp") == []
    assert "/tmp/.*" in literals


def test_partially_quoted_words_keep_active_metacharacters() -> None:
    """Quoting applies per character: an unquoted ``?`` still expands even when
    another part of the same word is quoted or escaped."""
    for command in (
        '/bin/cat "/etc/"shado?',
        "/bin/cat /etc/sha\\do?",
        '/bin/cat /etc/"shado"?',
    ):
        assert references_sensitive_path(command, working_directory="/tmp"), command
        assert normalized_glob_references(command, working_directory="/tmp") == ["/etc/shado?"], (
            command
        )


def test_mixed_quoting_in_one_word_does_not_disable_other_words() -> None:
    command = '/bin/echo "hello"world; /bin/cat /etc/shado?'
    assert references_sensitive_path(command, working_directory="/tmp")
    assert normalized_glob_references(command, working_directory="/tmp") == ["/etc/shado?"]
    relative = '/bin/echo "hello"world; /bin/cat shado?'
    assert references_sensitive_path(relative, working_directory="/etc", target_resource="/etc")
    assert normalized_glob_references(relative, working_directory="/etc") == ["/etc/shado?"]


def test_brace_alternatives_expand_before_matching() -> None:
    assert references_sensitive_path("/bin/ls {x,/etc/shadow}")
    assert references_sensitive_path("/bin/ls {/,x}etc/shadow")
    assert "/etc/shadow" in normalized_path_references("/bin/ls {x,/etc/shadow}")
    literals = normalized_path_references("/bin/echo /tmp/a{b,c}")
    assert literals == ["/bin/echo", "/tmp/ab", "/tmp/ac"]
    assert normalized_glob_references("/bin/ls /tmp/{a,b}*") == ["/tmp/a*", "/tmp/b*"]


def test_glob_references_are_absolute_and_separate_from_literals() -> None:
    literals = normalized_path_references("/bin/ls /tmp/* ./notes.txt", working_directory="/work")
    globs = normalized_glob_references("/bin/ls /tmp/* ./notes.txt", working_directory="/work")

    assert literals == ["/bin/ls", "/work", "/work/notes.txt"]
    assert globs == ["/tmp/*"]


# One row per case from the PR #99 correctness review. `cwd` is passed as
# working_directory; target_resource is empty throughout.
_MATRIX = [
    ("/bin/cat //app/keys/council.key", "", True),
    ("/bin/cat ///etc/shadow", "", True),
    ("/bin/cat council.key", "//app/keys", True),
    ("/bin/cat ../keys/council.key", "//app/workspace", True),
    ("/bin/ls /tmp/*", "", False),
    ("/bin/ls /var/log/*.log", "", False),
    ("/bin/ls /home/user/*", "", False),
    ("/bin/cat /etc/shado?", "", True),
    ("/bin/ls /app/ke*", "", True),
    ("/bin/cat /app/keys/*", "", True),
    ("/bin/ls /root/.ss*", "", True),
    ("/bin/cat /e*/shadow", "", True),
    ("/bin/ls ./*", "", False),
    ("/bin/ls ./*", "/tmp", False),
    ("/bin/ls *", "/app", True),
    ("/bin/ls ke*", "/app", True),
    ("/bin/ls shado?", "/etc", True),
    ("/bin/ls .ss*", "/tmp", True),
    ("/bin/ls *.log", "/tmp", False),
    ("/bin/ls ./ke*", "/app", True),
    ('/bin/echo "/etc/shado?"', "/tmp", False),
    ("/bin/grep '.*' README.md", "/tmp", False),
    ("/bin/echo '.ss*'", "/tmp", False),
    ("/bin/echo /etc/shado\\?", "/tmp", False),
    ("/bin/cat ../etc/shadow", "//tmp", True),
    ("/bin/cat file.txt", "//tmp", False),
    ("/bin/cat /tmp/file.txt", "//tmp", False),
    ("/bin/ls {x,/etc/shadow}", "", True),
    ("/bin/ls /[e]tc/shadow", "", True),
    ("/bin/ls /[!x]tc/shadow", "", True),
    ("/bin/ls /[^x]tc/shadow", "", True),
    ("/bin/cat ./{keys,foo}/council.key", "/app", True),
    ("/bin/echo /tmp/a{b,c}", "", False),
    ("/bin/cat shadow", "//etc", False),
    ("/bin/cat ./shadow", "//etc", True),
    ("/bin/ls ./../*", "", False),
    ("/bin/ls {/,x}etc/shadow", "", True),
    ("/bin/ls {.ssh,x}", "", True),
    ("/bin/ls */shadow", "", False),
    ("/bin/ls ?etc/shadow", "", False),
    ("/bin/ls ./[!x]tc/shadow", "", False),
    ("/bin/ls ./{/etc/shadow,x}", "", False),
]


@pytest.mark.parametrize(("command", "cwd", "expected"), _MATRIX)
def test_review_matrix(command: str, cwd: str, expected: bool) -> None:
    assert references_sensitive_path(command, working_directory=cwd) is expected
