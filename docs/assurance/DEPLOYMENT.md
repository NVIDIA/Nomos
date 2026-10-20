# Deploy asynchronous assurance

Assurance is disabled by default. Enable it explicitly to run the ledger bridge
and observer alongside an existing Nomos council. Neither component receives
Redis, database, signing, or agent credentials. The bridge exposes only ledger
reads; the observer verifies signatures against operator-provisioned public keys.
The bridge token, dashboard password, and machine read token must be distinct.
ETHOS inference and validated audit export each require separate opt-in.

## Prepare the image and configuration

Build the pinned monitoring image from the repository root:

```sh
docker build -f deploy/assurance/Dockerfile -t nomos-assurance:local .
```

The default image includes Compass and excludes Eval Author. Audit export needs
the optional export build target and a pinned Eval Author context; see
[the assurance build workflow](../../scripts/assurance_poc.py). ETHOS authoring
remains an offline task and needs no Eval Author deployment.

Provision `trusted-keys.json` as a JSON object mapping trusted signing key IDs to
64-character hexadecimal Ed25519 public keys. Obtain the council ID, ledger chain ID, and public keys
from the existing council configuration through a trusted operator channel;
do not infer trust anchors from unverified ledger events. Preserve old public
keys when the council rotates keys so historical verification remains possible.

For ETHOS, provide `ethos.json` and its referenced Markdown files in the same
configuration directory. See [the document examples](ethos/README.md) for the
manifest format, versioning, and agent mapping. Paths are resolved relative to
the manifest. Assign documents to authenticated agent IDs explicitly; example
documents are not automatically assigned to real agents. Disable ETHOS to run
only tool checks without inference credentials or ETHOS documents.

## Docker Compose

Use the standard stack plus the explicit assurance overlay and profile. No
assurance-specific secrets or paths are required when using the standard stack
alone. The overlay also parses without secrets when the profile is disabled.
When enabled, missing credentials or invalid trust configuration cause startup
to fail; a missing bind directory is rejected instead of silently created.

Copy `deploy/assurance/compose.env.example` to a private, untracked environment
file and fill the blanks. Set `ASSURANCE_CONFIG_DIR` to an absolute directory
containing `trusted-keys.json`. Restrict access to the environment file:

```sh
chmod 600 /absolute/path/assurance.env
docker compose --env-file deploy/.env --env-file /absolute/path/assurance.env \
  -f deploy/docker-compose.yml -f deploy/docker-compose.assurance.yml \
  --profile assurance config --quiet
docker compose --env-file deploy/.env --env-file /absolute/path/assurance.env \
  -f deploy/docker-compose.yml -f deploy/docker-compose.assurance.yml \
  --profile assurance up -d assurance-reader assurance
```

If your deployment uses Redis Sentinel or service mode, retain its existing
file combination and add `-f deploy/docker-compose.assurance.yml` last. The
bridge connects to the same internal `council:9901` MCP origin in either mode;
no companion PostgreSQL replication or migration stack is needed.

Open [the local dashboard](http://127.0.0.1:8090) and authenticate using the
configured dashboard user and password. Machine clients use
`Authorization: Bearer <ASSURANCE_API_READ_TOKEN>` for read-only API access.
`/api/status` reports ingestion and analysis health; TCP/process availability
alone does not establish that the ledger is current or that inference works.

The reader has no published port. The observer binds to loopback by default and
uses a separate internal network for reader access. ETHOS inference uses the
observer's egress network. Compose networks do not enforce an inference-host
allowlist; enforce approved inference destinations at your deployment's network
or proxy layer. Never expose the HTTP Basic endpoint publicly without a trusted
TLS reverse proxy and appropriate access controls.

Enable ETHOS by setting `ASSURANCE_ETHOS_ENABLED=true` and supplying
`ASSURANCE_MODEL`, `ASSURANCE_API_BASE`, `ASSURANCE_API_KEY`, `ethos.json`, and
reviewed documents. Use an explicit `openai/<model>` or `nvidia_nim/<model>` identifier. The inference
endpoint must support tool calling and structured output. Timeouts and output limits are independently configurable.
`ASSURANCE_INFERENCE_TIMEOUT` bounds each model request; the ETHOS detector also
has a deadline of twice that value. `ASSURANCE_STREAM_TIMEOUT` bounds the whole
analysis run, and `ASSURANCE_MAX_MODEL_CALLS` bounds attempted model requests per
run. Iterative Compass analysis may need several sequential calls. Set these
explicitly for the approved model and evidence window; exhausting a limit remains
a failed run rather than a successful negative finding.

Observer progress, findings, and document snapshots persist in the
`assurance-state` named volume across restarts. Keep one observer: do not scale
the Compose service or share its SQLite file across deployments. Stop assurance
with the same command prefix followed by `stop assurance assurance-reader`.
`down --volumes` removes its persistent history and must be treated as data loss.

## Helm

The standard chart renders no assurance workloads or endpoints by default.
Provision credentials separately, for example by creating a Secret from a
private file containing these keys:

- `sourceToken`: bridge read credential.
- `dashboardUser` and `dashboardPassword`: human reviewer credentials.
- `apiReadToken`: distinct machine read credential.
- `inferenceApiKey`: only required when ETHOS is enabled.

The chart references this existing Secret; it never generates assurance
credentials. Secret key names are configurable under `assurance.secretKeys`.
Store trusted keys in a versioned ConfigMap with key `trusted-keys.json`.
For ETHOS, use another versioned ConfigMap containing `ethos.json` and the
referenced Markdown files. ConfigMaps must fit Kubernetes size limits; do not
put confidential material into an ETHOS document.

```sh
kubectl -n nomos create secret generic nomos-assurance-credentials \
  --from-env-file=/absolute/path/assurance-secret-keys.env
kubectl -n nomos create configmap nomos-assurance-trust-v1 \
  --from-file=trusted-keys.json=/absolute/path/trusted-keys.json
helm lint deploy/kubernetes/nomos -f deploy/assurance/helm-values.yaml
helm upgrade --install nomos deploy/kubernetes/nomos -n nomos \
  -f /absolute/path/existing-deployment-values.yaml \
  -f deploy/assurance/helm-values.yaml
```

Replace placeholder identities in the example values before installation. Set
`images.assurance` to the image you built and made available to the cluster
(`kind load docker-image nomos-assurance:local --name <cluster>` for Kind).
For ETHOS, additionally supply a reviewed values file based on
`deploy/assurance/helm-ethos-values.yaml`. Its sample gateway selector and model
are placeholders. Provision the ETHOS ConfigMap and inference credential first.

Services remain ClusterIP; no assurance Ingress is created. For local access:

```sh
kubectl -n nomos port-forward service/nomos-assurance 8090:8080
```

The service name follows the Helm release's full name. To expose it remotely,
configure authenticated TLS routing to the observer Service and allow only the
selected ingress-controller peers under `assurance.ingressPeers`. Keep the
reader private. With `networkPolicy.enabled=true`, the observer can reach only
DNS, its reader, and explicitly configured ETHOS inference peers. The reader
can reach only DNS and council. The council policy permits ledger traffic from
the reader. The example values enable these policies; enforcement requires a
NetworkPolicy-capable CNI (default Kind networking does not enforce them).
`assurance.inferenceEgress` uses standard Kubernetes egress items: choose the
actual namespace/pod selectors or destination CIDRs and ports. A model URL alone
does not configure egress, and changing provider IPs requires policy maintenance.

The observer is a single replica using `Recreate`, with a persistent SQLite
volume. Set `assurance.persistence.existingClaim` to reuse a claim, or configure
its storage class and size for the generated ReadWriteOnce claim. Never scale
it beyond one replica. Back up the state volume before destructive releases;
`helm uninstall` deletes the chart-created claim. Use an independently managed
existing claim when retention across uninstall is required.

Change document or trust ConfigMap names for a reviewed update, then run
`helm upgrade` to roll out the configuration explicitly. In-place file changes
are not an automatic authoring or activation workflow. Runs retain their
selected document snapshots and provenance. Standard security settings use UID,
GID, and fsGroup 10001; override `assurance.podSecurityContext` for platform-assigned
IDs, ensuring the state mount remains writable by the observer.

## Validate a deployment

Render/configure first, then run the deployments sequentially on a local machine.
The deployment contract tests are in `tests/assurance/test_deployment.py`.
After startup, verify unauthenticated API requests fail, authenticated status and
findings work, and a known signed ledger fixture advances the checkpoint. For
ETHOS, first verify deterministic mode with no model credentials, then enable
approved inference and check run/coverage status for the mapped agent. Restart
the observer and confirm its checkpoint and findings persist without duplication.
A provider outage should appear in ETHOS run status while tool analysis continues.
