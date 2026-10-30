# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Keep deployment defaults authenticated, isolated and single-process."""

import pytest

from nomos_context_service import server

PUBLISHER = "test-only-publisher-credential-00001"
READER = "test-only-reader-credential-0000001"


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """Supply private synthetic token files and capture the server launch options."""
    paths = []
    for name, value in [("publisher", PUBLISHER), ("reader", READER)]:
        path = tmp_path / name
        path.write_text(value + "\n")
        path.chmod(0o600)
        paths.append(path)
    options = []
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kwargs: options.append((app, kwargs)))
    monkeypatch.delenv("NOMOS_CONTEXT_PUBLISHER_TOKEN_FILE", raising=False)
    monkeypatch.delenv("NOMOS_CONTEXT_READER_TOKEN_FILE", raising=False)

    def run(*extra):
        monkeypatch.setattr(
            "sys.argv",
            [
                "nomos-context-service",
                "--publisher-token-file",
                str(paths[0]),
                "--reader-token-file",
                str(paths[1]),
                *extra,
            ],
        )
        server.main()
        return options[-1]

    return run, paths


def test_cli_defaults_do_not_expose_or_duplicate_context(cli):
    """Loopback, one worker and no access/proxy logging are intentional defaults."""
    run, _ = cli
    _, args = run()
    assert args["host"] == "127.0.0.1" and args["port"] == 18083
    assert args["workers"] == 1
    assert args["access_log"] is False and args["proxy_headers"] is False


@pytest.mark.parametrize("host", ["0.0.0.0", "10.1.2.3", "context.example.com"])
def test_nonloopback_requires_explicit_transport_configuration(cli, host):
    """A hostname that happens to resolve locally is not implicitly trusted."""
    run, _ = cli
    with pytest.raises(SystemExit) as error:
        run("--host", host)
    assert error.value.code == 2


def test_insecure_lab_listener_is_a_deliberate_opt_in(cli):
    """The development escape hatch changes transport, never authentication."""
    run, _ = cli
    _, args = run("--host", "0.0.0.0", "--allow-insecure-lab")
    assert args["host"] == "0.0.0.0"


def test_tls_paths_are_passed_to_uvicorn(cli):
    """Server TLS remains available without requiring a separate proxy deployment."""
    run, _ = cli
    _, args = run(
        "--host", "0.0.0.0", "--tls-cert-file", "certificate.pem", "--tls-key-file", "private.pem"
    )
    assert args["ssl_certfile"] == "certificate.pem"
    assert args["ssl_keyfile"] == "private.pem"


@pytest.mark.parametrize(
    "extra", [["--tls-cert-file", "certificate.pem"], ["--port", "0"], ["--ttl-seconds", "nan"]]
)
def test_invalid_startup_settings_fail_before_listening(cli, extra):
    """Invalid TLS pairs, ports and retention cannot create a partial deployment."""
    run, _ = cli
    with pytest.raises(SystemExit) as error:
        run(*extra)
    assert error.value.code == 2


def test_missing_credential_files_do_not_leak_other_values(cli, capsys):
    """Unreadable credentials stop startup with a generic configuration error."""
    run, paths = cli
    paths[0].unlink()
    with pytest.raises(SystemExit):
        run()
    text = capsys.readouterr().err
    assert "credential file could not be read" in text
    assert PUBLISHER not in text and READER not in text


def test_equal_file_credentials_are_rejected(cli, capsys):
    """Separate roles stay separate when tokens are loaded through CLI files."""
    run, paths = cli
    paths[1].write_text(PUBLISHER)
    with pytest.raises(SystemExit):
        run()
    assert PUBLISHER not in capsys.readouterr().err


def test_token_file_paths_can_come_from_deployment_environment(cli, monkeypatch):
    """Only file paths, rather than bearer values, need environment configuration."""
    _, paths = cli
    monkeypatch.setenv("NOMOS_CONTEXT_PUBLISHER_TOKEN_FILE", str(paths[0]))
    monkeypatch.setenv("NOMOS_CONTEXT_READER_TOKEN_FILE", str(paths[1]))
    monkeypatch.setattr("sys.argv", ["nomos-context-service"])
    server.main()


def test_missing_file_settings_fail_closed(monkeypatch):
    """The executable has no development credentials or unauthenticated default."""
    monkeypatch.delenv("NOMOS_CONTEXT_PUBLISHER_TOKEN_FILE", raising=False)
    monkeypatch.delenv("NOMOS_CONTEXT_READER_TOKEN_FILE", raising=False)
    monkeypatch.setattr("sys.argv", ["nomos-context-service"])
    with pytest.raises(SystemExit) as error:
        server.main()
    assert error.value.code == 2
