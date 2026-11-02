# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Capture real reader inputs from synthetic context-API fixtures over verified TLS.

This is a component test, not a Hermes run. Council reviews and tool events are
reported fixtures, not live Council authorization or executed tool callbacks.
The local recording model returns fixed scores solely to capture the reader's
request. Those scores are never benchmark results; run_pilot.py replays the
captured states against the real decision-model endpoint.

Run in the existing Relay test environment (grpcio, PyJWT, cryptography, httpx).
Use a separate context-service Python environment and a built reader binary.
Generated credentials/private keys live only in a temporary private directory.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import importlib
import importlib.metadata
import json
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import grpc
import httpx
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.x509.oid import NameOID
from google.protobuf import json_format, struct_pb2


class RecordingModel(BaseHTTPRequestHandler):
    """Capture outgoing requests; fixed answers only release the gRPC fixture call."""

    requests: ClassVar[list[dict]] = []

    def do_POST(self):
        """Accept bounded local JSON and answer every configured question consistently."""
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 512 * 1024:
            self.send_error(413)
            return
        payload = json.loads(self.rfile.read(size))
        self.requests.append(payload)
        body = json.dumps(
            {
                "model": "context-capture-fixture",
                "answers": {key: {"type": "noul", "noul": 0.1} for key in payload["questions"]},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        """Suppress HTTP logging; synthetic payload capture is saved explicitly."""


def free_port() -> int:
    """Reserve an available loopback port without assuming the lab ports are free."""
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def write_private(path: Path, value: bytes) -> Path:
    """Create ephemeral credential material with owner-only file permissions."""
    with path.open("xb") as stream:
        path.chmod(0o600)
        stream.write(value)
    return path


def make_tls(directory: Path) -> tuple[Path, Path, Path]:
    """Issue a short-lived local certificate with a verified loopback SAN."""
    now = dt.datetime.now(dt.UTC)
    authority_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Context format fixture CA")])
    authority = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(authority_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(authority_key, hashes.SHA256())
    )
    service_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    import ipaddress

    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(name)
        .public_key(service_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(authority_key, hashes.SHA256())
    )
    ca = write_private(directory / "ca.pem", authority.public_bytes(serialization.Encoding.PEM))
    cert = write_private(
        directory / "service.pem", certificate.public_bytes(serialization.Encoding.PEM)
    )
    key = write_private(
        directory / "service.key",
        service_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    return ca, cert, key


def main() -> None:
    """Run isolated authenticated components and save model states, not fixture decisions."""
    parser = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve().parent
    parser.add_argument("--cases", type=Path, default=here / "cases.json")
    parser.add_argument("--questions", type=Path, default=here / "questions.json")
    parser.add_argument("--context-python", type=Path, required=True)
    parser.add_argument("--reader-binary", type=Path, required=True)
    parser.add_argument("--proto-dir", type=Path, required=True)
    parser.add_argument("--protoc", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("capture output must be new or empty; do not overwrite evidence")
    args.output.mkdir(parents=True, exist_ok=True)
    cases = json.loads(args.cases.read_text())["cases"]
    catalog = json.loads(args.questions.read_text())["catalogs"]["baseline"]
    RecordingModel.requests = []
    model = ThreadingHTTPServer(("127.0.0.1", 0), RecordingModel)
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    processes, logs = [], []
    states, lookups, checks = {}, {}, []
    try:
        with tempfile.TemporaryDirectory(prefix="nomos-context-format-") as temporary:
            private = Path(temporary)
            ca, cert, key = make_tls(private)
            publisher = secrets.token_urlsafe(40)
            reader = secrets.token_urlsafe(40)
            pubfile = write_private(private / "publisher.token", publisher.encode())
            readfile = write_private(private / "reader.token", reader.encode())
            signer = ed25519.Ed25519PrivateKey.generate()
            public = signer.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
            jwks = write_private(
                private / "jwks.json",
                json.dumps(
                    {
                        "keys": [
                            {
                                "kty": "OKP",
                                "crv": "Ed25519",
                                "kid": "fixture",
                                "alg": "EdDSA",
                                "use": "sig",
                                "x": base64.urlsafe_b64encode(public).rstrip(b"=").decode(),
                            }
                        ]
                    }
                ).encode(),
            )
            subprocess.run(
                [
                    str(args.protoc),
                    "-I" + str(args.proto_dir),
                    "-I" + str(args.protoc.parent.parent / "include"),
                    "--python_out=" + str(private),
                    str(args.proto_dir / "extension.proto"),
                    str(args.proto_dir / "supervisor_middleware.proto"),
                ],
                check=True,
            )
            sys.path.insert(0, str(private))
            proto = importlib.import_module("supervisor_middleware_pb2")
            extension = importlib.import_module("extension_pb2")

            def start(command: list[str], name: str):
                """Launch only this run's processes; never restart an existing lab service."""
                stream = (args.output / name).open("w")
                logs.append(stream)
                process = subprocess.Popen(
                    command,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    env={"PATH": "/usr/local/bin:/usr/bin:/bin", "RUST_LOG": "info"},
                )
                processes.append(process)
                return process

            context_port, rpc_port = free_port(), free_port()
            context_url = f"https://127.0.0.1:{context_port}"
            context = start(
                [
                    str(args.context_python),
                    "-c",
                    "from nomos_context_service.server import main; main()",
                    "--port",
                    str(context_port),
                    "--publisher-token-file",
                    str(pubfile),
                    "--reader-token-file",
                    str(readfile),
                    "--tls-cert-file",
                    str(cert),
                    "--tls-key-file",
                    str(key),
                ],
                "context-capture-server.log",
            )
            verify = ssl.create_default_context(cafile=str(ca))
            with httpx.Client(verify=verify, timeout=3, trust_env=False) as client:
                last_status = None
                for _ in range(200):
                    if context.poll() is not None:
                        raise RuntimeError("context service exited; inspect capture server log")
                    try:
                        last_status = client.get(context_url + "/health").status_code
                        if last_status == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.05)
                else:
                    raise RuntimeError(
                        f"context service readiness deadline exceeded (HTTP {last_status})"
                    )
                start(
                    [
                        str(args.reader_binary),
                        "--bind",
                        f"127.0.0.1:{rpc_port}",
                        "--model-endpoint",
                        f"http://127.0.0.1:{model.server_port}/v1/systemone",
                        "--model-id",
                        "context-capture-fixture",
                        "--allow-http-model",
                        "--tls-cert-file",
                        str(cert),
                        "--tls-key-file",
                        str(key),
                        "--extension-jwks-file",
                        str(jwks),
                        "--extension-gateway-id",
                        "format-fixture",
                        "--extension-audience",
                        "urn:nomos:context-format-fixture",
                        "--context-lookup-mode",
                        "best_effort",
                        "--context-service-url",
                        context_url,
                        "--context-ca-cert-file",
                        str(ca),
                        "--context-service-token-file",
                        str(readfile),
                        "--context-allowed-workspace",
                        "default",
                    ],
                    "context-capture-reader.log",
                )
                channel = grpc.secure_channel(
                    f"127.0.0.1:{rpc_port}",
                    grpc.ssl_channel_credentials(root_certificates=ca.read_bytes()),
                )
                grpc.channel_ready_future(channel).result(timeout=10)
                evaluate = channel.unary_unary(
                    "/openshell.middleware.v1.SupervisorMiddleware/EvaluateHttpRequest",
                    request_serializer=proto.HttpRequestEvaluation.SerializeToString,
                    response_deserializer=proto.HttpRequestResult.FromString,
                )
                describe = channel.unary_unary(
                    "/openshell.middleware.v1.SupervisorMiddleware/Describe",
                    request_serializer=proto.MiddlewareDescribeRequest.SerializeToString,
                    response_deserializer=proto.MiddlewareManifest.FromString,
                )
                config = json_format.ParseDict(catalog, struct_pb2.Struct())
                current = int(time.time())
                deadline = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=240)).isoformat()

                def credential(sandbox_id: str) -> list[tuple[str, str]]:
                    """Sign fixture supervisor claims bound to this gRPC request's sandbox."""
                    token = jwt.encode(
                        {
                            "iss": "openshell-gateway:format-fixture",
                            "aud": "urn:nomos:context-format-fixture",
                            "sub": "spiffe://openshell/sandbox/" + sandbox_id,
                            "iat": current,
                            "exp": current + 300,
                            "jti": "fixture-reusable",
                            "caller_kind": "supervisor",
                            "sandbox_id": sandbox_id,
                        },
                        signer,
                        algorithm="EdDSA",
                        headers={"typ": "openshell-ext+jwt", "kid": "fixture"},
                    )
                    return [("authorization", "Bearer " + token)]

                metadata = extension.PeerMetadata(
                    protocol_version=extension.ProtocolVersion(major=1),
                    implementation_name="context-format-fixture",
                    implementation_version="1",
                    supported_capabilities=["openshell.supervisor-middleware.contract"],
                )
                manifest = describe(
                    proto.MiddlewareDescribeRequest(gateway=metadata),
                    metadata=credential("fixture-0"),
                    timeout=5,
                )
                assert manifest.expected_audience == "urn:nomos:context-format-fixture"
                checks.append("authenticated native gRPC TLS and expected audience")

                for index, case in enumerate(cases):
                    name = "context-format-" + case["id"]
                    reported = case["context"]
                    snapshot_id = reported["snapshot_ref"]
                    retained = dict(reported["retained_context"])
                    retained.setdefault("tool_definitions_kept", False)
                    retained.setdefault("tool_definitions_sha256", None)
                    snapshot = {
                        "sandbox_name": name,
                        "snapshot_id": snapshot_id,
                        "model_attempt_id": "attempt-current",
                        "model_name": "synthetic-agent-fixture",
                        "not_after": deadline,
                        "context": retained,
                    }
                    publications = [("context-snapshots", snapshot)]
                    # Publish oldest first so the real service returns newest first.
                    for record in reversed(reported["recent_tool_records"]):
                        publications.append(("tool-records", {"sandbox_name": name, **record}))
                    for endpoint, payload in publications:
                        wire = json.dumps(
                            payload, ensure_ascii=False, separators=(",", ":")
                        ).encode()
                        assert len(wire) <= 16 * 1024
                        response = client.post(
                            context_url + "/v1/" + endpoint,
                            content=wire,
                            headers={
                                "Authorization": "Bearer " + publisher,
                                "Content-Type": "application/json",
                            },
                        )
                        assert response.status_code == 201, (
                            case["id"],
                            endpoint,
                            response.status_code,
                        )
                    lookup = client.post(
                        context_url + "/v1/context-lookups",
                        headers={"Authorization": "Bearer " + reader},
                        json={
                            "sandbox_name": name,
                            "include_content": True,
                            "sandbox_id": "fixture-" + str(index),
                            "http_request_id": "capture-" + case["id"],
                        },
                    )
                    assert lookup.status_code == 200
                    lookups[case["id"]] = lookup.json()
                    http = case["http"]
                    request = proto.HttpRequestEvaluation(
                        phase=proto.SUPERVISOR_MIDDLEWARE_PHASE_PRE_CREDENTIALS,
                        context=proto.RequestContext(
                            request_id="capture-" + case["id"],
                            sandbox_id="fixture-" + str(index),
                            sandbox=name,
                            workspace="default",
                        ),
                        config=config,
                        middleware_name="nomos-request-review",
                        target=proto.HttpRequestTarget(
                            **{k: v for k, v in http.items() if k != "body"}
                        ),
                        headers=[proto.HttpHeader(name="content-type", value="application/json")],
                        body=json.dumps(http["body"], ensure_ascii=False).encode(),
                    )
                    before = len(RecordingModel.requests)
                    evaluate(request, metadata=credential("fixture-" + str(index)), timeout=5)
                    assert len(RecordingModel.requests) == before + 1
                    state = RecordingModel.requests[-1]["state"]
                    assert state["body"] == http["body"]
                    assert state["conversation_context"]["retained_context"] == retained
                    assert len(state["conversation_context"]["recent_tool_records"]) == len(
                        reported["recent_tool_records"]
                    )
                    states[case["id"]] = state
                    print("Captured real context-service/reader state:", case["id"], flush=True)
                # A genuine 404 is a plumbing check, not another format comparison.
                request.context.sandbox = "never-published-context-format-fixture"
                evaluate(request, metadata=credential("fixture-" + str(len(cases) - 1)), timeout=5)
                assert "conversation_context" not in RecordingModel.requests[-1]["state"]
                checks.append("missing context yields HTTP-only input in best_effort mode")
                channel.close()
            (args.output / "captured_states.json").write_text(
                json.dumps({"schema_version": 1, "states": states}, indent=2) + "\n"
            )
            (args.output / "lookup-responses.json").write_text(json.dumps(lookups, indent=2) + "\n")
            proof = {
                "capture_kind": "controlled_component_fixture",
                "capture_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "dependency_versions": {
                    name: importlib.metadata.version(name)
                    for name in ["grpcio", "protobuf", "cryptography", "PyJWT", "httpx"]
                },
                "hermes_executed": False,
                "live_council_review": False,
                "native_tls_verified": True,
                "context_api_https_verified": True,
                "case_count": len(states),
                "checks": checks,
                "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
                "reader_binary_sha256": hashlib.sha256(args.reader_binary.read_bytes()).hexdigest(),
                "captured_states_sha256": hashlib.sha256(
                    (args.output / "captured_states.json").read_bytes()
                ).hexdigest(),
            }
            (args.output / "capture-proof.json").write_text(json.dumps(proof, indent=2) + "\n")
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        model.shutdown()
        model.server_close()
        for stream in logs:
            stream.close()


if __name__ == "__main__":
    main()
