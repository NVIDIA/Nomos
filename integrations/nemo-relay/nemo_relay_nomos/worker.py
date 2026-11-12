# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import signal
import sys

from nemo_relay_plugin import serve_plugin

from .plugin import NomosPlugin


async def main():
    previous = None
    if sys.platform == "win32":
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    plugin = NomosPlugin()
    try:
        await serve_plugin(plugin)
    finally:
        plugin.close()
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    asyncio.run(main())
