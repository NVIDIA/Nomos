<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# OpenShell integration: third-party dependencies

This integration is licensed under the repository's [Apache-2.0 license](../../LICENSE).
Setup downloads third-party software from upstream. Review the applicable license
terms before use. No dependency source code, model weights, downloaded compiler
binaries, or prebuilt service images are included in this integration.

The two copied OpenShell protocol definitions are NVIDIA-authored Apache-2.0
files. Their original notices are retained; [provenance and checksums](proto/UPSTREAM.md)
identify the public source revision.

The inventory below covers **every third-party package/version in Cargo.lock**,
including build, test and platform-specific dependencies. A locked entry does
not imply that it is compiled for every target or distributed in every binary.
Versions are pinned by Cargo.lock; the upstream crate pages provide source and
license information. Where an expression offers alternatives with `OR`, a
permissive alternative is available. **This integration selects the MIT license
for `r-efi` 6.0.0.** The inventory retains its upstream license expression for
accuracy; the selected license is identified explicitly in that row. `AND`
requires all listed terms.

`protoc-bin-vendored` downloads platform-specific compiler packages during the
build. Its wrapper's MIT declaration does not replace the license notices for
the bundled Protocol Buffers compiler and include files; retain those notices
if redistributing those artifacts. Creating a binary/container release requires
reviewing the licenses and notices of the artifacts actually distributed.

Python workspace dependencies are recorded separately in the root
[LICENSE-THIRD-PARTY](../../LICENSE-THIRD-PARTY). The optional lab destination
has its own [Python dependency inventory](tools/test-destination/THIRD_PARTY_NOTICES.md)
and `uv.lock`; it is not a Rust service runtime dependency. No benchmark tooling
is included.

The optional [curl workload Dockerfile](examples/curl-sandbox/Dockerfile)
downloads an Ubuntu 24.04 base image and distribution packages. Those operating
system components have their own license terms and notices, outside the Cargo
and Python lockfile inventories. This repository distributes the Dockerfile,
not the resulting image; review the actual image's components before redistribution.

| Crate | Locked version | Upstream license expression |
| --- | --- | --- |
| [aho-corasick](https://crates.io/crates/aho-corasick/1.1.5) | 1.1.5 | `Unlicense OR MIT` |
| [anstream](https://crates.io/crates/anstream/1.0.0) | 1.0.0 | `MIT OR Apache-2.0` |
| [anstyle](https://crates.io/crates/anstyle/1.0.14) | 1.0.14 | `MIT OR Apache-2.0` |
| [anstyle-parse](https://crates.io/crates/anstyle-parse/1.0.0) | 1.0.0 | `MIT OR Apache-2.0` |
| [anstyle-query](https://crates.io/crates/anstyle-query/1.1.5) | 1.1.5 | `MIT OR Apache-2.0` |
| [anstyle-wincon](https://crates.io/crates/anstyle-wincon/3.0.11) | 3.0.11 | `MIT OR Apache-2.0` |
| [anyhow](https://crates.io/crates/anyhow/1.0.104) | 1.0.104 | `MIT OR Apache-2.0` |
| [async-trait](https://crates.io/crates/async-trait/0.1.92) | 0.1.92 | `MIT OR Apache-2.0` |
| [atomic-waker](https://crates.io/crates/atomic-waker/1.1.2) | 1.1.2 | `Apache-2.0 OR MIT` |
| [autocfg](https://crates.io/crates/autocfg/1.5.1) | 1.5.1 | `Apache-2.0 OR MIT` |
| [axum](https://crates.io/crates/axum/0.8.9) | 0.8.9 | `MIT` |
| [axum-core](https://crates.io/crates/axum-core/0.5.6) | 0.5.6 | `MIT` |
| [base16ct](https://crates.io/crates/base16ct/0.2.0) | 0.2.0 | `Apache-2.0 OR MIT` |
| [base64](https://crates.io/crates/base64/0.22.1) | 0.22.1 | `MIT OR Apache-2.0` |
| [base64](https://crates.io/crates/base64/0.23.1) | 0.23.1 | `MIT OR Apache-2.0` |
| [base64ct](https://crates.io/crates/base64ct/1.8.3) | 1.8.3 | `Apache-2.0 OR MIT` |
| [bitflags](https://crates.io/crates/bitflags/2.13.2) | 2.13.2 | `MIT OR Apache-2.0` |
| [block-buffer](https://crates.io/crates/block-buffer/0.10.4) | 0.10.4 | `MIT OR Apache-2.0` |
| [bumpalo](https://crates.io/crates/bumpalo/3.20.3) | 3.20.3 | `MIT OR Apache-2.0` |
| [bytes](https://crates.io/crates/bytes/1.12.1) | 1.12.1 | `MIT` |
| [cc](https://crates.io/crates/cc/1.9.0) | 1.9.0 | `MIT OR Apache-2.0` |
| [cfg-if](https://crates.io/crates/cfg-if/1.0.5) | 1.0.5 | `MIT OR Apache-2.0` |
| [cfg_aliases](https://crates.io/crates/cfg_aliases/0.2.2) | 0.2.2 | `MIT` |
| [chacha20](https://crates.io/crates/chacha20/0.10.2) | 0.10.2 | `MIT OR Apache-2.0` |
| [clap](https://crates.io/crates/clap/4.6.7) | 4.6.7 | `MIT OR Apache-2.0` |
| [clap_builder](https://crates.io/crates/clap_builder/4.6.7) | 4.6.7 | `MIT OR Apache-2.0` |
| [clap_derive](https://crates.io/crates/clap_derive/4.6.7) | 4.6.7 | `MIT OR Apache-2.0` |
| [clap_lex](https://crates.io/crates/clap_lex/1.1.1) | 1.1.1 | `MIT OR Apache-2.0` |
| [colorchoice](https://crates.io/crates/colorchoice/1.0.5) | 1.0.5 | `MIT OR Apache-2.0` |
| [const-oid](https://crates.io/crates/const-oid/0.9.6) | 0.9.6 | `Apache-2.0 OR MIT` |
| [core-foundation](https://crates.io/crates/core-foundation/0.10.1) | 0.10.1 | `MIT OR Apache-2.0` |
| [core-foundation-sys](https://crates.io/crates/core-foundation-sys/0.8.7) | 0.8.7 | `MIT OR Apache-2.0` |
| [cpufeatures](https://crates.io/crates/cpufeatures/0.2.17) | 0.2.17 | `MIT OR Apache-2.0` |
| [cpufeatures](https://crates.io/crates/cpufeatures/0.3.1) | 0.3.1 | `MIT OR Apache-2.0` |
| [crypto-bigint](https://crates.io/crates/crypto-bigint/0.5.5) | 0.5.5 | `Apache-2.0 OR MIT` |
| [crypto-common](https://crates.io/crates/crypto-common/0.1.7) | 0.1.7 | `MIT OR Apache-2.0` |
| [curve25519-dalek](https://crates.io/crates/curve25519-dalek/4.1.3) | 4.1.3 | `BSD-3-Clause` |
| [curve25519-dalek-derive](https://crates.io/crates/curve25519-dalek-derive/0.1.1) | 0.1.1 | `MIT/Apache-2.0` |
| [der](https://crates.io/crates/der/0.7.10) | 0.7.10 | `Apache-2.0 OR MIT` |
| [deranged](https://crates.io/crates/deranged/0.5.9) | 0.5.9 | `MIT OR Apache-2.0` |
| [digest](https://crates.io/crates/digest/0.10.7) | 0.10.7 | `MIT OR Apache-2.0` |
| [displaydoc](https://crates.io/crates/displaydoc/0.2.7) | 0.2.7 | `MIT OR Apache-2.0` |
| [ecdsa](https://crates.io/crates/ecdsa/0.16.9) | 0.16.9 | `Apache-2.0 OR MIT` |
| [ed25519](https://crates.io/crates/ed25519/2.2.3) | 2.2.3 | `Apache-2.0 OR MIT` |
| [ed25519-dalek](https://crates.io/crates/ed25519-dalek/2.2.0) | 2.2.0 | `BSD-3-Clause` |
| [either](https://crates.io/crates/either/1.19.0) | 1.19.0 | `MIT OR Apache-2.0` |
| [elliptic-curve](https://crates.io/crates/elliptic-curve/0.13.8) | 0.13.8 | `Apache-2.0 OR MIT` |
| [equivalent](https://crates.io/crates/equivalent/1.0.3) | 1.0.3 | `Apache-2.0 OR MIT` |
| [errno](https://crates.io/crates/errno/0.3.14) | 0.3.14 | `MIT OR Apache-2.0` |
| [fastrand](https://crates.io/crates/fastrand/2.5.0) | 2.5.0 | `Apache-2.0 OR MIT` |
| [ff](https://crates.io/crates/ff/0.13.1) | 0.13.1 | `MIT/Apache-2.0` |
| [fiat-crypto](https://crates.io/crates/fiat-crypto/0.2.9) | 0.2.9 | `MIT OR Apache-2.0 OR BSD-1-Clause` |
| [find-msvc-tools](https://crates.io/crates/find-msvc-tools/0.1.15) | 0.1.15 | `MIT OR Apache-2.0` |
| [fixedbitset](https://crates.io/crates/fixedbitset/0.5.7) | 0.5.7 | `MIT OR Apache-2.0` |
| [fnv](https://crates.io/crates/fnv/1.0.7) | 1.0.7 | `Apache-2.0 / MIT` |
| [foldhash](https://crates.io/crates/foldhash/0.1.5) | 0.1.5 | `Zlib` |
| [form_urlencoded](https://crates.io/crates/form_urlencoded/1.2.2) | 1.2.2 | `MIT OR Apache-2.0` |
| [futures-channel](https://crates.io/crates/futures-channel/0.3.34) | 0.3.34 | `MIT OR Apache-2.0` |
| [futures-core](https://crates.io/crates/futures-core/0.3.34) | 0.3.34 | `MIT OR Apache-2.0` |
| [futures-io](https://crates.io/crates/futures-io/0.3.34) | 0.3.34 | `MIT OR Apache-2.0` |
| [futures-macro](https://crates.io/crates/futures-macro/0.3.34) | 0.3.34 | `MIT OR Apache-2.0` |
| [futures-sink](https://crates.io/crates/futures-sink/0.3.34) | 0.3.34 | `MIT OR Apache-2.0` |
| [futures-task](https://crates.io/crates/futures-task/0.3.34) | 0.3.34 | `MIT OR Apache-2.0` |
| [futures-util](https://crates.io/crates/futures-util/0.3.34) | 0.3.34 | `MIT OR Apache-2.0` |
| [generic-array](https://crates.io/crates/generic-array/0.14.7) | 0.14.7 | `MIT` |
| [getrandom](https://crates.io/crates/getrandom/0.2.17) | 0.2.17 | `MIT OR Apache-2.0` |
| [getrandom](https://crates.io/crates/getrandom/0.4.3) | 0.4.3 | `MIT OR Apache-2.0` |
| [group](https://crates.io/crates/group/0.13.0) | 0.13.0 | `MIT/Apache-2.0` |
| [h2](https://crates.io/crates/h2/0.4.20) | 0.4.20 | `MIT` |
| [hashbrown](https://crates.io/crates/hashbrown/0.15.5) | 0.15.5 | `MIT OR Apache-2.0` |
| [hashbrown](https://crates.io/crates/hashbrown/0.17.1) | 0.17.1 | `MIT OR Apache-2.0` |
| [heck](https://crates.io/crates/heck/0.5.0) | 0.5.0 | `MIT OR Apache-2.0` |
| [hkdf](https://crates.io/crates/hkdf/0.12.4) | 0.12.4 | `MIT OR Apache-2.0` |
| [hmac](https://crates.io/crates/hmac/0.12.1) | 0.12.1 | `MIT OR Apache-2.0` |
| [http](https://crates.io/crates/http/1.5.0) | 1.5.0 | `MIT OR Apache-2.0` |
| [http-body](https://crates.io/crates/http-body/1.1.0) | 1.1.0 | `MIT` |
| [http-body-util](https://crates.io/crates/http-body-util/0.1.5) | 0.1.5 | `MIT` |
| [httparse](https://crates.io/crates/httparse/1.10.1) | 1.10.1 | `MIT OR Apache-2.0` |
| [httpdate](https://crates.io/crates/httpdate/1.0.3) | 1.0.3 | `MIT OR Apache-2.0` |
| [hyper](https://crates.io/crates/hyper/1.12.0) | 1.12.0 | `MIT` |
| [hyper-rustls](https://crates.io/crates/hyper-rustls/0.27.10) | 0.27.10 | `Apache-2.0 OR ISC OR MIT` |
| [hyper-timeout](https://crates.io/crates/hyper-timeout/0.5.2) | 0.5.2 | `MIT OR Apache-2.0` |
| [hyper-util](https://crates.io/crates/hyper-util/0.1.21) | 0.1.21 | `MIT` |
| [icu_collections](https://crates.io/crates/icu_collections/2.3.0) | 2.3.0 | `Unicode-3.0` |
| [icu_locale_core](https://crates.io/crates/icu_locale_core/2.3.0) | 2.3.0 | `Unicode-3.0` |
| [icu_normalizer](https://crates.io/crates/icu_normalizer/2.3.0) | 2.3.0 | `Unicode-3.0` |
| [icu_normalizer_data](https://crates.io/crates/icu_normalizer_data/2.3.0) | 2.3.0 | `Unicode-3.0` |
| [icu_properties](https://crates.io/crates/icu_properties/2.3.0) | 2.3.0 | `Unicode-3.0` |
| [icu_properties_data](https://crates.io/crates/icu_properties_data/2.3.0) | 2.3.0 | `Unicode-3.0` |
| [icu_provider](https://crates.io/crates/icu_provider/2.3.1) | 2.3.1 | `Unicode-3.0` |
| [idna](https://crates.io/crates/idna/1.1.0) | 1.1.0 | `MIT OR Apache-2.0` |
| [idna_adapter](https://crates.io/crates/idna_adapter/1.2.2) | 1.2.2 | `Apache-2.0 OR MIT` |
| [indexmap](https://crates.io/crates/indexmap/2.14.2) | 2.14.2 | `Apache-2.0 OR MIT` |
| [ipnet](https://crates.io/crates/ipnet/2.12.2) | 2.12.2 | `MIT OR Apache-2.0` |
| [is_terminal_polyfill](https://crates.io/crates/is_terminal_polyfill/1.70.2) | 1.70.2 | `MIT OR Apache-2.0` |
| [itertools](https://crates.io/crates/itertools/0.14.0) | 0.14.0 | `MIT OR Apache-2.0` |
| [itoa](https://crates.io/crates/itoa/1.0.18) | 1.0.18 | `MIT OR Apache-2.0` |
| [js-sys](https://crates.io/crates/js-sys/0.3.106) | 0.3.106 | `MIT OR Apache-2.0` |
| [jsonwebtoken](https://crates.io/crates/jsonwebtoken/11.1.0) | 11.1.0 | `MIT` |
| [lazy_static](https://crates.io/crates/lazy_static/1.5.1) | 1.5.1 | `MIT OR Apache-2.0` |
| [libc](https://crates.io/crates/libc/0.2.191) | 0.2.191 | `MIT OR Apache-2.0` |
| [libm](https://crates.io/crates/libm/0.2.16) | 0.2.16 | `MIT` |
| [linux-raw-sys](https://crates.io/crates/linux-raw-sys/0.12.1) | 0.12.1 | `Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT` |
| [litemap](https://crates.io/crates/litemap/0.8.3) | 0.8.3 | `Unicode-3.0` |
| [log](https://crates.io/crates/log/0.4.34) | 0.4.34 | `MIT OR Apache-2.0` |
| [lru-slab](https://crates.io/crates/lru-slab/0.1.3) | 0.1.3 | `MIT OR Apache-2.0 OR Zlib` |
| [matchit](https://crates.io/crates/matchit/0.8.4) | 0.8.4 | `MIT AND BSD-3-Clause` |
| [memchr](https://crates.io/crates/memchr/2.8.3) | 2.8.3 | `Unlicense OR MIT` |
| [mime](https://crates.io/crates/mime/0.3.17) | 0.3.17 | `MIT OR Apache-2.0` |
| [mio](https://crates.io/crates/mio/1.2.4) | 1.2.4 | `MIT` |
| [multimap](https://crates.io/crates/multimap/0.10.1) | 0.10.1 | `MIT OR Apache-2.0` |
| [nu-ansi-term](https://crates.io/crates/nu-ansi-term/0.50.3) | 0.50.3 | `MIT` |
| [num-bigint-dig](https://crates.io/crates/num-bigint-dig/0.8.6) | 0.8.6 | `MIT/Apache-2.0` |
| [num-conv](https://crates.io/crates/num-conv/0.2.2) | 0.2.2 | `MIT OR Apache-2.0` |
| [num-integer](https://crates.io/crates/num-integer/0.1.47) | 0.1.47 | `MIT OR Apache-2.0` |
| [num-iter](https://crates.io/crates/num-iter/0.1.46) | 0.1.46 | `MIT OR Apache-2.0` |
| [num-traits](https://crates.io/crates/num-traits/0.2.19) | 0.2.19 | `MIT OR Apache-2.0` |
| [once_cell](https://crates.io/crates/once_cell/1.21.4) | 1.21.4 | `MIT OR Apache-2.0` |
| [once_cell_polyfill](https://crates.io/crates/once_cell_polyfill/1.70.2) | 1.70.2 | `MIT OR Apache-2.0` |
| [openssl-probe](https://crates.io/crates/openssl-probe/0.2.1) | 0.2.1 | `MIT OR Apache-2.0` |
| [p256](https://crates.io/crates/p256/0.13.2) | 0.13.2 | `Apache-2.0 OR MIT` |
| [p384](https://crates.io/crates/p384/0.13.1) | 0.13.1 | `Apache-2.0 OR MIT` |
| [pem-rfc7468](https://crates.io/crates/pem-rfc7468/0.7.0) | 0.7.0 | `Apache-2.0 OR MIT` |
| [percent-encoding](https://crates.io/crates/percent-encoding/2.3.2) | 2.3.2 | `MIT OR Apache-2.0` |
| [petgraph](https://crates.io/crates/petgraph/0.8.3) | 0.8.3 | `MIT OR Apache-2.0` |
| [pin-project](https://crates.io/crates/pin-project/1.1.13) | 1.1.13 | `Apache-2.0 OR MIT` |
| [pin-project-internal](https://crates.io/crates/pin-project-internal/1.1.13) | 1.1.13 | `Apache-2.0 OR MIT` |
| [pin-project-lite](https://crates.io/crates/pin-project-lite/0.2.17) | 0.2.17 | `Apache-2.0 OR MIT` |
| [pkcs1](https://crates.io/crates/pkcs1/0.7.5) | 0.7.5 | `Apache-2.0 OR MIT` |
| [pkcs8](https://crates.io/crates/pkcs8/0.10.2) | 0.10.2 | `Apache-2.0 OR MIT` |
| [potential_utf](https://crates.io/crates/potential_utf/0.1.6) | 0.1.6 | `Unicode-3.0` |
| [powerfmt](https://crates.io/crates/powerfmt/0.2.1) | 0.2.1 | `MIT OR Apache-2.0` |
| [ppv-lite86](https://crates.io/crates/ppv-lite86/0.2.21) | 0.2.21 | `MIT OR Apache-2.0` |
| [prettyplease](https://crates.io/crates/prettyplease/0.2.37) | 0.2.37 | `MIT OR Apache-2.0` |
| [primeorder](https://crates.io/crates/primeorder/0.13.6) | 0.13.6 | `Apache-2.0 OR MIT` |
| [proc-macro2](https://crates.io/crates/proc-macro2/1.0.107) | 1.0.107 | `MIT OR Apache-2.0` |
| [prost](https://crates.io/crates/prost/0.14.4) | 0.14.4 | `Apache-2.0` |
| [prost-build](https://crates.io/crates/prost-build/0.14.4) | 0.14.4 | `Apache-2.0` |
| [prost-derive](https://crates.io/crates/prost-derive/0.14.4) | 0.14.4 | `Apache-2.0` |
| [prost-types](https://crates.io/crates/prost-types/0.14.4) | 0.14.4 | `Apache-2.0` |
| [protoc-bin-vendored](https://crates.io/crates/protoc-bin-vendored/3.3.0) | 3.3.0 | `MIT` |
| [protoc-bin-vendored-linux-aarch_64](https://crates.io/crates/protoc-bin-vendored-linux-aarch_64/3.3.0) | 3.3.0 | `MIT` |
| [protoc-bin-vendored-linux-ppcle_64](https://crates.io/crates/protoc-bin-vendored-linux-ppcle_64/3.3.0) | 3.3.0 | `MIT` |
| [protoc-bin-vendored-linux-s390_64](https://crates.io/crates/protoc-bin-vendored-linux-s390_64/3.3.0) | 3.3.0 | `MIT` |
| [protoc-bin-vendored-linux-x86_32](https://crates.io/crates/protoc-bin-vendored-linux-x86_32/3.3.0) | 3.3.0 | `MIT` |
| [protoc-bin-vendored-linux-x86_64](https://crates.io/crates/protoc-bin-vendored-linux-x86_64/3.3.0) | 3.3.0 | `MIT` |
| [protoc-bin-vendored-macos-aarch_64](https://crates.io/crates/protoc-bin-vendored-macos-aarch_64/3.3.0) | 3.3.0 | `MIT` |
| [protoc-bin-vendored-macos-x86_64](https://crates.io/crates/protoc-bin-vendored-macos-x86_64/3.3.0) | 3.3.0 | `MIT` |
| [protoc-bin-vendored-win32](https://crates.io/crates/protoc-bin-vendored-win32/3.3.0) | 3.3.0 | `MIT` |
| [pulldown-cmark](https://crates.io/crates/pulldown-cmark/0.13.4) | 0.13.4 | `MIT` |
| [pulldown-cmark-to-cmark](https://crates.io/crates/pulldown-cmark-to-cmark/22.0.3) | 22.0.3 | `Apache-2.0` |
| [quinn](https://crates.io/crates/quinn/0.11.12) | 0.11.12 | `MIT OR Apache-2.0` |
| [quinn-proto](https://crates.io/crates/quinn-proto/0.11.19) | 0.11.19 | `MIT OR Apache-2.0` |
| [quinn-udp](https://crates.io/crates/quinn-udp/0.5.16) | 0.5.16 | `MIT OR Apache-2.0` |
| [quote](https://crates.io/crates/quote/1.0.47) | 1.0.47 | `MIT OR Apache-2.0` |
| [r-efi](https://crates.io/crates/r-efi/6.0.0) | 6.0.0 | `MIT OR Apache-2.0 OR LGPL-2.1-or-later` (MIT selected) |
| [rand](https://crates.io/crates/rand/0.10.3) | 0.10.3 | `MIT OR Apache-2.0` |
| [rand](https://crates.io/crates/rand/0.8.8) | 0.8.8 | `MIT OR Apache-2.0` |
| [rand_chacha](https://crates.io/crates/rand_chacha/0.3.1) | 0.3.1 | `MIT OR Apache-2.0` |
| [rand_core](https://crates.io/crates/rand_core/0.10.1) | 0.10.1 | `MIT OR Apache-2.0` |
| [rand_core](https://crates.io/crates/rand_core/0.6.4) | 0.6.4 | `MIT OR Apache-2.0` |
| [rand_pcg](https://crates.io/crates/rand_pcg/0.10.2) | 0.10.2 | `MIT OR Apache-2.0` |
| [regex](https://crates.io/crates/regex/1.13.1) | 1.13.1 | `MIT OR Apache-2.0` |
| [regex-automata](https://crates.io/crates/regex-automata/0.4.18) | 0.4.18 | `MIT OR Apache-2.0` |
| [regex-syntax](https://crates.io/crates/regex-syntax/0.8.11) | 0.8.11 | `MIT OR Apache-2.0` |
| [reqwest](https://crates.io/crates/reqwest/0.12.28) | 0.12.28 | `MIT OR Apache-2.0` |
| [rfc6979](https://crates.io/crates/rfc6979/0.4.0) | 0.4.0 | `Apache-2.0 OR MIT` |
| [ring](https://crates.io/crates/ring/0.17.14) | 0.17.14 | `Apache-2.0 AND ISC` |
| [rsa](https://crates.io/crates/rsa/0.9.10) | 0.9.10 | `MIT OR Apache-2.0` |
| [rustc-hash](https://crates.io/crates/rustc-hash/2.1.3) | 2.1.3 | `Apache-2.0 OR MIT` |
| [rustc_version](https://crates.io/crates/rustc_version/0.4.1) | 0.4.1 | `MIT OR Apache-2.0` |
| [rustix](https://crates.io/crates/rustix/1.1.5) | 1.1.5 | `Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT` |
| [rustls](https://crates.io/crates/rustls/0.23.46) | 0.23.46 | `Apache-2.0 OR ISC OR MIT` |
| [rustls-native-certs](https://crates.io/crates/rustls-native-certs/0.8.5) | 0.8.5 | `Apache-2.0 OR ISC OR MIT` |
| [rustls-pki-types](https://crates.io/crates/rustls-pki-types/1.15.1) | 1.15.1 | `MIT OR Apache-2.0` |
| [rustls-webpki](https://crates.io/crates/rustls-webpki/0.103.15) | 0.103.15 | `ISC` |
| [rustversion](https://crates.io/crates/rustversion/1.0.23) | 1.0.23 | `MIT OR Apache-2.0` |
| [ryu](https://crates.io/crates/ryu/1.0.23) | 1.0.23 | `Apache-2.0 OR BSL-1.0` |
| [schannel](https://crates.io/crates/schannel/0.1.30) | 0.1.30 | `MIT` |
| [sec1](https://crates.io/crates/sec1/0.7.3) | 0.7.3 | `Apache-2.0 OR MIT` |
| [security-framework](https://crates.io/crates/security-framework/3.7.0) | 3.7.0 | `MIT OR Apache-2.0` |
| [security-framework-sys](https://crates.io/crates/security-framework-sys/2.17.0) | 2.17.0 | `MIT OR Apache-2.0` |
| [semver](https://crates.io/crates/semver/1.0.28) | 1.0.28 | `MIT OR Apache-2.0` |
| [serde](https://crates.io/crates/serde/1.0.229) | 1.0.229 | `MIT OR Apache-2.0` |
| [serde_core](https://crates.io/crates/serde_core/1.0.229) | 1.0.229 | `MIT OR Apache-2.0` |
| [serde_derive](https://crates.io/crates/serde_derive/1.0.229) | 1.0.229 | `MIT OR Apache-2.0` |
| [serde_json](https://crates.io/crates/serde_json/1.0.154) | 1.0.154 | `MIT OR Apache-2.0` |
| [serde_path_to_error](https://crates.io/crates/serde_path_to_error/0.1.20) | 0.1.20 | `MIT OR Apache-2.0` |
| [serde_urlencoded](https://crates.io/crates/serde_urlencoded/0.7.1) | 0.7.1 | `MIT/Apache-2.0` |
| [sha2](https://crates.io/crates/sha2/0.10.9) | 0.10.9 | `MIT OR Apache-2.0` |
| [sharded-slab](https://crates.io/crates/sharded-slab/0.1.7) | 0.1.7 | `MIT` |
| [shlex](https://crates.io/crates/shlex/2.0.1) | 2.0.1 | `MIT OR Apache-2.0` |
| [signal-hook-registry](https://crates.io/crates/signal-hook-registry/1.4.8) | 1.4.8 | `MIT OR Apache-2.0` |
| [signature](https://crates.io/crates/signature/2.2.0) | 2.2.0 | `Apache-2.0 OR MIT` |
| [slab](https://crates.io/crates/slab/0.4.12) | 0.4.12 | `MIT` |
| [smallvec](https://crates.io/crates/smallvec/1.16.3) | 1.16.3 | `MIT OR Apache-2.0` |
| [socket2](https://crates.io/crates/socket2/0.6.5) | 0.6.5 | `MIT OR Apache-2.0` |
| [spin](https://crates.io/crates/spin/0.9.9) | 0.9.9 | `MIT` |
| [spki](https://crates.io/crates/spki/0.7.3) | 0.7.3 | `Apache-2.0 OR MIT` |
| [stable_deref_trait](https://crates.io/crates/stable_deref_trait/1.2.1) | 1.2.1 | `MIT OR Apache-2.0` |
| [strsim](https://crates.io/crates/strsim/0.11.1) | 0.11.1 | `MIT` |
| [subtle](https://crates.io/crates/subtle/2.6.1) | 2.6.1 | `BSD-3-Clause` |
| [syn](https://crates.io/crates/syn/2.0.119) | 2.0.119 | `MIT OR Apache-2.0` |
| [syn](https://crates.io/crates/syn/3.0.9) | 3.0.9 | `MIT OR Apache-2.0` |
| [sync_wrapper](https://crates.io/crates/sync_wrapper/1.0.2) | 1.0.2 | `Apache-2.0` |
| [synstructure](https://crates.io/crates/synstructure/0.14.0) | 0.14.0 | `MIT` |
| [tempfile](https://crates.io/crates/tempfile/3.27.0) | 3.27.0 | `MIT OR Apache-2.0` |
| [thiserror](https://crates.io/crates/thiserror/2.0.21) | 2.0.21 | `MIT OR Apache-2.0` |
| [thiserror-impl](https://crates.io/crates/thiserror-impl/2.0.21) | 2.0.21 | `MIT OR Apache-2.0` |
| [thread_local](https://crates.io/crates/thread_local/1.1.10) | 1.1.10 | `MIT OR Apache-2.0` |
| [time](https://crates.io/crates/time/0.3.55) | 0.3.55 | `MIT OR Apache-2.0` |
| [time-core](https://crates.io/crates/time-core/0.1.9) | 0.1.9 | `MIT OR Apache-2.0` |
| [time-macros](https://crates.io/crates/time-macros/0.2.32) | 0.2.32 | `MIT OR Apache-2.0` |
| [tinystr](https://crates.io/crates/tinystr/0.8.4) | 0.8.4 | `Unicode-3.0` |
| [tinyvec](https://crates.io/crates/tinyvec/1.13.3) | 1.13.3 | `Zlib OR Apache-2.0 OR MIT` |
| [tokio](https://crates.io/crates/tokio/1.54.0) | 1.54.0 | `MIT` |
| [tokio-macros](https://crates.io/crates/tokio-macros/2.7.2) | 2.7.2 | `MIT` |
| [tokio-rustls](https://crates.io/crates/tokio-rustls/0.26.6) | 0.26.6 | `MIT OR Apache-2.0` |
| [tokio-stream](https://crates.io/crates/tokio-stream/0.1.19) | 0.1.19 | `MIT` |
| [tokio-util](https://crates.io/crates/tokio-util/0.7.20) | 0.7.20 | `MIT` |
| [tonic](https://crates.io/crates/tonic/0.14.6) | 0.14.6 | `MIT` |
| [tonic-build](https://crates.io/crates/tonic-build/0.14.6) | 0.14.6 | `MIT` |
| [tonic-prost](https://crates.io/crates/tonic-prost/0.14.6) | 0.14.6 | `MIT` |
| [tonic-prost-build](https://crates.io/crates/tonic-prost-build/0.14.6) | 0.14.6 | `MIT` |
| [tower](https://crates.io/crates/tower/0.5.3) | 0.5.3 | `MIT` |
| [tower-http](https://crates.io/crates/tower-http/0.6.11) | 0.6.11 | `MIT` |
| [tower-layer](https://crates.io/crates/tower-layer/0.3.3) | 0.3.3 | `MIT` |
| [tower-service](https://crates.io/crates/tower-service/0.3.3) | 0.3.3 | `MIT` |
| [tracing](https://crates.io/crates/tracing/0.1.44) | 0.1.44 | `MIT` |
| [tracing-attributes](https://crates.io/crates/tracing-attributes/0.1.31) | 0.1.31 | `MIT` |
| [tracing-core](https://crates.io/crates/tracing-core/0.1.36) | 0.1.36 | `MIT` |
| [tracing-log](https://crates.io/crates/tracing-log/0.2.0) | 0.2.0 | `MIT` |
| [tracing-serde](https://crates.io/crates/tracing-serde/0.2.0) | 0.2.0 | `MIT` |
| [tracing-subscriber](https://crates.io/crates/tracing-subscriber/0.3.23) | 0.3.23 | `MIT` |
| [try-lock](https://crates.io/crates/try-lock/0.2.5) | 0.2.5 | `MIT` |
| [typenum](https://crates.io/crates/typenum/1.20.1) | 1.20.1 | `MIT OR Apache-2.0` |
| [unicase](https://crates.io/crates/unicase/2.10.0) | 2.10.0 | `MIT OR Apache-2.0` |
| [unicode-ident](https://crates.io/crates/unicode-ident/1.0.27) | 1.0.27 | `(MIT OR Apache-2.0) AND Unicode-3.0` |
| [untrusted](https://crates.io/crates/untrusted/0.9.0) | 0.9.0 | `ISC` |
| [url](https://crates.io/crates/url/2.5.8) | 2.5.8 | `MIT OR Apache-2.0` |
| [utf8_iter](https://crates.io/crates/utf8_iter/1.0.4) | 1.0.4 | `Apache-2.0 OR MIT` |
| [utf8parse](https://crates.io/crates/utf8parse/0.2.2) | 0.2.2 | `Apache-2.0 OR MIT` |
| [valuable](https://crates.io/crates/valuable/0.1.1) | 0.1.1 | `MIT` |
| [version_check](https://crates.io/crates/version_check/0.9.5) | 0.9.5 | `MIT/Apache-2.0` |
| [want](https://crates.io/crates/want/0.3.2) | 0.3.2 | `MIT` |
| [wasi](https://crates.io/crates/wasi/0.11.1+wasi-snapshot-preview1) | 0.11.1+wasi-snapshot-preview1 | `Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT` |
| [wasm-bindgen](https://crates.io/crates/wasm-bindgen/0.2.129) | 0.2.129 | `MIT OR Apache-2.0` |
| [wasm-bindgen-futures](https://crates.io/crates/wasm-bindgen-futures/0.4.79) | 0.4.79 | `MIT OR Apache-2.0` |
| [wasm-bindgen-macro](https://crates.io/crates/wasm-bindgen-macro/0.2.129) | 0.2.129 | `MIT OR Apache-2.0` |
| [wasm-bindgen-macro-support](https://crates.io/crates/wasm-bindgen-macro-support/0.2.129) | 0.2.129 | `MIT OR Apache-2.0` |
| [wasm-bindgen-shared](https://crates.io/crates/wasm-bindgen-shared/0.2.129) | 0.2.129 | `MIT OR Apache-2.0` |
| [wasm-streams](https://crates.io/crates/wasm-streams/0.4.2) | 0.4.2 | `MIT OR Apache-2.0` |
| [web-sys](https://crates.io/crates/web-sys/0.3.106) | 0.3.106 | `MIT OR Apache-2.0` |
| [web-time](https://crates.io/crates/web-time/1.1.0) | 1.1.0 | `MIT OR Apache-2.0` |
| [windows-link](https://crates.io/crates/windows-link/0.2.1) | 0.2.1 | `MIT OR Apache-2.0` |
| [windows-sys](https://crates.io/crates/windows-sys/0.52.0) | 0.52.0 | `MIT OR Apache-2.0` |
| [windows-sys](https://crates.io/crates/windows-sys/0.61.2) | 0.61.2 | `MIT OR Apache-2.0` |
| [windows-targets](https://crates.io/crates/windows-targets/0.52.6) | 0.52.6 | `MIT OR Apache-2.0` |
| [windows_aarch64_gnullvm](https://crates.io/crates/windows_aarch64_gnullvm/0.52.6) | 0.52.6 | `MIT OR Apache-2.0` |
| [windows_aarch64_msvc](https://crates.io/crates/windows_aarch64_msvc/0.52.6) | 0.52.6 | `MIT OR Apache-2.0` |
| [windows_i686_gnu](https://crates.io/crates/windows_i686_gnu/0.52.6) | 0.52.6 | `MIT OR Apache-2.0` |
| [windows_i686_gnullvm](https://crates.io/crates/windows_i686_gnullvm/0.52.6) | 0.52.6 | `MIT OR Apache-2.0` |
| [windows_i686_msvc](https://crates.io/crates/windows_i686_msvc/0.52.6) | 0.52.6 | `MIT OR Apache-2.0` |
| [windows_x86_64_gnu](https://crates.io/crates/windows_x86_64_gnu/0.52.6) | 0.52.6 | `MIT OR Apache-2.0` |
| [windows_x86_64_gnullvm](https://crates.io/crates/windows_x86_64_gnullvm/0.52.6) | 0.52.6 | `MIT OR Apache-2.0` |
| [windows_x86_64_msvc](https://crates.io/crates/windows_x86_64_msvc/0.52.6) | 0.52.6 | `MIT OR Apache-2.0` |
| [writeable](https://crates.io/crates/writeable/0.6.4) | 0.6.4 | `Unicode-3.0` |
| [yoke](https://crates.io/crates/yoke/0.8.3) | 0.8.3 | `Unicode-3.0` |
| [yoke-derive](https://crates.io/crates/yoke-derive/0.8.4) | 0.8.4 | `Unicode-3.0` |
| [zerocopy](https://crates.io/crates/zerocopy/0.8.63) | 0.8.63 | `BSD-2-Clause OR Apache-2.0 OR MIT` |
| [zerocopy-derive](https://crates.io/crates/zerocopy-derive/0.8.63) | 0.8.63 | `BSD-2-Clause OR Apache-2.0 OR MIT` |
| [zerofrom](https://crates.io/crates/zerofrom/0.1.8) | 0.1.8 | `Unicode-3.0` |
| [zerofrom-derive](https://crates.io/crates/zerofrom-derive/0.1.8) | 0.1.8 | `Unicode-3.0` |
| [zeroize](https://crates.io/crates/zeroize/1.9.1) | 1.9.1 | `Apache-2.0 OR MIT` |
| [zeroize_derive](https://crates.io/crates/zeroize_derive/1.5.0) | 1.5.0 | `Apache-2.0 OR MIT` |
| [zerotrie](https://crates.io/crates/zerotrie/0.2.5) | 0.2.5 | `Unicode-3.0` |
| [zerovec](https://crates.io/crates/zerovec/0.11.8) | 0.11.8 | `Unicode-3.0` |
| [zerovec-derive](https://crates.io/crates/zerovec-derive/0.11.6) | 0.11.6 | `Unicode-3.0` |
| [zmij](https://crates.io/crates/zmij/1.0.23) | 1.0.23 | `MIT` |
