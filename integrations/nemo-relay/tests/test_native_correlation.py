# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Native request/response correlation: no model-carried token and no latest-request guess."""

import asyncio
import json
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from types import SimpleNamespace

import pytest
from nemo_relay_plugin import ScopeStackEntry, ScopeStackSnapshot, ScopeType
from support import INTENT, LOCAL_TOOL, Context, Council, Next, base_config, call

from nemo_relay_nomos import NomosPlugin
from nemo_relay_nomos.native_context import NativeContextError
from nemo_relay_nomos.repository import StoreError

TOOL = "read_file"


class Runtime:
    """Model the SDK's task-local native scope binding, not any model arguments."""

    def __init__(self):
        """Initialize isolated fixture state for native ownership and callback observations."""
        self.root = str(uuid.uuid4())
        self.single_agent_root = False
        self.current = ContextVar("native-test-agent", default=None)

    @contextmanager
    def bind(self, agent):
        """Keep a synthetic application Agent identity local to this asynchronous test context."""
        token = self.current.set(agent)
        try:
            yield
        finally:
            self.current.reset(token)

    async def current_scope_stack_snapshot(self):
        """Expose native root and application Agent scopes without consulting model text."""
        agent = self.current.get()
        if agent is None:
            return None
        entries = [ScopeStackEntry(agent, ScopeType.AGENT)]
        if not self.single_agent_root:
            entries.insert(0, ScopeStackEntry(self.root, ScopeType.AGENT))
        return ScopeStackSnapshot(entries)


class NativeContext(Context):
    def __init__(self):
        """Initialize isolated fixture state for native ownership and callback observations."""
        super().__init__()
        self.runtime = Runtime()

    def register_llm_execution_intercept(self, name, callback, **kw):
        """Capture the unary execution callback and its registration priority."""
        self.hooks[name] = (callback, kw)

    def register_llm_stream_execution_intercept(self, name, callback, **kw):
        """Capture the streaming execution callback and its registration priority."""
        self.hooks[name] = (callback, kw)


class RequestCodec:
    async def decode(self, request):
        """Return decoded fixture content using the host codec interface."""
        return deepcopy(request["content"])

    async def encode(self, annotated, original):
        """Apply prepared request content while retaining the original host envelope."""
        return {**original, "content": annotated}


class ResponseCodec:
    async def decode(self, response):
        """Return decoded fixture content using the host codec interface."""
        return response


def codecs(api="openai_chat", request=True, response=True):
    """Supply host request and response codecs, including deliberately missing-codec cases."""
    return SimpleNamespace(
        request_codec=SimpleNamespace(
            codec=SimpleNamespace(id=api), resolve_codec=lambda: RequestCodec() if request else None
        ),
        response_codec=SimpleNamespace(resolve_codec=lambda: ResponseCodec()) if response else None,
    )


def request(text="Read the file"):
    """Build a model request offering the local file tool and the supplied user context."""
    return {
        "headers": {},
        "content": {"messages": [{"role": "user", "content": text}], "tools": [deepcopy(LOCAL_TOOL)]},
    }


def response(*ids):
    """Build completed provider proposals with stable IDs for origin-association tests."""
    return {"tool_calls": [{"id": value, "name": TOOL, "arguments": {"path": "a"}} for value in ids]}


def chunks(call_id="call_stream"):
    """Split a provider tool proposal across chunks before its terminal completion."""
    return [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": call_id, "function": {"name": TOOL, "arguments": "{"}}
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ]


class Model:
    def __init__(self, value):
        """Initialize isolated fixture state for native ownership and callback observations."""
        self.value = value
        self.requests = []

    async def call(self, prepared):
        """Run the synthetic continuation and retain enough state to detect unexpected dispatch."""
        self.requests.append(prepared)
        return self.value


class Stream:
    def __init__(self, values):
        """Initialize isolated fixture state for native ownership and callback observations."""
        self.values = values
        self.closed = False
        self.requests = []

    def call(self, prepared):
        """Run the synthetic continuation and retain enough state to detect unexpected dispatch."""
        self.requests.append(prepared)

        async def source():
            """Yield fixture chunks while preserving cancellation and cleanup behavior."""
            try:
                for value in self.values:
                    yield value
            finally:
                self.closed = True

        return source()


@pytest.fixture
def native(request):
    """Register a worker with task-local Agent identity and a signed local-tool Council fixture."""
    context, council = NativeContext(), Council()
    cfg = base_config(guard={"include": [TOOL]}, tools={TOOL: {"executor": "local"}})
    if getattr(request, "param", False):
        cfg["correlation"] = {"allow_single_agent_root": True}
        context.runtime.single_agent_root = True
    plugin = NomosPlugin(council=council)
    plugin.register(context, cfg)
    yield plugin, council, context.runtime, context
    plugin.close()


async def propose(plugin, call_id="call_a", text="Read the file"):
    """Run a unary model response through H2 and verify its proposal is preserved."""
    model = Model(response(call_id))
    result = await plugin.native_unary("fixture", request(text), codecs(), model)
    assert result == response(call_id)
    return model


async def execute(plugin, call_id="call_a", **kwargs):
    """Authorize a bound local proposal and expose the continuation for execution assertions."""
    continuation = Next({"text": "test content"})
    result = await plugin.h3(call(name=TOOL, call_id=call_id, path="a", **kwargs), continuation)
    return result.result, continuation


async def test_native_schema_and_request_have_no_model_tracking_token(native):
    """Keep bookkeeping internal while preserving valid guarded-tool execution."""
    plugin, council, runtime, context = native
    assert "inject-intent" not in context.hooks
    assert {"correlate-request", "correlate-stream", "authorize-execution"} <= context.hooks.keys()
    with runtime.bind(str(uuid.uuid4())):
        model = await propose(plugin)
        prepared = model.requests[0]
        field = prepared["content"]["tools"][0]["function"]["parameters"]["properties"]["nomos"]
        assert field["required"] == list(INTENT)
        assert "turn" not in field["properties"]
        assert "nt_" not in json.dumps(prepared)
        result, continuation = await execute(plugin)
    assert result["status"] == "executed" and len(council.calls) == 1
    assert continuation.calls == [{"path": "a"}]


@pytest.mark.parametrize("native", [False, True], indirect=True)
async def test_overlapping_requests_are_bound_by_response_ids_not_completion_order(native):
    """Resolve concurrent proposals to their own context despite reversed response order."""
    plugin, council, runtime, _ = native
    agent = str(uuid.uuid4())
    b_finished = asyncio.Event()
    order = []

    class ConcurrentModel(Model):
        async def call(self, prepared):
            """Run the synthetic continuation and retain enough state to detect unexpected dispatch."""
            label = prepared["content"]["messages"][0]["content"]
            if label == "request A":
                await b_finished.wait()
            else:
                b_finished.set()
            order.append(label)
            return self.value

    with runtime.bind(agent):
        await asyncio.gather(
            *(
                plugin.native_unary(
                    "fixture",
                    request("request " + label),
                    codecs(),
                    ConcurrentModel(response("call_" + label)),
                )
                for label in ["A", "B"]
            )
        )
        assert order == ["request B", "request A"]
        for label in ["A", "B"]:
            result, _ = await execute(plugin, "call_" + label)
            assert result["status"] == "executed"
            assert "request " + label in council.calls[-1]["context"]


async def test_multiple_calls_from_one_response_share_its_snapshot(native):
    """Associate sibling proposals with the immutable request that produced them."""
    plugin, council, runtime, _ = native
    with runtime.bind(str(uuid.uuid4())):
        await plugin.native_unary(
            "fixture", request("one request"), codecs(), Model(response("call_a", "call_b"))
        )
        for call_id in ["call_a", "call_b"]:
            result, _ = await execute(plugin, call_id)
            assert result["status"] == "executed"
            assert "one request" in council.calls[-1]["context"]


async def test_host_resolved_wrapper_keeps_origin_and_reviews_actual_leaf(native):
    """Retain wrapper provenance while Council authorizes the actual leaf operation."""
    plugin, council, runtime, _ = native
    proposal = {
        "tool_calls": [
            {
                "id": "wrapper_call",
                "name": "call_tool",
                "arguments": {
                    "name": TOOL,
                    "arguments": {"path": "a", "nomos": INTENT},
                },
            }
        ]
    }
    with runtime.bind(str(uuid.uuid4())):
        await plugin.native_unary("fixture", request("wrapped request"), codecs(), Model(proposal))
        result, continuation = await execute(plugin, "wrapper_call")
    assert result["status"] == "executed"
    assert council.calls[0]["tool_call"]["tool_name"] == TOOL
    assert "wrapped request" in council.calls[0]["context"]
    assert continuation.calls == [{"path": "a"}]


@pytest.mark.parametrize("native", [False, True], indirect=True)
async def test_same_call_id_in_distinct_agents_is_isolated(native):
    """Allow identical provider IDs across Agents without sharing request context."""
    plugin, council, runtime, _ = native
    agents = [str(uuid.uuid4()), str(uuid.uuid4())]
    for i, agent in enumerate(agents):
        with runtime.bind(agent):
            await propose(plugin, text=f"agent {i}")
    for i, agent in enumerate(agents):
        with runtime.bind(agent):
            result, _ = await execute(plugin)
            assert result["status"] == "executed"
            assert f"agent {i}" in council.calls[-1]["context"]


@pytest.mark.parametrize("native", [False, True], indirect=True)
async def test_reused_call_id_in_one_agent_invalidates_the_binding(native):
    """Invalidate ambiguous origin bindings instead of silently replacing a prior request."""
    plugin, council, runtime, _ = native
    with runtime.bind(str(uuid.uuid4())):
        await propose(plugin)
        with pytest.raises(StoreError, match="native_call_id_reused"):
            await propose(plugin, text="another request")
        result, continuation = await execute(plugin)
    assert result["status"] == "native_context_unavailable"
    assert not council.calls and not continuation.calls


@pytest.mark.parametrize("native", [False, True], indirect=True)
async def test_foreign_agent_cannot_use_a_known_call_id(native):
    """Prevent a known provider ID from resolving under a different Agent."""
    plugin, council, runtime, _ = native
    with runtime.bind(str(uuid.uuid4())):
        await propose(plugin)
    with runtime.bind(str(uuid.uuid4())):
        result, continuation = await execute(plugin)
    assert result["status"] == "native_context_unavailable"
    assert not council.calls and not continuation.calls


async def test_model_carried_turn_cannot_override_native_identity(native):
    """Reject model-supplied tracking metadata before it can influence native ownership."""
    plugin, council, runtime, _ = native
    with runtime.bind(str(uuid.uuid4())):
        await propose(plugin)
        result, continuation = await execute(plugin, intent={**INTENT, "turn": "nt_" + "a" * 32})
    assert result["status"] == "missing_intent"
    assert '"turn":' not in result["message"]
    assert not council.calls and not continuation.calls


async def test_no_native_agent_refuses_before_model_dispatch(native):
    """Avoid provider dispatch when application Agent provenance is unavailable."""
    plugin, _, _, _ = native
    model = Model(response("call_a"))
    with pytest.raises(NativeContextError):
        await plugin.native_unary("fixture", request(), codecs(), model)
    assert not model.requests


@pytest.mark.parametrize("native", [False, True], indirect=True)
async def test_complete_stream_publishes_bindings_and_preserves_chunks(native):
    """Pass streaming chunks through unchanged and publish IDs after successful completion."""
    plugin, council, runtime, _ = native
    source = Stream(chunks())
    with runtime.bind(str(uuid.uuid4())):
        observed = [
            chunk
            async for chunk in plugin.native_stream("fixture", request(), codecs(response=False), source)
        ]
        result, _ = await execute(plugin, "call_stream")
    assert observed == chunks() and source.closed
    assert result["status"] == "executed" and len(council.calls) == 1


@pytest.mark.parametrize("native", [False, True], indirect=True)
async def test_cancelled_stream_has_no_usable_binding(native):
    """Close cancelled streams without exposing partial proposal identities to H3."""
    plugin, council, runtime, _ = native
    source = Stream(chunks())
    with runtime.bind(str(uuid.uuid4())):
        stream = plugin.native_stream("fixture", request(), codecs(response=False), source)
        await anext(stream)
        await stream.aclose()
        result, continuation = await execute(plugin, "call_stream")
    assert source.closed and result["status"] == "native_context_unavailable"
    assert not council.calls and not continuation.calls


async def test_incomplete_stream_refuses_before_publishing_bindings(native):
    """Do not publish tool-call IDs from a stream lacking terminal completion."""
    plugin, council, runtime, _ = native
    source = Stream(chunks()[:1])
    with runtime.bind(str(uuid.uuid4())):
        with pytest.raises(NativeContextError, match="did not finish"):
            _ = [chunk async for chunk in plugin.native_stream("fixture", request(), codecs(), source)]
        result, continuation = await execute(plugin, "call_stream")
    assert source.closed and result["status"] == "native_context_unavailable"
    assert not council.calls and not continuation.calls


async def test_unsupported_stream_format_refuses_without_calling_provider(native):
    """Reject unsupported wire formats before invoking the model continuation."""
    plugin, _, runtime, _ = native
    source = Stream([])
    with runtime.bind(str(uuid.uuid4())), pytest.raises(NativeContextError, match="OpenAI-chat"):
        _ = [chunk async for chunk in plugin.native_stream("fixture", request(), codecs(api="other"), source)]
    assert not source.requests


async def test_callback_codec_stream_requires_operator_format_declaration(native):
    """A callback codec alone cannot attest the format of its raw streaming chunks."""
    plugin, _, runtime, _ = native
    stream = Stream(chunks())
    with runtime.bind(str(uuid.uuid4())):
        with pytest.raises(NativeContextError, match="stream_format"):
            async for _ in plugin.native_stream("fixture", request(), codecs(api=None), stream):
                pass
        assert not stream.requests
        object.__setattr__(plugin.settings, "stream_format", "openai_chat")
        async for _ in plugin.native_stream("fixture", request(), codecs(api=None), stream):
            pass
        result, _ = await execute(plugin, call_id="call_stream")
        assert result["status"] == "executed"


@pytest.mark.parametrize("streaming", [False, True])
async def test_text_only_response_survives_request_snapshot_expiry(native, monkeypatch, streaming):
    """A long text-only response needs no origin binding after its snapshot expires."""
    plugin, council, runtime, _ = native
    clock = [1000.0]
    monkeypatch.setattr("nemo_relay_nomos.repository.time.time", lambda: clock[0])

    class SlowTextModel(Model):
        async def call(self, prepared):
            """Finish beyond retention without sleeping or contacting a provider."""
            clock[0] += plugin.store.limits.retention_seconds + 1
            return await super().call(prepared)

    class SlowTextStream(Stream):
        def call(self, prepared):
            """Expire the origin while a text-only stream is being consumed."""

            async def source():
                """Yield a terminal text chunk after the request retention window."""
                clock[0] += plugin.store.limits.retention_seconds + 1
                yield {"choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": "stop"}]}

            return source()

    with runtime.bind(str(uuid.uuid4())):
        if streaming:
            result = [
                chunk
                async for chunk in plugin.native_stream("fixture", request(), codecs(), SlowTextStream([]))
            ]
            assert result[0]["choices"][0]["delta"]["content"] == "Hello"
        else:
            assert (
                await plugin.native_unary("fixture", request(), codecs(), SlowTextModel(response()))
                == response()
            )
    assert not council.calls
    assert plugin.store.db.execute("SELECT count(*) FROM native_calls").fetchone()[0] == 0


@pytest.mark.parametrize("streaming", [False, True])
async def test_full_evidence_quota_does_not_block_text_or_proposal_publication(native, streaming):
    """Evidence capacity gates guarded execution, while ordinary model responses remain usable."""
    from dataclasses import replace

    plugin, council, runtime, _ = native
    plugin.store.limits = replace(plugin.store.limits, max_records=1, conversation_share_percent=100)
    with runtime.bind(str(uuid.uuid4())):
        await propose(plugin, "first")
        assert (await execute(plugin, "first"))[0]["status"] == "executed"
        if streaming:
            source = Stream(
                [{"choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": "stop"}]}]
            )
            result = [chunk async for chunk in plugin.native_stream("fixture", request(), codecs(), source)]
            assert result == source.values and source.closed
        else:
            assert await plugin.native_unary("fixture", request(), codecs(), Model(response())) == response()
        await propose(plugin, "second")
        denied, continuation = await execute(plugin, "second")
        assert denied["status"] == "storage_quota_exceeded"
        assert not continuation.calls
    assert len(council.calls) == 1


async def test_default_rejects_single_agent_root_before_model_or_council(native):
    """The compatibility policy must be configured, not inferred from the host."""
    plugin, council, runtime, _ = native
    runtime.single_agent_root = True
    model = Model(response("call_a"))
    with runtime.bind(str(uuid.uuid4())):
        with pytest.raises(NativeContextError, match="application Agent"):
            await plugin.native_unary("fixture", request(), codecs(), model)
        result, continuation = await execute(plugin)
    assert result["status"] == "native_context_unavailable"
    assert not model.requests and not council.calls and not continuation.calls
