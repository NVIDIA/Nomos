"""OpenShell trusted executor entrypoint."""

import asyncio
import hashlib
import logging
import sys

from executors.docker.api import build_executor_id, create_server
from executors.docker.main import _load_public_key, connect_nonce_store_with_retries
from executors.docker.nonce_store import NonceStore
from trusted_executor_contract import ExecutorCapabilities

from .config import OpenShellExecutorConfig
from .enforcer import OpenShellEnforcer
from .identity import build_executor_identity

logger = logging.getLogger(__name__)


def main():
    config = OpenShellExecutorConfig.load()

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    try:
        public_key = _load_public_key(config)
    except Exception as e:
        logger.error("Failed to load council public key: %s", e)
        sys.exit(1)

    fingerprint = hashlib.sha256(public_key).hexdigest()
    logger.info("Council public key loaded (fingerprint: %s)", fingerprint[:16])

    try:
        executor_identity = build_executor_identity(
            private_key_path=config.executor_private_key_path,
            public_key_path=config.executor_public_key_path,
        )
        logger.info(
            "Executor evidence key ready (fingerprint: %s)",
            executor_identity.key_fingerprint[:16],
        )
    except Exception as e:
        logger.error("Failed to initialize executor evidence key: %s", e)
        sys.exit(1)

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

    executor_id = build_executor_id("openshell", public_key, executor_identity)
    try:
        loop.run_until_complete(
            nonce_store.register_trusted_executor(
                executor_id=executor_id,
                public_key_hex=executor_identity.public_key_hex,
                key_fingerprint=executor_identity.key_fingerprint,
                metadata={"executor_type": "openshell"},
            )
        )
        logger.info("Trusted executor registered in Redis as %s", executor_id)
    except Exception as e:
        logger.error("Failed to register trusted executor: %s", e)
        sys.exit(1)

    enforcer = OpenShellEnforcer(
        public_key=public_key,
        nonce_store=nonce_store,
        config=config,
    )

    capabilities = ExecutorCapabilities(
        execution_modes=["openshell"],
        supports_packaged_files=True,
        supports_dry_run=False,
        supports_async_execution=False,
        metadata={"evidence_signing": "ed25519"},
    )

    try:
        server = create_server(
            config,
            enforcer,
            public_key,
            loop=loop,
            executor_identity=executor_identity,
            executor_id_prefix="openshell",
            capabilities=capabilities,
        )
        logger.info("OpenShell executor started")
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        loop.run_until_complete(nonce_store.close())
        loop.close()


if __name__ == "__main__":
    main()
