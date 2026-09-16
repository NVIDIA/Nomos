# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


from nomos.level_1.policy.paths import normalized_path_references, references_sensitive_path


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
