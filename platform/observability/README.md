# Development observability stack

This directory contains reviewable values and manifests for the GCP kubeadm
reference runtime. Helm renders the pinned upstream charts, while the small
single-binary Tempo deployment is rendered from the checked-in Kustomize base.
The repository does not vendor generated Kubernetes YAML.

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
```

The deploy command is idempotent: a release is reconciled only when it is
missing, its pinned chart version changed, or its managed values changed. Helm
uses `--atomic --wait`; an unsuccessful install is rolled back instead of being
accepted as complete.

The verify command checks Pod readiness, five Bound PVCs, private-only Services,
the absence of Ingress, Prometheus/Alertmanager/Grafana/Loki/Tempo readiness,
five Cilium/Hubble Prometheus targets with `up=1`, and a Loki stream labeled
with the expected `cluster_id` and namespace.

## Private access

No Grafana, Prometheus, Alertmanager, Loki or Tempo endpoint is exposed through
a NodePort, LoadBalancer or Ingress. From an SSH session on the VM, start a
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

This stack does not prove alert delivery, Agent RCA provider integration,
complete server-side trace coverage for every Online Boutique service,
production HA, long-term storage, or backup/restore.
