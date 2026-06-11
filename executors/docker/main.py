# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Writ Executor — entrypoint.

Loads config, council public key (from file or council service),
initializes Redis nonce store, and starts the HTTP server.
"""

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from executors.common import load_or_create_executor_identity

from .api import build_executor_id, create_server
from .config import ExecutorConfig
from .enforcer import WritEnforcer
from .nonce_store import NonceStore

logger = logging.getLogger(__name__)

KEY_FETCH_MAX_RETRIES = 10
KEY_FETCH_RETRY_DELAY = 3  # seconds
REDIS_CONNECT_MAX_RETRIES = 30
REDIS_CONNECT_RETRY_DELAY = 2.0  # seconds


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        logger.warning("Invalid %s=%r; using %d", name, value, default)
        return default
    return max(1, parsed)


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        logger.warning("Invalid %s=%r; using %.1f", name, value, default)
        return default
    return max(0.1, parsed)


def connect_nonce_store_with_retries(
    loop: asyncio.AbstractEventLoop,
    nonce_store: NonceStore,
) -> None:
    """Connect to Redis, tolerating transient Docker DNS/network readiness lag."""
    max_retries = _env_int("EXECUTOR_REDIS_CONNECT_RETRIES", REDIS_CONNECT_MAX_RETRIES)
    retry_delay = _env_float("EXECUTOR_REDIS_CONNECT_RETRY_DELAY", REDIS_CONNECT_RETRY_DELAY)
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            loop.run_until_complete(nonce_store.connect())
            logger.info("Connected to Redis for nonce tracking")
            return
        except Exception as e:
            last_error = e
            loop.run_until_complete(nonce_store.close())
            if attempt >= max_retries:
                break
            logger.warning(
                "Failed to connect to Redis (attempt %d/%d): %s; retrying in %.1fs",
                attempt,
                max_retries,
                e,
                retry_delay,
            )
            time.sleep(retry_delay)

    raise RuntimeError(f"Failed to connect to Redis after {max_retries} attempts: {last_error}")


def _fetch_key_from_council(council_url: str) -> bytes:
    """Fetch the council's Ed25519 public key via its MCP endpoint.

    Retries with backoff since the council service may still be starting.
    """
    url = council_url.rstrip("/")
    # The council_get_public_key MCP tool returns JSON with public_key_hex
    # Try the NAT debug endpoint first, then fall back to direct tool call
    last_error = None
    for attempt in range(1, KEY_FETCH_MAX_RETRIES + 1):
        # Strategy 1: NAT debug endpoint (simple REST)
        try:
            debug_url = f"{url}/debug/tools/call/council_get_public_key"
            req = urllib.request.Request(  # noqa: S310
                debug_url,
                data=json.dumps({"query": ""}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                body = json.loads(resp.read())
                if isinstance(body, str):
                    body = json.loads(body)
                if "result" in body:
                    result = body["result"]
                    if isinstance(result, str):
                        result = json.loads(result)
                else:
                    result = body

                pub_hex = result.get("public_key_hex", "")
                if pub_hex and len(pub_hex) == 64:
                    key_bytes = bytes.fromhex(pub_hex)
                    logger.info(
                        "Fetched council public key from %s (attempt %d)",
                        debug_url,
                        attempt,
                    )
                    return key_bytes

                error = result.get("error", "")
                if error:
                    logger.warning("Council returned error: %s", error)

        except (
            urllib.error.URLError,
            ConnectionError,
            OSError,
            json.JSONDecodeError,
            ValueError,
        ) as e:
            last_error = e

        # Strategy 2: MCP streamable-HTTP endpoint (JSON-RPC)
        try:
            mcp_url = f"{url}/mcp"
            rpc_payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "council_get_public_key", "arguments": {"query": ""}},
                }
            )
            req = urllib.request.Request(  # noqa: S310
                mcp_url,
                data=rpc_payload.encode(),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                body = json.loads(resp.read())
                rpc_result = body.get("result", {})
                content = rpc_result.get("content", [])
                if content:
                    text = content[0].get("text", "")
                    parsed = json.loads(text) if text else {}
                    pub_hex = parsed.get("public_key_hex", "")
                    if pub_hex and len(pub_hex) == 64:
                        key_bytes = bytes.fromhex(pub_hex)
                        logger.info(
                            "Fetched council public key from %s (attempt %d)",
                            mcp_url,
                            attempt,
                        )
                        return key_bytes

        except (
            urllib.error.URLError,
            ConnectionError,
            OSError,
            json.JSONDecodeError,
            ValueError,
        ) as e:
            last_error = e

        logger.info(
            "Council not ready (attempt %d/%d): %s — retrying in %ds",
            attempt,
            KEY_FETCH_MAX_RETRIES,
            last_error,
            KEY_FETCH_RETRY_DELAY,
        )
        time.sleep(KEY_FETCH_RETRY_DELAY)

    raise RuntimeError(
        f"Failed to fetch council public key after {KEY_FETCH_MAX_RETRIES} attempts: {last_error}"
    )


def _load_public_key(config: ExecutorConfig) -> bytes:
    """Load the council public key from file or council service."""
    # Prefer file if configured and exists
    if config.council_public_key_path:
        pub_key_path = Path(config.council_public_key_path)
        if pub_key_path.exists():
            key_bytes = pub_key_path.read_bytes()
            if len(key_bytes) != 32:
                raise ValueError(f"Invalid public key: expected 32 bytes, got {len(key_bytes)}")
            return key_bytes
        logger.warning("Public key file not found at %s", pub_key_path)

    # Fall back to fetching from council service
    if config.council_url:
        return _fetch_key_from_council(config.council_url)

    raise RuntimeError(
        "No key source available. Set EXECUTOR_PUBLIC_KEY_PATH (file)"
        " or EXECUTOR_COUNCIL_URL (service)"
    )


def main():
    config = ExecutorConfig.load()

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Load council public key
    try:
        public_key = _load_public_key(config)
    except Exception as e:
        logger.error("Failed to load council public key: %s", e)
        sys.exit(1)

    fingerprint = hashlib.sha256(public_key).hexdigest()
    logger.info("Council public key loaded (fingerprint: %s)", fingerprint[:16])

    # Load or generate the executor-owned identity used to sign execution evidence.
    try:
        executor_identity = load_or_create_executor_identity(
            config.executor_private_key_path,
            config.executor_public_key_path,
        )
        logger.info(
            "Executor evidence key ready (fingerprint: %s)",
            executor_identity.key_fingerprint[:16],
        )
    except Exception as e:
        logger.error("Failed to initialize executor evidence key: %s", e)
        sys.exit(1)

    # Initialize nonce store
    loop = asyncio.new_event_loop()
    nonce_store = NonceStore(
        config.redis_url,
        config.redis_key_prefix,
        redis_mode=config.redis_mode,
        sentinel_service_name=config.redis_sentinel_service_name,
        sentinel_nodes=config.redis_sentinel_nodes,
        sentinel_password=config.redis_sentinel_password,
        connect_timeout_ms=config.redis_connect_timeout_ms,
        command_timeout_ms=config.redis_command_timeout_ms,
    )
    try:
        connect_nonce_store_with_retries(loop, nonce_store)
    except Exception as e:
        logger.error("Failed to connect to Redis: %s", e)
        sys.exit(1)

    executor_id = build_executor_id("docker", public_key, executor_identity)
    try:
        loop.run_until_complete(
            nonce_store.register_trusted_executor(
                executor_id=executor_id,
                public_key_hex=executor_identity.public_key_hex,
                key_fingerprint=executor_identity.key_fingerprint,
                metadata={"executor_type": "docker"},
            )
        )
        logger.info("Trusted executor registered in Redis as %s", executor_id)
    except Exception as e:
        logger.error("Failed to register trusted executor: %s", e)
        sys.exit(1)

    # Create enforcer
    enforcer = WritEnforcer(
        public_key=public_key,
        nonce_store=nonce_store,
        config=config,
    )

    # Start HTTP server
    try:
        server = create_server(
            config,
            enforcer,
            public_key,
            loop=loop,
            executor_identity=executor_identity,
            executor_id_prefix="docker",
        )
        logger.info("Writ executor started")
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        loop.run_until_complete(nonce_store.close())
        loop.close()


if __name__ == "__main__":
    main()
