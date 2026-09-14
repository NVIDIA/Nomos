<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Nomos mutual TLS configuration

Nomos supports explicit mutual TLS (mTLS) for its internal HTTP, MCP, gRPC,
Redis/Sentinel, OPA, Vault, executor, and OpenShell connections. The feature is
portable: Nomos consumes ordinary PEM files and does not depend on a particular
private PKI, cert-manager issuer, SPIFFE/SPIRE installation, or service mesh.

mTLS is a transport boundary. It encrypts bearer tokens and request data and
authenticates the workloads at both ends of a connection. It does **not** replace
Ed25519-signed Writs of Execution and does not yet make a certificate identity
the Nomos actor that requested or redeemed a writ. Application-level actor
binding remains a separate authorization control.

## Workload authorization

Trusting a CA answers "was this certificate issued by a trusted authority?"; it
does not by itself answer "may this workload call this service?" Use distinct
workload identities rather than a shared leaf certificate, then constrain them
with both layers available in the deployment:

- Kubernetes NetworkPolicies restrict which component pods can reach each
  listener. In mTLS mode the Council policy exposes the nginx sidecar port and
  not the loopback FastMCP port.
- Configure OPA with `--authentication=tls` and a `system.authz` policy when it
  must allow only the Council certificate subject/URI. The bundled transport
  configuration rejects certificate-less callers; organization-specific names
  belong in the deployment's OPA authorization policy.
- For nginx/Vault/managed data services, use the platform's certificate-subject
  or URI-SAN allowlists where available. If a listener can validate only the CA,
  issue a dedicated client CA for that edge or combine CA validation with its
  existing token/database authorization.

Bearer tokens, Vault auth, Redis/PostgreSQL credentials, and Writ verification
remain in force over mTLS. Do not remove them merely because a peer certificate
is present. A future identity layer should bind the authenticated actor—not only
the workload—to Writ minting and redemption.

## Certificate contract

Each workload needs its own leaf certificate and private key. Certificates must:

- chain to the configured CA bundle;
- contain the DNS name used in the connection URL as a DNS SAN;
- contain `serverAuth` when used by a listener and `clientAuth` when presented
  by a client (a workload certificate may contain both);
- be currently valid and use a key readable only by the workload;
- preferably contain a stable URI SAN such as `spiffe://example.org/nomos/monitor`.

Nomos requires TLS 1.2 or newer and enables hostname verification by default.
The optional `*_TLS_SERVER_NAME` setting is available for gRPC integrations
whose dial target differs from the certificate SAN; prefer matching DNS names.

For CA rotation, place both the retiring and replacement CA certificates in the
PEM trust bundle, roll out new leaf certificates, and remove the old CA only
after every peer has moved. A restart or rolling update is currently required
after certificate files change, except for OPA, which can refresh its files.

### Kubernetes identity and SAN inventory

The Helm release's fully qualified name is shown below as `<fullname>` and its
namespace as `<namespace>`. Include the short Service name actually used by the
client and, where applicable, its namespace-qualified forms. Client-only
identities can use a stable subject or URI SAN without a DNS SAN if the issuer
permits it.

| Identity | Required DNS SANs | EKUs |
| --- | --- | --- |
| Council | `<fullname>-council`, `<fullname>-council.<namespace>.svc`, and the cluster-domain form | `serverAuth`, `clientAuth` |
| Monitor | `<fullname>-monitor` and its namespace-qualified forms | `serverAuth`, `clientAuth` |
| Council gRPC | `<fullname>-council-grpc` and its namespace-qualified forms | `serverAuth`, `clientAuth` |
| Council gateway | None for its client-only Nomos identity | `clientAuth` |
| Executor | `<fullname>-executor-openshell` and its namespace-qualified forms | `serverAuth`, `clientAuth` |
| OpenShell | `<fullname>-openshell` and its namespace-qualified forms | `serverAuth` |
| Agent signup | `<fullname>-agent-signup` and its namespace-qualified forms | `serverAuth`, `clientAuth` |
| Redis/Sentinel | Both Service names, the Redis headless Service, and every advertised StatefulSet pod DNS name | `serverAuth`, `clientAuth` |
| OPA | `<fullname>-opa` and its namespace-qualified forms | `serverAuth` |

For Redis, the advertised pod names have the form
`<fullname>-redis-<ordinal>.<fullname>-redis-headless`; include their
`.<namespace>.svc` and cluster-domain forms when clients or replicas use those
names. If a private PKI will not issue mixed client/server EKUs or all required
SANs in one certificate, split the identities in deployment-specific chart
wiring rather than disabling verification.

## Shared environment contract

Every outbound client uses the following suffixes under its prefix:

| Variable suffix | Default | Meaning |
| --- | --- | --- |
| `_TLS_ENABLED` | `false` | Explicitly enable TLS; no opportunistic fallback |
| `_TLS_CA_FILE` | empty | PEM CA bundle used to verify the server |
| `_TLS_CERT_FILE` | empty | PEM client certificate |
| `_TLS_KEY_FILE` | empty | Client private key |
| `_TLS_VERIFY_HOSTNAME` | `true` | Verify the URL hostname against certificate SANs |
| `_TLS_MINIMUM_VERSION` | `TLSv1.2` | `TLSv1.2` or `TLSv1.3` |
| `_TLS_SERVER_NAME` | empty | Optional gRPC server-name override |

Every listener uses:

| Variable suffix | Default | Meaning |
| --- | --- | --- |
| `_TLS_ENABLED` | `false` | Explicitly enable TLS |
| `_TLS_CERT_FILE` | empty | PEM server certificate |
| `_TLS_KEY_FILE` | empty | Server private key |
| `_TLS_CLIENT_CA_FILE` | empty | CA bundle used to authenticate clients |
| `_TLS_REQUIRE_CLIENT_CERT` | `true` | Require a verified client certificate |
| `_TLS_MINIMUM_VERSION` | `TLSv1.2` | `TLSv1.2` or `TLSv1.3` |

Certificate and key settings are paired. Partial, unreadable, or TLS-disabled
configurations containing TLS material fail before network traffic is sent.
`alpha` and its `production`/`prod` aliases require explicit mTLS on
sensitive application edges. `dev` retains an explicit plaintext-compatible
mode for local development.

## Connection inventory

| Connection | Client prefix | Listener prefix / server control |
| --- | --- | --- |
| Monitor to Council MCP sidecar | `MONITOR_UPSTREAM` | nginx Council sidecar |
| gRPC proxy to Monitor MCP | `COUNCIL_MCP` | `MONITOR_LISTENER` |
| Demo to Monitor MCP | `NOMOS_COUNCIL` | `MONITOR_LISTENER` |
| Demo to executor | `NOMOS_EXECUTOR` | `EXECUTOR_LISTENER` |
| Executor to Council key bootstrap | `EXECUTOR_COUNCIL` | Council sidecar |
| Council telemetry to Monitor | `COUNCIL_TELEMETRY` | `MONITOR_LISTENER` |
| Council to OPA | `COUNCIL_OPA` | OPA TLS/authentication flags |
| Council/executor to Vault | `NOMOS_VAULT` | Vault listener configuration |
| Executor to OpenShell | `OPENSHELL` | `OPENSHELL_LISTENER` |
| gRPC clients to Council proxy | client library TLS material | `COUNCIL_GRPC` |
| Council to Redis/Sentinel | `COUNCIL_REDIS` | Redis TLS configuration |
| Monitor to Redis/Sentinel | `MONITOR_REDIS` | Redis TLS configuration |
| Executors to Redis/Sentinel | `EXECUTOR_REDIS` | Redis TLS configuration |
| Agent signup to Redis/Sentinel | `NOMOS_SIGNUP_REDIS` | Redis TLS configuration |
| Account-management CLI to Redis/Sentinel | `NOMOS_REDIS` | Redis TLS configuration |

Redis applies the same CA, certificate, key, and hostname checks to Sentinel
discovery and the selected master connection. Server-side Redis must disable its
plaintext port and enable `tls-auth-clients yes`.

PostgreSQL uses its native DSN. Strict profiles require `sslmode=verify-full`,
`sslrootcert`, `sslcert`, and `sslkey`, for example:

```text
postgresql://nomos@postgres/nomos?sslmode=verify-full&sslrootcert=/run/nomos/tls/ca.crt&sslcert=/run/nomos/tls/tls.crt&sslkey=/run/nomos/tls/tls.key
```

The PostgreSQL server must also require client certificates in `pg_hba.conf`.
Vault must be configured with a verified HTTPS listener that trusts the Nomos
client CA; its existing Kubernetes/AppRole login remains the application-level
authorization mechanism.

## Development Compose deployment

Generate throwaway development material and apply the overlay:

```bash
.venv/bin/python scripts/generate_dev_mtls_pki.py deploy/mtls
docker compose \
  -f deploy/docker-compose.yml \
  -f deploy/docker-compose.mtls.yml \
  --profile executor-openshell up --build
```

For the Redis Sentinel topology, include both TLS overlays after the topology
overlay so the three data nodes, all Sentinel listeners, replication links,
discovery, and application clients use TLS:

```bash
docker compose \
  -f deploy/docker-compose.yml \
  -f deploy/docker-compose.redis-sentinel.yml \
  -f deploy/docker-compose.mtls.yml \
  -f deploy/docker-compose.redis-sentinel-mtls.yml \
  --profile executor-openshell up --build
```

The generator creates a development CA and distinct identities for Council,
the Council TLS proxy, Monitor, gRPC, executors, OpenShell, Demo, agent signup,
Redis, OPA, Vault, PostgreSQL, and an administrative client. Generated material
is ignored by Git and private keys are written with mode `0600`. On a native
Linux Docker host, copy keys into Docker secrets or adjust ownership so the
non-root container UID can read only its own key; do not make keys world-readable.

The overlay is an executable example, not production PKI. It turns Redis and
OPA into mTLS-only servers, places an mTLS nginx listener in front of FastMCP,
and configures first-party clients/listeners with separate identities.

The chart's and Compose stack's bundled development PostgreSQL and Vault are
plaintext local-development services. Do not combine them with an mTLS security
claim. Use external TLS-enabled PostgreSQL and Vault endpoints, configure the
client prefixes above, and enforce client certificates at those servers. The
Helm chart rejects this unsafe combination at render time.

## Kubernetes and private PKI

In Kubernetes, issue one Secret per workload containing `tls.crt` and `tls.key`,
plus a Secret containing `ca.crt`. Mount those files read-only and
set the prefixes in the table above. With cert-manager, create one `Certificate`
per workload and include every Kubernetes Service DNS name in `dnsNames`; set
both `server auth` and `client auth` usages when the same workload accepts and
initiates connections.

The chart projects TLS material with group-readable mode `0440`. Each pod must
have an `fsGroup` that the cluster permits, or the certificate delivery driver
must set compatible ownership, so the non-root workload can read its key. For
example, set `monitor.podSecurityContext.fsGroup` to an operator-approved group.
Platforms that assign an `fsGroup` through admission policy can leave the value
unset. Validate file ownership and readability using the final pod security
context; do not make private keys world-readable to work around a mismatch.

The Helm chart accepts those existing Secrets directly:

```bash
helm upgrade --install nomos deploy/kubernetes/nomos \
  --namespace nomos --create-namespace \
  -f deploy/kubernetes/mtls-values.example.yaml
```

The chart rejects `mtls.enabled` when either chart-managed development
PostgreSQL or development Vault is enabled, because those local-only services
use plaintext listeners. Use external TLS-enabled instances in an mTLS
deployment; PostgreSQL must use `sslmode=verify-full` with client credentials.

See `deploy/kubernetes/cert-manager-mtls.example.yaml` for the cert-manager
`Certificate` shape. Repeat it for each enabled identity, with DNS SANs matching
the rendered Service names. The chart projects only that workload's identity
and the shared CA into each pod, assigns a distinct ServiceAccount to each mTLS
workload, enables the matching clients/listeners, runs an mTLS nginx sidecar in
front of the loopback-only FastMCP listener, and changes Redis/Sentinel and OPA
to reject plaintext/certificate-less clients. When an external Vault uses
Kubernetes authentication, bind its roles to the specific Council and executor
ServiceAccounts rather than the chart's compatibility ServiceAccount.

A company PKI, SPIRE CSI driver, service mesh, cert-manager, or secret delivery
controller may populate the files. Nomos does not issue production certificates.
If an existing platform already issues and rotates workload certificates, the
post-merge work is normally deployment configuration: request the identities,
mount the issued material, set the variables, and configure peer trust/policy.

Most Nomos processes load their TLS context at startup. A certificate controller
can update a projected Secret without restarting the process, so arrange a
rolling restart after renewal. `global.podAnnotations` is copied to every Nomos
mTLS workload and can be used with an operator-selected Secret reload controller.
Nomos does not require or endorse a particular controller. Test at least one
renewal before relying on automated rotation.

### Ingress and public clients

An internal listener that requires a client certificate cannot be reached by an
ordinary browser or by an ingress controller that does not present an accepted
client identity. When a service needs both public access and internal mTLS, use
a separate ingress-facing gateway: terminate public TLS and authentication at
that boundary, then have the gateway present its workload certificate on the
internal connection. Do not weaken the internal listener's client-certificate
requirement to make public ingress work.

A mesh can encrypt pod-to-pod traffic without application changes, but strict
Nomos mode still expects the application TLS contract unless the deployment has
a reviewed, equivalent mesh-only policy and deliberately remains in `dev` mode.
This avoids an accidental plaintext deployment when sidecar injection or mesh
policy is missing.

## Operations and troubleshooting

- `certificate verify failed`: confirm the complete CA chain, certificate dates,
  EKU, and that the URL hostname appears in a DNS SAN.
- `tlsv13 alert certificate required`: the client did not present a certificate
  trusted by the listener's client CA.
- configuration failure before startup: check paired cert/key settings and file
  readability from the container's non-root UID.
- Redis failures after Sentinel discovery: configure TLS material for the entire
  Redis prefix; Nomos applies it to Sentinel and data connections together.
- PostgreSQL hostname failures: retain `sslmode=verify-full` and reissue the
  server certificate with the actual connection hostname rather than weakening
  verification.
- During rotation, verify both CA certificates are present before changing leaf
  certificates. Roll back by restoring the overlap bundle and previous leaves.
- For revocation or key compromise, remove the affected workload from service,
  revoke it in the issuer, issue a new identity, rotate any bearer credentials it
  could observe, then roll the workload and its trust policy.

Useful checks:

```bash
openssl x509 -in tls.crt -noout -dates -ext subjectAltName -ext extendedKeyUsage
openssl s_client -connect monitor:9901 -servername monitor \
  -CAfile ca.crt -cert client.crt -key client.key -verify_return_error
```

Never use `curl -k`, disable hostname verification, or add a plaintext fallback
to make a failed deployment pass. Fix the certificate, trust bundle, DNS name,
or file permissions instead.
