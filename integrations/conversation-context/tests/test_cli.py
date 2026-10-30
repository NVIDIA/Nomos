# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Keep deployment defaults authenticated, isolated and single-process."""

import shutil
import subprocess

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
        """Invoke startup with fixture credentials and retain captured launch options."""
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

    run.options = options
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


@pytest.fixture
def tls_pair(tmp_path):
    """Generate disposable PEM files without adding crypto dependencies."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("TLS fixture generation requires the OpenSSL executable")

    def generate(name):
        """Create a matching self-signed certificate and unencrypted private key."""
        cert, key = tmp_path / f"{name}.pem", tmp_path / f"{name}.key"
        subprocess.run(  # noqa: S603 - resolved executable and fixed test arguments.
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
            ],
            check=True,
            capture_output=True,
        )
        key.chmod(0o600)
        return cert, key

    return generate


def test_tls_paths_are_passed_to_uvicorn(cli, tls_pair):
    """A real matching certificate/key pair is validated before server startup."""
    run, _ = cli
    cert, key = tls_pair("valid")
    _, args = run("--host", "0.0.0.0", "--tls-cert-file", str(cert), "--tls-key-file", str(key))
    assert args["ssl_certfile"] == str(cert)
    assert args["ssl_keyfile"] == str(key)


@pytest.mark.parametrize(
    "invalid", ["missing_certificate", "missing_key", "mismatched", "malformed"]
)
def test_invalid_tls_material_is_sanitized_before_listening(cli, tls_pair, capsys, invalid):
    """Unreadable, invalid and mismatched TLS files produce one safe CLI error."""
    run, _ = cli
    cert, key = tls_pair("primary")
    if invalid == "missing_certificate":
        cert.unlink()
    elif invalid == "missing_key":
        key.unlink()
    elif invalid == "mismatched":
        _, key = tls_pair("other")
    else:
        key.write_text("malformed-private-material")
    with pytest.raises(SystemExit) as error:
        run("--tls-cert-file", str(cert), "--tls-key-file", str(key))
    assert error.value.code == 2
    text = capsys.readouterr().err
    assert "TLS certificate/key could not be loaded" in text
    assert str(cert) not in text and str(key) not in text
    assert "malformed-private-material" not in text
    assert PUBLISHER not in text and READER not in text
    assert run.options == []


def test_encrypted_tls_key_is_rejected_without_password_prompt(cli, tls_pair, capsys, monkeypatch):
    """An unattended process must never block waiting for a PEM password."""
    run, _ = cli
    cert, key = tls_pair("encrypted")
    encrypted = key.with_name("encrypted-private.key")
    openssl = shutil.which("openssl")
    subprocess.run(  # noqa: S603 - resolved executable and fixed synthetic password.
        [
            openssl,
            "pkcs8",
            "-topk8",
            "-in",
            str(key),
            "-out",
            str(encrypted),
            "-passout",
            "pass:test-only-password",
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("password prompt attempted"))
    with pytest.raises(SystemExit) as error:
        run("--tls-cert-file", str(cert), "--tls-key-file", str(encrypted))
    assert error.value.code == 2
    text = capsys.readouterr().err
    assert "TLS certificate/key could not be loaded" in text
    assert "test-only-password" not in text and str(encrypted) not in text
    assert run.options == []


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
