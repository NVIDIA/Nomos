"""Tests for the OpenShell executor scaffolding."""

import base64
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import executors.openshell.main as openshell_main
from executors.openshell.client import OpenShellClient
from executors.openshell.config import OpenShellExecutorConfig
from executors.openshell.enforcer import OpenShellEnforcer
from executors.openshell.runtime import create_server as create_runtime_server
from trusted_executor_contract import ExecutionResult, WritOfExecution


class _OpenShellHandler(BaseHTTPRequestHandler):
    seen_payload: dict | None = None

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.__class__.seen_payload = json.loads(self.rfile.read(length))
        body = json.dumps(
            {
                "exit_code": 0,
                "stdout": "ok\n",
                "stderr": "",
                "shell": False,
                "argv": ["echo", "hello"],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def openshell_backend():
    _OpenShellHandler.seen_payload = None
    server = HTTPServer(("127.0.0.1", 0), _OpenShellHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", _OpenShellHandler
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def openshell_runtime(tmp_path):
    server = create_runtime_server("127.0.0.1", 0, [str(tmp_path)])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", tmp_path
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _post_json(url: str, body: dict) -> dict:
    req = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
        return json.loads(resp.read())


def _get_json(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post_raw(url: str, body: bytes) -> tuple[int, dict]:
    req = urllib.request.Request(  # noqa: S310
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _writ(**overrides) -> WritOfExecution:
    now = time.time()
    data = {
        "writ_id": "writ-openshell",
        "intent_hash": "ab" * 32,
        "command": "echo hello",
        "working_directory": "/workspace",
        "target_agent_id": "agent-1",
        "issued_at": now - 1,
        "expires_at": now + 60,
        "max_executions": 1,
        "nonce": "cd" * 32,
        "council_id": "council-prod",
        "threshold_used": "unanimous",
        "chairman_risk_level": "low",
    }
    data.update(overrides)
    return WritOfExecution(**data)


def test_openshell_config_loads_backend_section(tmp_path, monkeypatch):
    config = tmp_path / "executor-openshell.yaml"
    config.write_text(
        "executor:\n"
        "  listen_port: 9906\n"
        "openshell:\n"
        "  endpoint: http://openshell.test:8080\n"
        "  request_timeout: 12\n"
    )
    monkeypatch.setenv("EXECUTOR_CONFIG", str(config))

    loaded = OpenShellExecutorConfig.load()

    assert loaded.listen_port == 9906
    assert loaded.openshell_endpoint == "http://openshell.test:8080"
    assert loaded.openshell_request_timeout == 12


@pytest.mark.asyncio
async def test_openshell_client_posts_command_payload(openshell_backend):
    endpoint, handler = openshell_backend
    client = OpenShellClient(endpoint, request_timeout=5)

    result = await client.execute(
        command="echo hello",
        working_directory="/workspace",
        timeout_seconds=5,
        packaged_files=[],
        metadata={"writ_id": "writ-1"},
    )

    assert result.exit_code == 0
    assert result.stdout == "ok\n"
    assert result.shell is False
    assert result.argv == ["echo", "hello"]
    assert handler.seen_payload["command"] == "echo hello"
    assert handler.seen_payload["working_directory"] == "/workspace"
    assert handler.seen_payload["metadata"]["writ_id"] == "writ-1"


def test_openshell_runtime_executes_command(openshell_runtime):
    endpoint, workspace = openshell_runtime

    result = _post_json(
        f"{endpoint}/v1/execute",
        {
            "command": "python3 -c 'print(42)'",
            "working_directory": str(workspace),
            "timeout_seconds": 5,
            "packaged_files": [],
            "metadata": {"writ_id": "writ-1"},
        },
    )

    assert result["exit_code"] == 0
    assert result["stdout"] == "42\n"
    assert result["shell"] is True
    assert result["argv"] == []


def test_openshell_runtime_server_accepts_parallel_requests(openshell_runtime):
    endpoint, workspace = openshell_runtime

    def execute(index: int) -> dict:
        return _post_json(
            f"{endpoint}/v1/execute",
            {
                "command": f"python3 -c 'print({index})'",
                "working_directory": str(workspace),
                "timeout_seconds": 5,
                "packaged_files": [],
                "metadata": {"writ_id": f"writ-parallel-{index}"},
            },
        )

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(execute, range(40)))

    assert [item["exit_code"] for item in results] == [0] * 40
    assert sorted(int(item["stdout"].strip()) for item in results) == list(range(40))


def test_openshell_runtime_runs_bash_and_python_in_workspace(openshell_runtime):
    endpoint, workspace = openshell_runtime
    command = (
        "bash -lc 'python3 -c "
        '"import os; print(os.environ[\\"OPENSHELL_RUNTIME\\"]); '
        'print(os.getcwd()); print(os.environ[\\"HOME\\"]); print(os.environ[\\"TMPDIR\\"])"'
        "'"
    )

    result = _post_json(
        f"{endpoint}/v1/execute",
        {
            "command": command,
            "working_directory": str(workspace),
            "timeout_seconds": 5,
            "packaged_files": [],
            "metadata": {"writ_id": "writ-bash-python"},
        },
    )

    assert result["exit_code"] == 0
    assert result["stdout"] == f"1\n{workspace}\n{workspace}\n{workspace / '.tmp'}\n"
    assert result["shell"] is True


def test_openshell_runtime_rejects_working_directory_outside_scope(openshell_runtime):
    endpoint, workspace = openshell_runtime

    result = _post_json(
        f"{endpoint}/v1/execute",
        {
            "command": "python3 -c 'print(42)'",
            "working_directory": str(workspace.parent),
            "timeout_seconds": 5,
            "packaged_files": [],
            "metadata": {"writ_id": "writ-outside"},
        },
    )

    assert result["exit_code"] is None
    assert result["error"] == f"Working directory not allowed: {workspace.parent}"


def test_openshell_runtime_materializes_packaged_files(openshell_runtime):
    endpoint, workspace = openshell_runtime
    content = b"hello from package\n"

    result = _post_json(
        f"{endpoint}/v1/execute",
        {
            "command": "cat packaged.txt",
            "working_directory": str(workspace),
            "timeout_seconds": 5,
            "packaged_files": [
                {
                    "path": "packaged.txt",
                    "content_base64": base64.b64encode(content).decode(),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "mode": "0600",
                }
            ],
            "metadata": {"writ_id": "writ-2"},
        },
    )

    assert result["exit_code"] == 0
    assert result["stdout"] == "hello from package\n"
    assert (workspace / "packaged.txt").read_bytes() == content


def test_openshell_runtime_get_health_and_not_found(openshell_runtime):
    endpoint, workspace = openshell_runtime

    status, health = _get_json(f"{endpoint}/healthz")
    missing_status, missing = _get_json(f"{endpoint}/missing")

    assert status == 200
    assert health == {"ok": True, "allowed_working_dirs": [str(workspace)]}
    assert missing_status == 404
    assert missing == {"error": "not_found"}


def test_openshell_runtime_rejects_invalid_request_bodies(openshell_runtime):
    endpoint, _workspace = openshell_runtime

    empty_status, empty = _post_raw(f"{endpoint}/v1/execute", b"")
    invalid_status, invalid = _post_raw(f"{endpoint}/v1/execute", b"{not-json")
    missing_status, missing = _post_raw(f"{endpoint}/v1/missing", b"{}")

    assert empty_status == 400
    assert empty == {"error": "Empty request body"}
    assert invalid_status == 400
    assert invalid["error"].startswith("Invalid JSON:")
    assert missing_status == 404
    assert missing == {"error": "not_found"}


@pytest.mark.parametrize(
    ("payload_updates", "error_prefix"),
    [
        ({"command": ""}, "Command is empty"),
        ({"command": "echo \x00"}, "Command contains null byte"),
        ({"working_directory": "/workspace/does-not-exist"}, "Working directory not allowed"),
        (
            {"packaged_files": [{"path": "", "content_base64": ""}]},
            "Packaged file path is invalid",
        ),
        (
            {"packaged_files": [{"path": "../escape.txt", "content_base64": ""}]},
            "Packaged file path escapes working directory",
        ),
        (
            {"packaged_files": [{"path": "bad.txt", "content_base64": "not-base64"}]},
            "Packaged file 'bad.txt' failed base64 decode",
        ),
        (
            {
                "packaged_files": [
                    {
                        "path": "hash.txt",
                        "content_base64": base64.b64encode(b"content").decode(),
                        "sha256": "00" * 32,
                    }
                ]
            },
            "Packaged file 'hash.txt' hash mismatch",
        ),
    ],
)
def test_openshell_runtime_rejects_invalid_payloads(
    openshell_runtime,
    payload_updates,
    error_prefix,
):
    endpoint, workspace = openshell_runtime
    payload = {
        "command": "echo ok",
        "working_directory": str(workspace),
        "timeout_seconds": 5,
        "packaged_files": [],
        "metadata": {},
    }
    payload.update(payload_updates)

    result = _post_json(f"{endpoint}/v1/execute", payload)

    assert result["exit_code"] is None
    assert result["error"].startswith(error_prefix)


def test_openshell_runtime_reports_timeout_and_missing_binary(openshell_runtime):
    endpoint, workspace = openshell_runtime

    timeout = _post_json(
        f"{endpoint}/v1/execute",
        {
            "command": "python3 -c 'import time; time.sleep(1)'",
            "working_directory": str(workspace),
            "timeout_seconds": 0,
            "packaged_files": [],
            "metadata": {},
        },
    )
    missing = _post_json(
        f"{endpoint}/v1/execute",
        {
            "command": "definitely-not-a-real-command",
            "working_directory": str(workspace),
            "timeout_seconds": 5,
            "packaged_files": [],
            "metadata": {},
        },
    )

    assert timeout["error"] == "Command timed out after 0s"
    assert timeout["shell"] is True
    assert missing["error"].startswith("Execution failed:")
    assert missing["shell"] is False
    assert missing["argv"] == ["definitely-not-a-real-command"]


class _FakeOpenShell:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


@pytest.fixture
def openshell_enforcer(tmp_path):
    nonce_store = SimpleNamespace(report_outcome=AsyncMock())
    config = OpenShellExecutorConfig(
        allowed_working_directories=[str(tmp_path)],
        max_command_timeout=30,
        openshell_endpoint="http://openshell.test",
        openshell_request_timeout=5,
    )
    enforcer = OpenShellEnforcer(
        public_key=b"k" * 32,
        nonce_store=nonce_store,
        config=config,
    )
    return enforcer, nonce_store


@pytest.mark.asyncio
async def test_openshell_enforcer_delegates_and_reports_outcome(openshell_enforcer):
    enforcer, nonce_store = openshell_enforcer
    enforcer._openshell = _FakeOpenShell(
        SimpleNamespace(
            exit_code=0,
            stdout="ok\n",
            stderr="",
            error=None,
            shell=False,
            argv=["echo", "ok"],
        )
    )

    result = await enforcer._execute_command(_writ(command="echo ok"))

    assert result == ExecutionResult(
        writ_id="writ-openshell",
        success=True,
        exit_code=0,
        stdout="ok\n",
        stderr="",
        duration_seconds=result.duration_seconds,
        verification_stage="passed",
        executed_command_shell=False,
        executed_command_argv=["echo", "ok"],
    )
    assert enforcer._openshell.calls[0]["metadata"] == {
        "writ_id": "writ-openshell",
        "intent_hash": "ab" * 32,
        "target_agent_id": "agent-1",
        "council_id": "council-prod",
    }
    nonce_store.report_outcome.assert_awaited_once()


@pytest.mark.asyncio
async def test_openshell_enforcer_returns_execution_error(openshell_enforcer):
    enforcer, nonce_store = openshell_enforcer
    enforcer._openshell = _FakeOpenShell(
        SimpleNamespace(  # noqa: S604 - response field, not process execution
            exit_code=None,
            stdout="",
            stderr="boom",
            error="OpenShell request failed",
            shell=True,
            argv=[],
        )
    )

    result = await enforcer._execute_command(_writ())

    assert result.success is False
    assert result.error == "OpenShell request failed"
    assert result.stderr == "boom"
    assert result.verification_stage == "execution"
    nonce_store.report_outcome.assert_not_called()


@pytest.mark.asyncio
async def test_openshell_enforcer_rejects_expired_writ_before_delegation(openshell_enforcer):
    enforcer, _nonce_store = openshell_enforcer
    enforcer._openshell = _FakeOpenShell(SimpleNamespace(error=None))

    result = await enforcer._execute_command(_writ(expires_at=time.time() - 1))

    assert result.error == "Writ expired before execution could start"
    assert result.verification_stage == "temporal"
    assert enforcer._openshell.calls == []
    assert enforcer._unpack_packaged_files(_writ()) is None


def test_openshell_main_exits_when_public_key_load_fails(monkeypatch):
    monkeypatch.setattr(
        openshell_main.OpenShellExecutorConfig,
        "load",
        classmethod(lambda cls: OpenShellExecutorConfig(log_level="INFO")),
    )
    monkeypatch.setattr(
        openshell_main,
        "_load_public_key",
        lambda config: (_ for _ in ()).throw(RuntimeError("missing key")),
    )

    with pytest.raises(SystemExit) as exc:
        openshell_main.main()

    assert exc.value.code == 1


def test_openshell_main_exits_when_redis_connect_fails(monkeypatch):
    monkeypatch.setattr(
        openshell_main.OpenShellExecutorConfig,
        "load",
        classmethod(lambda cls: OpenShellExecutorConfig(log_level="INFO")),
    )
    monkeypatch.setattr(openshell_main, "_load_public_key", lambda config: b"k" * 32)
    monkeypatch.setattr(
        openshell_main,
        "build_executor_identity",
        lambda **kwargs: SimpleNamespace(key_fingerprint="f" * 64),
    )
    monkeypatch.setattr(
        openshell_main,
        "connect_nonce_store_with_retries",
        lambda loop, store: (_ for _ in ()).throw(RuntimeError("redis down")),
    )

    with pytest.raises(SystemExit) as exc:
        openshell_main.main()

    assert exc.value.code == 1


def test_openshell_main_starts_server_and_closes_nonce_store(monkeypatch):
    closed = False

    class FakeNonceStore:
        def __init__(self, redis_url, redis_key_prefix, **kwargs):
            self.redis_url = redis_url
            self.redis_key_prefix = redis_key_prefix
            self.redis_kwargs = kwargs
            self.registration = None

        async def register_trusted_executor(self, **kwargs):
            self.registration = kwargs

        async def close(self):
            nonlocal closed
            closed = True

    class FakeServer:
        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(
        openshell_main.OpenShellExecutorConfig,
        "load",
        classmethod(
            lambda cls: OpenShellExecutorConfig(
                redis_url="redis://redis:6379/0",
                redis_key_prefix="nomos:",
                log_level="INFO",
            )
        ),
    )
    monkeypatch.setattr(openshell_main, "_load_public_key", lambda config: b"k" * 32)
    monkeypatch.setattr(
        openshell_main,
        "build_executor_identity",
        lambda **kwargs: SimpleNamespace(
            key_fingerprint="f" * 64,
            public_key_hex="a" * 64,
        ),
    )
    monkeypatch.setattr(openshell_main, "NonceStore", FakeNonceStore)
    monkeypatch.setattr(
        openshell_main, "connect_nonce_store_with_retries", lambda loop, store: None
    )
    monkeypatch.setattr(
        openshell_main,
        "OpenShellEnforcer",
        lambda public_key, nonce_store, config: SimpleNamespace(),
    )
    monkeypatch.setattr(openshell_main, "create_server", lambda *args, **kwargs: FakeServer())

    openshell_main.main()

    assert closed is True
