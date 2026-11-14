"""Opt-in metadata-only response observations; send return never means delivery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)
CLIENT_ID = re.compile(rb"[a-f0-9]{12}:[1-9][0-9]{0,9}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
SCOPE_KEY = "nomos.delivery_trace"
QUEUE_CAPACITY = 8192
MAX_BYTES = 256 * 1024 * 1024
FINAL_RESERVE_BYTES = 4096
EVENT_LIMIT = 64


def error_category(exc: BaseException) -> str:
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, BaseExceptionGroup):
        return "exception_group"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, OSError):
        return "os_error"
    return "error"


def safe_id(value: object) -> str | None:
    return value if isinstance(value, str) and SAFE_ID.fullmatch(value) else None


def logical_id(headers: list) -> tuple[str | None, str]:
    values = [v for k, v in headers if k.lower() == b"x-nomos-stress-request-id"]
    if not values:
        return None, "absent"
    if len(values) != 1:
        return None, "duplicate"
    value = values[0]
    if not isinstance(value, bytes) or not CLIENT_ID.fullmatch(value):
        return None, "invalid"
    return value.decode("ascii"), "valid"


class DeliverySink:
    """Bounded nonblocking producer; one daemon writer owns the file."""

    def __init__(
        self, directory: str, *, capacity: int = QUEUE_CAPACITY, max_bytes: int = MAX_BYTES
    ):
        if type(capacity) is not int or not 1 <= capacity <= QUEUE_CAPACITY:
            raise ValueError("delivery sink capacity out of range")
        if type(max_bytes) is not int or not 8192 <= max_bytes <= MAX_BYTES:
            raise ValueError("delivery sink byte limit out of range")
        self.max_bytes = max_bytes
        self.bytes_written = 0
        self.trace_capped = 0
        self.limit_reached = False
        self.accepting = True
        self.boot_id = uuid.uuid4().hex
        self.queue: queue.Queue[dict] = queue.Queue(maxsize=capacity)
        self.capacity = capacity
        self.closed = threading.Event()
        self.offered = self.written = self.dropped = self.write_failures = self.abandoned = 0
        self.lock = threading.Lock()
        self.path = Path(directory) / f"delivery-{self.boot_id}.jsonl"
        source = Path(__file__).parent
        self.source_hash = hashlib.sha256(
            b"".join(
                (source / name).read_bytes()
                for name in ("delivery_trace.py", "stateless_worker.py", "functions.py")
            )
        ).hexdigest()
        self.base = {
            "boot_id": self.boot_id,
            "pid": os.getpid(),
            "source_hash": self.source_hash,
            "pod_uid": safe_id(os.environ.get("POD_UID")),
            "attempt": safe_id(os.environ.get("NOMOS_DELIVERY_TRACE_ATTEMPT")),
        }
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            self.file = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        try:
            self.thread = threading.Thread(
                target=self._write, name="nomos-delivery-trace", daemon=True
            )
            self.thread.start()
        except BaseException:
            try:
                self.file.close()
            except Exception:
                logger.error("delivery_trace_initialization_close_failed")
            raise
        self.emit({"event": "sink_started", "queue_capacity": capacity, "event_limit": EVENT_LIMIT})

    def counters(self) -> dict:
        with self.lock:
            counts = {
                k: getattr(self, k)
                for k in ("offered", "written", "dropped", "write_failures", "abandoned")
            }
            counts["pending"] = self.offered - self.written - self.dropped - self.abandoned
            counts["trace_capped"] = self.trace_capped
            counts.update(
                bytes_written=self.bytes_written,
                max_bytes=self.max_bytes,
                limit_reached=self.limit_reached,
            )
            return counts

    def note_trace_drop(self) -> None:
        with self.lock:
            self.trace_capped += 1

    def emit(self, record: dict) -> bool:
        # Only observer-created primitive dictionaries reach this internal API.
        record = {**self.base, **record, "at_ns": time.time_ns(), "mono_ns": time.monotonic_ns()}
        with self.lock:
            self.offered += 1
            if self.closed.is_set() or not self.accepting:
                self.dropped += 1
                return False
            try:
                self.queue.put_nowait(record)
                return True
            except queue.Full:
                self.dropped += 1
                return False

    def _write(self) -> None:
        try:
            while not self.closed.is_set() or not self.queue.empty():
                try:
                    record = self.queue.get(timeout=0.1)
                except queue.Empty:
                    self.file.flush()
                    continue
                encoded = json.dumps(record, separators=(",", ":")) + "\n"
                size = len(encoded.encode("utf-8"))
                with self.lock:
                    over_limit = self.bytes_written + size > self.max_bytes - FINAL_RESERVE_BYTES
                    if over_limit:
                        self.accepting = False
                        self.limit_reached = True
                        self.abandoned = self.offered - self.written - self.dropped
                if over_limit:
                    while not self.queue.empty():
                        self.queue.get_nowait()
                    # Preserve one final summary for actual shutdown. No new
                    # ordinary events are accepted; the request path never waits.
                    self.closed.wait()
                    break
                self.file.write(encoded)
                with self.lock:
                    self.written += 1
                    self.bytes_written += size
            final = {**self.base, "event": "sink_final", **self.counters(), "at_ns": time.time_ns()}
            encoded = json.dumps(final, separators=(",", ":")) + "\n"
            final_size = len(encoded.encode("utf-8"))
            if final_size > FINAL_RESERVE_BYTES:
                raise RuntimeError("delivery final summary exceeds reservation")
            self.file.write(encoded)
            with self.lock:
                self.bytes_written += final_size
            self.file.flush()
        except Exception:
            with self.lock:
                self.closed.set()
                self.write_failures += 1
                self.abandoned = self.offered - self.written - self.dropped
            while not self.queue.empty():
                self.queue.get_nowait()
            logger.error("delivery_trace_writer_failed")
        finally:
            try:
                self.file.close()
            except Exception:
                with self.lock:
                    self.write_failures += 1
                logger.error("delivery_trace_close_failed")

    def close(self) -> dict:
        self.closed.set()
        self.thread.join(timeout=2.0)
        result = {**self.counters(), "writer_alive": self.thread.is_alive()}
        logger.info("delivery_trace_shutdown %s", json.dumps(result, separators=(",", ":")))
        return result


def configured_sink() -> DeliverySink | None:
    if os.environ.get("NOMOS_DELIVERY_TRACE") != "1":
        return None
    directory = os.environ.get("NOMOS_DELIVERY_TRACE_DIRECTORY")
    if not directory:
        logger.error("delivery_trace_unavailable reason=missing_directory")
        return None
    try:
        return DeliverySink(directory)
    except Exception:
        logger.error("delivery_trace_unavailable reason=initialization_failed")
        return None


class RequestTrace:
    def __init__(self, sink, scope: dict):
        self.sink = sink
        self.http_id = uuid.uuid4().hex
        self.sequence = self.dropped = self.body_count = self.body_bytes = 0
        self.observer_failures = 0
        self.disconnect = self.final_body = self.http_finished = False
        client, category = logical_id(scope.get("headers", []))
        self.identity = {"http_trace_id": self.http_id, "logical_id": client}
        method = scope.get("method", "")
        self.record(
            "http_enter",
            method=method if method in {"POST", "GET", "DELETE"} else "other",
            correlation=category,
        )

    def record(self, event: str, *, final: bool = False, **fields) -> None:
        if self.sequence >= EVENT_LIMIT - 1 and not final:
            self.dropped += 1
            note = getattr(self.sink, "note_trace_drop", None)
            if note is not None:
                try:
                    note()
                except Exception:
                    self.observer_failures += 1
            return
        self.sequence += 1
        try:
            accepted = self.sink.emit(
                {
                    **self.identity,
                    "event": event,
                    "sequence": self.sequence,
                    "after_http_final": self.http_finished,
                    **fields,
                }
            )
        except Exception:
            self.observer_failures += 1
            accepted = False
        if not accepted:
            self.dropped += 1


class DeliveryASGI:
    def __init__(self, app, sink):
        self.app, self.sink = app, sink

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            try:
                return await self.app(scope, receive, send)
            finally:
                if scope["type"] == "lifespan":
                    try:
                        # Lifespan shutdown only. A bounded join avoids
                        # indefinitely queued default-executor cleanup.
                        self.sink.close()
                    except Exception:
                        logger.error("delivery_trace_shutdown_failed")
        trace = RequestTrace(self.sink, scope)
        scope.setdefault("state", {})[SCOPE_KEY] = trace

        async def observed_receive():
            message = await receive()
            if message["type"] == "http.disconnect" and not trace.disconnect:
                trace.disconnect = True
                trace.record("http_disconnect")
            return message

        async def observed_send(message):
            kind = message["type"]
            fields = {
                "kind": kind if kind in {"http.response.start", "http.response.body"} else "other"
            }
            if kind == "http.response.start":
                status = message.get("status")
                fields["status"] = status if type(status) is int and 100 <= status <= 599 else None
            elif kind == "http.response.body":
                trace.body_count += 1
                size = len(message.get("body", b""))
                trace.body_bytes += size
                fields.update(
                    body_ordinal=trace.body_count,
                    bytes=size,
                    more_body=bool(message.get("more_body", False)),
                )
            trace.record("asgi_send_enter", **fields)
            try:
                await send(message)
            except BaseException as exc:
                trace.record("asgi_send_failed", outcome=error_category(exc), **fields)
                raise
            else:
                if kind == "http.response.body" and not message.get("more_body", False):
                    trace.final_body = True
                trace.record("asgi_send_return", **fields)

        outcome = "returned"
        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException as exc:
            outcome = error_category(exc)
            raise
        finally:
            trace.record(
                "http_final",
                final=True,
                outcome=outcome,
                body_count=trace.body_count,
                body_bytes=trace.body_bytes,
                final_body_send_returned=trace.final_body,
                disconnect_observed=trace.disconnect,
                trace_dropped=trace.dropped,
                observer_failures=trace.observer_failures,
            )
            trace.http_finished = True
            # Scope owns this descriptor: no global retention. Keep it available
            # if a handler reports an outcome while transport cancellation unwinds.


def current_trace():
    """Follow the request object carried across SDK task boundaries explicitly."""
    try:
        from mcp.server.lowlevel.server import request_ctx

        context = request_ctx.get()
        request = context.request
        trace = request.scope.get("state", {}).get(SCOPE_KEY) if request is not None else None
        return trace if isinstance(trace, RequestTrace) else None
    except (LookupError, AttributeError):
        return None


def observe_authorize_output(result) -> None:
    if os.environ.get("NOMOS_DELIVERY_TRACE") != "1":
        return
    trace = current_trace()
    if trace is None:
        return
    status = getattr(result, "status", None)
    trace.record(
        "authorize_output_ready",
        status=status if status in {"approved", "denied", "error", "pending"} else "other",
        intent_id=safe_id(getattr(result, "decision_id", None)),
        writ_id=safe_id(getattr(result, "writ_id", None)),
    )


def install_handler_observers(mcp) -> None:
    from mcp import types

    for request_type, method in (
        (types.CallToolRequest, "tools/call"),
        (types.ListToolsRequest, "tools/list"),
    ):
        handler = mcp._mcp_server.request_handlers.get(request_type)
        if handler is None:
            continue

        async def observed(request, _handler=handler, _method=method):
            trace = current_trace()
            method = (
                "internal/tools/list" if _method == "tools/list" and request is None else _method
            )
            rpc_id = None
            if trace is not None:
                from mcp.server.lowlevel.server import request_ctx

                value = request_ctx.get().request_id
                rpc_id = value if type(value) is int and abs(value) <= 2**53 else None
                trace.record("mcp_handler_enter", method=method, rpc_id=rpc_id)
            try:
                result = await _handler(request)
            except BaseException as exc:
                if trace is not None:
                    trace.record(
                        "mcp_handler_failed",
                        method=method,
                        rpc_id=rpc_id,
                        outcome=error_category(exc),
                    )
                raise
            if trace is not None:
                trace.record("mcp_handler_return", method=method, rpc_id=rpc_id)
            return result

        mcp._mcp_server.request_handlers[request_type] = observed
