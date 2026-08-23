# GCP kubeadm bootstrap automation

This layer configures the already-provisioned Compute Engine VM. Terraform
continues to own the GCP network, firewall, identity, VM, disk and address; this
Ansible layer owns host prerequisites, containerd, pinned Kubernetes packages,
single-node `kubeadm init`, Cilium/Hubble, the development observability stack
and the Online Boutique target workload with runtime verification.

It intentionally does not run `kubeadm reset`, distribute kubeconfig off the VM,
open firewall rules, or deploy Agent RCA workloads.

## Local controller setup

```bash
make bootstrap-ansible
cp automation/ansible/inventories/dev.example.yml \
  automation/ansible/inventories/dev.yml
```

Use Terraform output for the VM address. Connect once with `gcloud compute ssh`
so OS Login registers the SSH key, then run `whoami` on the VM. Put that POSIX
username, the external address and the local Google Compute Engine private-key
path in the ignored `dev.yml`. Do not commit account email, project ID, address,
credentials or kubeconfig.

## Validate and apply

```bash
make ansible-syntax
make ansible-ping
make bootstrap-kubernetes
make verify-kubernetes
make render-observability
make deploy-observability
make verify-observability
make render-online-boutique
make deploy-online-boutique
make verify-online-boutique
```

The bootstrap is intentionally fail-fast on a host other than Ubuntu 24.04
x86_64 with cgroup v2. Package and binary versions plus download checksums are
pinned in `group_vars/all.yml` and mirrored in `platform/versions.yaml`.

`kubeadm init` is guarded by `/etc/kubernetes/admin.conf`, so a normal rerun does
not reset or reinitialize the cluster. Cilium reconciliation runs only when its
Helm release is absent or the managed values file changes. The verification
playbook performs read-only Kubernetes, Cilium and Hubble checks; its local
Hubble CLI creates and cleans up a per-query port-forward that is never exposed
publicly. Verification also rejects a degraded systemd state, a missing kubeadm
certificate, or a certificate with less than 30 days remaining.

Ansible package tasks wait for the apt/dpkg lock. Never delete dpkg lock files;
if unattended upgrades are active, let them finish and rerun the playbook.

The observability playbook installs pinned Helm releases for local-path storage,
kube-prometheus-stack, Loki in monolithic mode and Alloy, then applies the
digest-pinned monolithic Tempo manifest. It also reconciles Cilium/Hubble
ServiceMonitors. Verification requires five Bound PVCs, only ClusterIP services,
no Ingress, healthy Prometheus/Alertmanager/Grafana/Loki/Tempo endpoints,
Cilium/Hubble `up=1` targets and one normalized Kubernetes log stream from Loki.
A normal rerun does not create new Helm revisions.

The Online Boutique playbook renders the SHA-pinned Kustomize overlay on the
local controller and copies the rendered result to the VM before applying it.
The upstream external frontend and in-cluster load generator are removed, all
11 target services remain ClusterIP, and Redis is pinned by OCI digest. The
overlay also deploys an internal OpenTelemetry Collector and enables the pinned
upstream direct tracing code on seven services. Verification requires the exact
12-Deployment/service set, completed rollouts, the approved instrumentation
environment, a stable restart count without OOM termination, a working
frontend, no target PVC, a normalized Loki log, a healthy Prometheus telemetry
target, non-zero span-derived RED metrics and at least one Tempo trace. The
three remaining services retain the server-side tracing gap documented in
`platform/online-boutique/README.md`.

This is a single-node development storage boundary. `Retain` protects a PV from
automatic deletion with its claim or release, but the data still resides on the
VM boot disk and is neither highly available nor backed up.
