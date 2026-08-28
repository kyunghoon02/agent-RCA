# Development observability stack

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
fault-target metric series, logs, and traces exist in the receiver and that a
central synthetic alert becomes a durable Incident with remote Evidence.

## Private access

No endpoint uses a LoadBalancer, public Ingress, or open public firewall. The
receiver exposes fixed Prometheus, Loki, and Tempo NodePorts only inside the
VPC, restricted by source/target network tags. Grafana remains ClusterIP. From
an SSH session on the observability VM, start a loopback-only port forward:

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
