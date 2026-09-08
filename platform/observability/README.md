# Development observability stack

The GCP reference runtime was retired on 2026-09-08. Dated checks below are
historical evidence; access commands require a redeployed environment.

This directory contains reviewable values and manifests for the GCP kubeadm
reference runtime. The same base supports `local`, `forwarder`, and `receiver`
profiles. The fault target forwards selected metric, log, and trace data; the
separate observability domain stores and queries it.

The current `forwarder` profile is transitional: it keeps the base local stack
installed while sending authoritative target telemetry to the receiver. A
later lightweight profile can remove unused target-local stores only after
their PVC retention or deletion is reviewed explicitly.

| Release | Purpose | Development boundary |
|---|---|---|
| local-path-provisioner | Dynamic host-path PV provisioning | `Retain`, not default, VM boot disk only |
| kube-prometheus-stack | Prometheus, Alertmanager, Grafana and Kubernetes/node metrics | 7-day or 12 GiB Prometheus retention |
| Loki | Kubernetes container log storage and query | monolithic, one replica, 72-hour retention |
| Alloy | Kubernetes API-based Pod log discovery and forwarding | one Deployment, read-only Pod/log RBAC |
| Tempo | OTLP trace storage and search | monolithic, one replica, 72-hour retention, 5 GiB PVC |

Cilium agent, Envoy, operator, Hubble metrics and Hubble Relay expose
ServiceMonitors only after the Prometheus Operator CRD exists. Hubble flow
queries remain a separate read-only source and are not replaced by metrics.

## Validate and deploy

```bash
make render-observability
make ansible-syntax
make deploy-observability
make verify-observability
make deploy-three-domain
```

The deploy command is idempotent: a release is reconciled only when it is
missing, its pinned chart version changed, or its managed values changed. Helm
uses `--atomic --wait`; an unsuccessful install is rolled back instead of being
accepted as complete.

The verify command checks Pod readiness, Bound PVCs, the reviewed Service
exposure, absence of Ingress, component readiness, Cilium/Hubble targets, and
normalized `cluster_id` labels. `deploy-three-domain` additionally proves that
fault-target metric series, logs, traces, and a bounded Hubble flow summary are
available to RCA control, and that a central synthetic alert becomes a durable
Incident with remote Evidence.

## Workload event alerts

`remote-workload-alerts.yaml` adds `OnlineBoutiqueRecentOOMRestart` without changing
the six service-impact rules (`failure rate > 5%`, request rate > 0.1, `for: 2m`).
It requires the same cluster/namespace/Pod UID/container to have a last termination
reason of OOMKilled, a termination timestamp within five minutes (not in the
future), and a positive restart count. There is no additional hold time. A
counter increase baseline is not required, so the first observed sample may
already have restartCount=1. Ordinary restarts alone do not trigger RCA.

Controller ownership maps Pod → ReplicaSet → Deployment → matching Service name
for the 11 explicitly listed Online Boutique application Services. The telemetry
collector is excluded. This name mapping is a reference-workload convention, not
a general Kubernetes guarantee. UID joins prevent old Pod identities from being
combined with replacements; exporter replicas are deduplicated before joining.
The final alert contains Service-level labels only, because the current Worker
requires a Service-scoped Incident. It omits `krca_profile` and uses existing
root-scoped Kubernetes/metric/Loki/Hubble collection and StateGraph localization.

Multiple matching Pods coalesce into one Service alert. Expiry of the event window
is not a service-recovery claim, and the Alert itself is not root-cause Evidence.
Missing metrics/ownership fail closed and can prevent detection; short events
overwritten before a scrape or deleted Pods can still be missed. The termination
metrics are experimental in [kube-state-metrics](https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/workload/pod-metrics.md).

```bash
make validate-alert-rules    # pinned promtool, isolated samples, no cluster access
make deploy-workload-alerts # only the central workload PrometheusRule
```

The 28 scenarios cover first-sample OOM, expiry, ordinary restarts, missing data,
UID/cluster/namespace isolation, ownership, duplicate exporters and unchanged
service-impact hold behavior. CI runs them with the pinned PromQL evaluator.
Rule deployment and healthy Service mappings are verified live. On 2026-09-05,
one real checkout OOM fired this rule and reached an accepted Agent Report in
27 seconds from Incident ingestion, without a synthetic alert. Exact workload
restoration and the natural resolved webhook were verified. This is a single-run
connectivity check, not an accuracy result or downstream impact-localization test;
see the [runtime record](../../evaluation/REPORT.md#native-alert-runtime-check).

## Cilium and Hubble dashboard

The central Grafana dashboard **Agent RCA · Cilium & Hubble** has the stable URL
`/d/agent-rca-cilium-hubble`. Select `agent-rca-chaos-eval` in the Cluster selector.
Its [JSON source](dashboards/cilium-hubble.json) is provisioned through a labeled
ConfigMap, so it survives Pod replacement and does not depend on manual imports.

Start with scrape health and sample age, then check endpoint state, BPF map
pressure, flow verdicts, drop reason/protocol, and observation loss. All panels are
cluster-scoped. The existing Hubble exporter does not attach application workload
labels; `namespace=kube-system` identifies the exporter, not traffic ownership.
This is an operations dashboard, not Hubble UI's service map or an RCA Report.

The fault target forwards `cilium_*` and `hubble_*` metrics to the central
Prometheus with its `cluster_id`. Local receiver series without that label are
excluded. The readout does not replace missing data with zero or treat all drops
as outages. For example, unsupported ICMPv6 drops need not indicate an application
failure. Freshness and collection quality must be checked alongside any signal.
See the [Cilium metric reference](https://docs.cilium.io/en/stable/observability/metrics/).

```bash
make deploy-network-observability
make deploy-incident-worker
```

The first updates only the target monitoring release's remote-write override and
the central dashboard ConfigMap, preserving other Helm values and the Cilium
dataplane. The second patches only the independently pinned collection worker
image and runs a bounded Hubble read/normalization/projection probe; it creates no
Incident, injects no fault, and makes no LLM call. The full observability deploy
also provisions this dashboard in receiver/local profiles.

On 2026-09-08, all 12 metric panel queries returned live target series through
Grafana's Prometheus datasource. This proves dashboard connectivity, not network
fault RCA accuracy. The collection runtime now copies Hubble CLI `v1.20.1` from
the digest-pinned official Cilium image, matching the target Relay; the separately
installed host diagnostic CLI remains `v1.19.4`. These are distinct pins in
`platform/versions.yaml`. The rollout probe uses the worker's 500-flow budget and
rejects `PARTIAL`, observation gaps and truncation. Its 60-second read-only check
passed with 119 flows; retention remains `UNKNOWN`. See the
[runtime verification](../../evaluation/REPORT.md#hubble-cli-compatibility-repair)
and [Provider contract](../../contracts/providers.md#networkflowprovider).

## Private access

### Hubble UI

The fault target also runs the upstream **Hubble UI**, separate from Grafana.
Select `online-boutique` to inspect the service map and individual recent flows.
UI frontend/backend images are digest-pinned by the Cilium 1.20.1 chart.

`make deploy-hubble-ui` enables only the target's UI with a server-side Helm
preview that refuses changes to existing non-UI Deployments, DaemonSets, and
ConfigMaps. The regular Cilium bootstrap shares the same UI values. The UI has
one replica with bounded resources, a ClusterIP Service, no Ingress, and an
ingress-deny NetworkPolicy for Pod traffic. The built-in UI identity is read-only;
verification rejects Secret-read and Pod-patch access. Egress is not restricted
by this ingress-only policy. Access relies on trusted SSH/Kubernetes port-forward
permissions; there is no separate end-user login or tenant isolation in this UI.

From an SSH session on the **fault-target** VM:

```bash
sudo kubectl --kubeconfig /etc/kubernetes/admin.conf \
  --namespace kube-system port-forward \
  service/hubble-ui 32000:80 --address 127.0.0.1
```

Tunnel local port `12000` to that VM's loopback port `32000`, then open
`http://127.0.0.1:12000`. Flow details can contain internal Pod names and addresses;
do not publish raw screenshots or treat this live stream as durable Incident
Evidence. See the [official Hubble UI guide](https://docs.cilium.io/en/stable/observability/hubble/hubble-ui/).

### Controlled network Evidence verification

`tools/verify_hubble_evidence.py` runs one development-only verification of
`frontend -> productcatalogservice:3550/TCP`. It requires three distinct reference
hosts, healthy workloads, and no existing target/clusterwide policy, Chaos object,
or controlled-fault lock. It creates only a namespaced Cilium deny rule with
`enableDefaultDeny` disabled; it does not replace existing policies or alter the
Cilium release. Explicit deny semantics are described in the
[Cilium policy guide](https://docs.cilium.io/en/stable/security/policy/deny/).

```bash
PYTHONPATH=src:. .venv/bin/python tools/verify_hubble_evidence.py \
  --target-host "$RCA_TARGET_HOST" \
  --control-host "$RCA_CONTROL_HOST" \
  --observability-host "$RCA_OBSERVABILITY_HOST" \
  --ssh-user "$RCA_SSH_USER" --ssh-key "$RCA_SSH_KEY" \
  --execute --confirm-controlled-fault development
```

An independently armed target-side systemd timer removes this run's exact owned
policy and lock after 150 seconds. A local `finally` block also restores them on
completion/error; loss of the target VM or its Kubernetes API can delay cleanup.
Do not start another drill until policy removal and application recovery are
verified. The normal fault window is bounded to approximately 95 seconds plus
in-flight bounded commands, and the target watchdog remains independent of SSH.

This drill **submits an evaluation alert to Alertmanager**, with
`agent_rca_enabled=false`; it is not native Prometheus detection, an LLM run, or
network root-cause accuracy evaluation. It verifies actual stored Hubble Evidence,
the Neo4j Service event, the Frozen Context, and the deployed Agent's normal
candidate selector/read-only inspection tool without invoking the model. It does
not create an RCA Report or force the verification Incident past `ANALYZING`.
Hubble retention remains `UNKNOWN`, and warnings/partial collection stay visible.

Private before/fault/recovery probes, metric snapshots, identifiers and audit
results are saved under ignored `tmp/hubble-evidence-*/result.json`. Failed runs
remain failed artifacts; raw results and unreviewed screenshots must not be
published. Publish only a privacy-reviewed summary or capture after inspecting
the actual outcome. This standalone check
does not change the frozen three-fault/no-fault accuracy matrices or taxonomy.

For an existing run, replace the two fault-authorization flags with
`--audit-only RUN_ID`. This performs no alert submission or policy write; it
checks cleanup, unchanged Pod identities/restarts and Deployment specs, the
`ANALYZING` state, the original Context hash, Neo4j facts and Agent tool access.
It writes a separate `read-only-followup.json`, preserving the original result.

The first 2026-09-08 reference check observed three successful product requests before
the fault, three approximately five-second client timeouts during the fault, and
three successful requests after restoration. The stored sample contained 431
flows including 18 policy denials. Initial API metric series had not yet arrived;
the error counter appeared in later samples. These are client-timeout observations,
not three observed HTTP 5xx responses. The harness now waits for API baseline
series before applying a policy. The collection retained its compatibility
warning and partial quality; no LLM diagnosis was requested.

A second run at **2026-09-08 03:48:12–03:48:52 UTC** verified that baseline gate
live.
Three baseline requests returned HTTP 200 in 45–54 ms; all three fault probes
timed out in 5,029–5,033 ms while `/_healthz` still returned HTTP 200. After
policy removal, three product requests returned HTTP 200 in 42–44 ms.
The bounded stored sample contained **205 flows and 18 `POLICY_DENY` observations**;
these are flow observations, not a count of failed application requests.

The run and a separate read-only follow-up both passed: policy/lock absent,
unchanged Pod identities/restarts and Deployment specs, matching Neo4j facts,
unchanged Frozen Context hash, and successful deployed Agent tool inspection.
`PARTIAL`, completeness 0.5, `CLI_RELAY_VERSION_MISMATCH` and retention `UNKNOWN`
remained visible. The evaluation alert was resolved, the verification Incident
remained `ANALYZING`, and the Agent run count was zero. This is not a network
root-cause accuracy result and does not change the existing fault matrices.

The [root README's combined full-map capture](../../README.md#network-fault-evidence-hubble-ui)
comes from a third run at **2026-09-08 05:25:32–05:26:14 UTC**. The three baseline
requests returned HTTP 200 in 40–44 ms, the fault probes timed out in
5,025–5,034 ms, and three recovery requests returned HTTP 200 in 39–47 ms.
`/_healthz` remained HTTP 200. The stored sample contained **210 flows and
18 `POLICY_DENY` observations**; the same graph/Context/tool checks and a
separate cleanup/immutability follow-up passed with zero Agent runs.

Selecting the actual **05:25:50.893Z** policy-denied Flow in the unfiltered
namespace map displays Hubble's native red dashed marker at destination port
3550 alongside Flow Details. The marker's color, dash pattern and width are
unchanged; explanatory callouts merely point to it. Private identifier fields
are hidden, the flow table is omitted and the map/detail panel layout is fitted
for readability. No service, connection, timestamp or verdict is fabricated.
The final capture is after recovery and the map contains session-accumulated
observations: it is not an atomic fault-time topology snapshot, the stored
Frozen Context, or proof that a fault remains active. Both this run and earlier
verification records remain independent of the frozen accuracy matrices.

### Grafana and telemetry

No endpoint uses a LoadBalancer, public Ingress, or open public firewall. The
receiver exposes fixed Prometheus, Loki, and Tempo NodePorts only inside the
VPC, restricted by source/target network tags. The fault target exposes Hubble
Relay on a fixed private NodePort restricted to the RCA-control source tag.
Grafana remains ClusterIP. From an SSH session on the observability VM, start a
loopback-only port forward:

```bash
sudo kubectl --kubeconfig /etc/kubernetes/admin.conf \
  --namespace observability port-forward \
  service/monitoring-grafana 3000:80 --address 127.0.0.1
```

Create a local SSH tunnel to that VM loopback port and open
`http://127.0.0.1:3000`. Retrieve the generated Grafana admin password only when
needed; never record its output in Git or documentation:

```bash
sudo kubectl --kubeconfig /etc/kubernetes/admin.conf \
  --namespace observability get secret monitoring-grafana \
  --output jsonpath='{.data.admin-password}' | base64 --decode
```

## Storage and deletion boundary

The `agent-rca-local` StorageClass provisions under
`/var/lib/agent-rca/local-path` on the single VM and uses `Retain`. Removing a
Helm release or PVC therefore does not mean that its PV or host data was
deleted. Conversely, `Retain` is not backup: deleting the VM boot disk can still
destroy all telemetry. Before any destructive cleanup, resolve the exact PVC,
PV and host path and decide explicitly whether to archive or remove it.

The live three-domain verification proves alert delivery and current Provider
connectivity for the reference workload. It does not prove production HA,
long-term storage, backup/restore, or correctness across the full fault matrix.
