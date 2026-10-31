<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Context service: third-party dependencies

This component is first-party Apache-2.0 source. Its dependencies are downloaded
from their public upstream distributions; their code is not copied here.
Preserve required license texts/notices when redistributing built artifacts.
The inventory below lists every third-party package in this component's
`uv.lock`, including development and platform-specific entries; not every entry
is installed on every platform. It is separate from the root Python lockfile.

The build backend is pinned to `uv_build==0.12.1` (`MIT OR Apache-2.0`),
which declares no Python dependencies. Its wheel includes both license texts;
it is separately resolved for build isolation and is not in this lockfile.
This avoids Hatchling’s indirect MPL-2.0 dependency on pathspec. Python and uv are prerequisites,
not bundled runtime artifacts. No prebuilt image is included by this change.
Package license expressions below come from distribution/PyPI metadata; review
upstream license files when packaging or redistributing this service.

| Package | Locked version | Upstream license expression |
| --- | --- | --- |
| [annotated-doc](https://pypi.org/project/annotated-doc/0.0.5/) | 0.0.5 | `MIT` |
| [annotated-types](https://pypi.org/project/annotated-types/0.8.0/) | 0.8.0 | `MIT` |
| [anyio](https://pypi.org/project/anyio/4.15.1/) | 4.15.1 | `MIT` |
| [click](https://pypi.org/project/click/8.5.0/) | 8.5.0 | `BSD-3-Clause` |
| [colorama](https://pypi.org/project/colorama/0.4.6/) | 0.4.6 | `BSD-3-Clause` |
| [coverage](https://pypi.org/project/coverage/7.17.0/) | 7.17.0 | `Apache-2.0` |
| [fastapi](https://pypi.org/project/fastapi/0.162.0/) | 0.162.0 | `MIT` |
| [h11](https://pypi.org/project/h11/0.16.0/) | 0.16.0 | `MIT` |
| [httpcore2](https://pypi.org/project/httpcore2/2.16.0/) | 2.16.0 | `BSD-3-Clause` |
| [httpx2](https://pypi.org/project/httpx2/2.16.0/) | 2.16.0 | `BSD-3-Clause` |
| [httpx2-jsfetch](https://pypi.org/project/httpx2-jsfetch/1.0/) | 1.0 | `BSD-3-Clause` |
| [idna](https://pypi.org/project/idna/3.20/) | 3.20 | `BSD-3-Clause` |
| [iniconfig](https://pypi.org/project/iniconfig/2.3.1/) | 2.3.1 | `MIT` |
| [opentelemetry-api](https://pypi.org/project/opentelemetry-api/1.45.2/) | 1.45.2 | `Apache-2.0` |
| [packaging](https://pypi.org/project/packaging/26.3/) | 26.3 | `Apache-2.0 OR BSD-2-Clause` |
| [pluggy](https://pypi.org/project/pluggy/1.7.0/) | 1.7.0 | `MIT` |
| [pydantic](https://pypi.org/project/pydantic/2.14.2/) | 2.14.2 | `MIT` |
| [pydantic-core](https://pypi.org/project/pydantic-core/2.50.2/) | 2.50.2 | `MIT` |
| [pygments](https://pypi.org/project/pygments/2.21.0/) | 2.21.0 | `BSD-2-Clause` |
| [pytest](https://pypi.org/project/pytest/9.2.0/) | 9.2.0 | `MIT` |
| [pytest-cov](https://pypi.org/project/pytest-cov/7.1.0/) | 7.1.0 | `MIT` |
| [ruff](https://pypi.org/project/ruff/0.15.9/) | 0.15.9 | `MIT` |
| [starlette](https://pypi.org/project/starlette/1.10.0/) | 1.10.0 | `BSD-3-Clause` |
| [truststore](https://pypi.org/project/truststore/0.10.5/) | 0.10.5 | `MIT` |
| [typing-extensions](https://pypi.org/project/typing-extensions/4.16.0/) | 4.16.0 | `PSF-2.0` |
| [typing-inspection](https://pypi.org/project/typing-inspection/0.4.4/) | 0.4.4 | `MIT` |
| [uvicorn](https://pypi.org/project/uvicorn/0.54.0/) | 0.54.0 | `BSD-3-Clause` |

## License check for this change (30 October 2026)

Reviewed all 27 third-party entries in this lockfile, including development and
platform-specific packages, plus the pinned `uv_build==0.12.1` build backend.
All use permissive license expressions: MIT, BSD-2-Clause, BSD-3-Clause,
Apache-2.0, PSF-2.0, or alternatives among those licenses. No non-permissive
license was found in this component's reviewed dependency set.

For each exact version, a published wheel was fetched from PyPI, its SHA-256
checked against PyPI and (for locked entries) `uv.lock`, and its packaged
license files and license metadata inspected. Colorama 0.4.6 lacks a usable
metadata expression; its packaged three-clause BSD license resolves that gap.
The `uv_build` wheel contains both MIT and Apache-2.0 texts and declares no
Python dependencies. The built first-party wheel contains this notice and the
Apache-2.0 LICENSE file.

Hatchling was replaced before publication because its build dependency pathspec
uses MPL-2.0. No pathspec/Hatchling dependency is introduced by this project.
This audit is for these exact dependencies; recheck license terms when updating
the lockfile or backend. It does not inventory unrelated existing Nomos stacks.
