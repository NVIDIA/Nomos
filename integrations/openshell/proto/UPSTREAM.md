<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Copied OpenShell protocol files

Source: https://github.com/NVIDIA/OpenShell

Revision: `50b7fcd02b9cd70d40e93d176d684f6909c8f16f`

Files are copied without modification from `proto/` at that revision.
Their NVIDIA copyright and Apache-2.0 notices are retained.
The repository [LICENSE](../../../LICENSE) contains the Apache-2.0 terms.

SHA-256:

- `supervisor_middleware.proto`: `b3c2037a6b51285b48a39025b85e79a4c717a2b95accb89cf43dbe6079831c75`
- `extension.proto`: `60f28ee9dc846d41152f4db7e82a439c78d024786efaceb464900d1d1ad129ad`

The build generates Rust message types and gRPC interfaces from these copied
files. Compiling this service does not require a separate OpenShell checkout.
Compare and repin both contracts together when upgrading OpenShell.
