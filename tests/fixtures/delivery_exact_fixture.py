"""In-process exact NAT/MCP fixture; HTTPX ASGI transport, no sockets."""

import asyncio
import importlib.metadata as md
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvloop
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from nat.builder.function_info import FunctionInfo
from nat.builder.workflow_builder import WorkflowBuilder
from nat.cli.register_workflow import register_function
from nat.data_models.config import Config, GeneralConfig
from nat.data_models.function import FunctionBaseConfig
from nat.plugins.mcp.server.front_end_config import MCPFrontEndConfig
from nat.runtime.loader import PluginTypes, discover_and_register_plugins
from pydantic import BaseModel

from nomos.nat.delivery_trace import current_trace
from nomos.nat.functions import CouncilService
from nomos.nat.stateless_worker import StatelessMCPWorker

logging.disable(logging.CRITICAL)
discover_and_register_plugins(PluginTypes.FRONT_END)
seen = []


class Input(BaseModel):
    probe: bool = True


class FixtureConfig(FunctionBaseConfig, name="delivery_scope_fixture"):
    pass


@register_function(config_type=FixtureConfig)
async def registered(config, builder):
    async def fixture(input_data: Input) -> str:
        trace = current_trace()
        assert trace is not None
        seen.append(trace.http_id)

        async def fake_authorize(*args):
            return SimpleNamespace(
                status="approved", decision_id="intent-fixture", writ_id="writ-fixture"
            )

        async def run(call, kind):
            return await call

        service = SimpleNamespace(_authorize=fake_authorize, _run_workflow_request=run)
        result = await CouncilService.authorize(service, input_data)
        assert result.status == "approved"
        return '{"ok":true}'

    yield FunctionInfo.from_fn(
        fixture, input_schema=Input, description="Controlled in-process fixture"
    )


async def startup_failures():
    from unittest.mock import patch

    from nomos.nat.delivery_trace import DeliverySink
    from nomos.nat.stateless_worker import MCPFrontEndPluginWorker

    cases = 0
    for failure in (RuntimeError, asyncio.CancelledError):
        expected = failure("controlled route setup failure")

        async def bad_routes(*args, _expected=expected):
            raise _expected

        worker = StatelessMCPWorker.__new__(StatelessMCPWorker)
        sink = DeliverySink(os.environ["NOMOS_DELIVERY_TRACE_DIRECTORY"])
        worker._delivery_sink = sink
        with patch.object(MCPFrontEndPluginWorker, "add_routes", bad_routes):
            try:
                await worker.add_routes(None, None)
            except BaseException as exc:
                assert exc is expected
            else:
                raise AssertionError("route failure was lost")
        assert sink.closed.is_set() and sink.file.closed and not sink.thread.is_alive()
        assert worker._delivery_sink is None
        cases += 1
    for where in ("factory", "middleware"):
        expected = RuntimeError("controlled app setup failure")

        class App:
            def add_middleware(self, *args, _expected=expected, **kwargs):
                raise _expected

        class MCP:
            settings = SimpleNamespace(stateless_http=False)

            def streamable_http_app(self, _where=where, _expected=expected, _app=App):
                if _where == "factory":
                    raise _expected
                return _app()

        async def create(*args, _mcp=MCP):
            return _mcp()

        worker = StatelessMCPWorker.__new__(StatelessMCPWorker)
        with patch.object(MCPFrontEndPluginWorker, "create_mcp_server", create):
            mcp = await worker.create_mcp_server()
        sink = worker._delivery_sink
        try:
            mcp.streamable_http_app()
        except BaseException as exc:
            assert exc is expected
        else:
            raise AssertionError("app setup failure was lost")
        assert sink.closed.is_set() and sink.file.closed and not sink.thread.is_alive()
        assert worker._delivery_sink is None
        cases += 1
    return cases


async def main():
    startup_cases = await startup_failures()
    config = Config(
        general=GeneralConfig(front_end=MCPFrontEndConfig(host="127.0.0.1", port=8000)),
        functions={"read_value": FixtureConfig()},
        workflow=FixtureConfig(),
    )
    async with WorkflowBuilder.from_config(config=config) as builder:
        worker = StatelessMCPWorker(config)
        mcp = await worker.create_mcp_server()
        await worker.add_routes(mcp, builder)
        app = mcp.streamable_http_app()

        async def request():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                headers={
                    "X-Nomos-Stress-Request-ID": "abcdef012345:7",
                    "Authorization": "Bearer CANARY_HEADER_SECRET",
                },
            ) as client:
                async with streamable_http_client(
                    "http://localhost:8000/mcp", http_client=client
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool("read_value", {})
                        assert not result.isError
                        assert json.loads(result.content[0].text) == {"ok": True}

        async with mcp.session_manager.run():
            await asyncio.wait_for(asyncio.gather(request(), request()), timeout=10)
        counts = worker._delivery_sink.close()
        events = [json.loads(line) for line in worker._delivery_sink.path.read_text().splitlines()]
    approved = [e for e in events if e["event"] == "authorize_output_ready"]
    assert len(approved) == 2 and len(set(seen)) == 2
    assert {e["http_trace_id"] for e in approved} == set(seen)
    assert all(e["logical_id"] == "abcdef012345:7" for e in approved)
    assert all(e["intent_id"] == "intent-fixture" for e in approved)
    call = [e for e in events if e["event"] == "mcp_handler_return" and e["method"] == "tools/call"]
    lists = [
        e for e in events if e["event"] == "mcp_handler_return" and e["method"] == "tools/list"
    ]
    assert len(call) == len(lists) == 2
    assert {e["http_trace_id"] for e in call} == set(seen)
    assert not set(seen).intersection(e["http_trace_id"] for e in lists)
    assert len({e["rpc_id"] for e in call}) == 1
    assert not counts["dropped"] and not counts["abandoned"] and not counts["pending"]
    assert not counts["write_failures"] and not counts["writer_alive"]
    assert "CANARY_HEADER_SECRET" not in json.dumps(events)
    assert not any(e.get("trace_dropped", 0) or e.get("observer_failures", 0) for e in events)
    report = {
        "passed": True,
        "client_results": 2,
        "authorize_output_events": 2,
        "authorize_http_traces": len(set(seen)),
        "total_http_traces": len(
            {e["http_trace_id"] for e in events if e["event"] == "http_enter"}
        ),
        "trace_file": worker._delivery_sink.path.name,
        "startup_failure_cases": startup_cases,
        "actual_nat_registration": True,
        "public_authorize_boundary": True,
        "business_implementation": "controlled fixture",
        "network": "none; HTTPX ASGITransport only",
        "versions": {
            name: md.version(name)
            for name in [
                "mcp",
                "nvidia-nat-core",
                "nvidia-nat-mcp",
                "httpx",
                "sse-starlette",
                "uvicorn",
            ]
        },
        "loop": type(asyncio.get_running_loop()).__module__
        + "."
        + type(asyncio.get_running_loop()).__name__,
        "sink": counts,
    }
    Path(os.environ["NOMOS_DELIVERY_TRACE_DIRECTORY"], "fixture-result.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report))


uvloop.run(main())
