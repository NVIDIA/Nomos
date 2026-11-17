# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run an ASGI service with the shared Nomos listener TLS contract."""

from __future__ import annotations

import argparse
import os

from .tls import ServerTLSConfig, require_strict_listener, strict_transport_profile


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("app", help="ASGI import string, for example package.module:app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--tls-prefix", required=True)
    args = parser.parse_args()

    tls = ServerTLSConfig.from_env(args.tls_prefix)
    require_strict_listener(
        tls,
        label=args.tls_prefix,
        strict=strict_transport_profile(os.environ.get("NOMOS_DEPLOY_PROFILE")),
    )

    import uvicorn

    if tls.enabled:
        config = uvicorn.Config(
            args.app,
            host=args.host,
            port=args.port,
            ssl_certfile=tls.cert_file,
            ssl_keyfile=tls.key_file,
        )
    else:
        config = uvicorn.Config(args.app, host=args.host, port=args.port)
    config.load()
    if tls.enabled:
        # Replace uvicorn's context with the shared implementation so minimum
        # protocol and mutual-auth behavior cannot drift between listeners.
        config.ssl = tls.ssl_context()
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
