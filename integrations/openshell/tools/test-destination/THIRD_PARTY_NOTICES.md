<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Test destination: third-party dependencies

This test utility is first-party Apache-2.0 source. Running it downloads Python
packages from their public upstream distributions. Their sources and binaries
are not copied into this directory. Review upstream license terms before use;
preserve required notices when redistributing built artifacts.

The following inventory contains all 25 third-party package/version entries in
this utility's `uv.lock`, including development and platform-specific entries.
A locked entry is not necessarily installed on every platform or without the
development dependency group. Links identify exact versions on public PyPI.
These are independent of the Rust service's Cargo.lock dependencies and the
root Python workspace; this utility does not add benchmark or plotting packages.

The separately resolved build backend is `hatchling>=1.27,<2` (MIT), declared in
`pyproject.toml`. Build-isolation dependencies are not recorded in this uv.lock;
review the actual build artifacts if packaging/distributing this utility.
Python, uv and OpenShell are separately installed prerequisites, not vendored
runtime components. No prebuilt destination image or Python wheel is distributed.

| Package | Locked version | Upstream license expression |
| --- | --- | --- |
| [annotated-doc](https://pypi.org/project/annotated-doc/0.0.5/) | 0.0.5 | `MIT` |
| [annotated-types](https://pypi.org/project/annotated-types/0.8.0/) | 0.8.0 | `MIT` |
| [anyio](https://pypi.org/project/anyio/4.15.1/) | 4.15.1 | `MIT` |
| [click](https://pypi.org/project/click/8.5.0/) | 8.5.0 | `BSD-3-Clause` |
| [colorama](https://pypi.org/project/colorama/0.4.6/) | 0.4.6 | `BSD-3-Clause` |
| [fastapi](https://pypi.org/project/fastapi/0.150.0/) | 0.150.0 | `MIT` |
| [h11](https://pypi.org/project/h11/0.16.0/) | 0.16.0 | `MIT` |
| [httpcore2](https://pypi.org/project/httpcore2/2.15.1/) | 2.15.1 | `BSD-3-Clause` |
| [httpx2](https://pypi.org/project/httpx2/2.15.1/) | 2.15.1 | `BSD-3-Clause` |
| [httpx2-jsfetch](https://pypi.org/project/httpx2-jsfetch/1.0/) | 1.0 | `BSD-3-Clause` |
| [idna](https://pypi.org/project/idna/3.20/) | 3.20 | `BSD-3-Clause` |
| [iniconfig](https://pypi.org/project/iniconfig/2.3.1/) | 2.3.1 | `MIT` |
| [opentelemetry-api](https://pypi.org/project/opentelemetry-api/1.45.1/) | 1.45.1 | `Apache-2.0` |
| [packaging](https://pypi.org/project/packaging/26.3/) | 26.3 | `Apache-2.0 OR BSD-2-Clause` |
| [pluggy](https://pypi.org/project/pluggy/1.7.0/) | 1.7.0 | `MIT` |
| [pydantic](https://pypi.org/project/pydantic/2.14.1/) | 2.14.1 | `MIT` |
| [pydantic-core](https://pypi.org/project/pydantic-core/2.50.1/) | 2.50.1 | `MIT` |
| [pygments](https://pypi.org/project/pygments/2.21.0/) | 2.21.0 | `BSD-2-Clause` |
| [pytest](https://pypi.org/project/pytest/9.1.1/) | 9.1.1 | `MIT` |
| [ruff](https://pypi.org/project/ruff/0.17.1/) | 0.17.1 | `MIT` |
| [starlette](https://pypi.org/project/starlette/1.9.0/) | 1.9.0 | `BSD-3-Clause` |
| [truststore](https://pypi.org/project/truststore/0.10.5/) | 0.10.5 | `MIT` |
| [typing-extensions](https://pypi.org/project/typing-extensions/4.16.0/) | 4.16.0 | `PSF-2.0` |
| [typing-inspection](https://pypi.org/project/typing-inspection/0.4.4/) | 0.4.4 | `MIT` |
| [uvicorn](https://pypi.org/project/uvicorn/0.54.0/) | 0.54.0 | `BSD-3-Clause` |
