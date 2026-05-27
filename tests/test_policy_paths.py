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
