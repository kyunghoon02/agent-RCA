# GCP kubeadm bootstrap automation

This layer configures the already-provisioned Compute Engine VM. Terraform
continues to own the GCP network, firewall, identity, VM, disk and address; this
Ansible layer owns host prerequisites, containerd, pinned Kubernetes packages,
single-node `kubeadm init`, Cilium/Hubble, the development observability stack
and the Online Boutique target workload with runtime verification. It also
reconciles the three-domain RCA wiring after Terraform creates the reviewed
private firewall paths.

It intentionally does not run `kubeadm reset`, distribute an admin kubeconfig
off a VM, or create public application ingress.

## Directory map

```text
inventories/  SSH targets only; real files are ignored, examples are tracked
group_vars/   shared pins plus control, target and observability profiles
playbooks/    thin workflow entrypoints selected by Make targets
roles/        reusable bootstrap, stack, verification, wiring and evaluation units
```

Roles use these suffixes consistently:

- `*_stack`: reconcile one deployed component.
- `*_verify`: perform read-only runtime checks for that component.
- `*_harness`: own one bounded fault or control experiment and its restoration.
- `three_domain_*`: wire private access between the target, observability and
  RCA control domains.

`controlled_fault_evaluation` owns the shared post-injection path used by the
image-pull and missing-ConfigMap harnesses: Alertmanager submission, Context and
Agent completion waits, Evidence export, private Ground Truth scoring and alert
resolution. Each scenario harness retains only its own baseline, injection,
watchdog and exact restoration logic. OOM and no-fault remain separate because
their external workload, Chaos Mesh and post-run attestation contracts differ.

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
make deploy-viewer-frontend
make verify-viewer-frontend
make deploy-three-domain
```

## Chaos evaluation inventory

The parallel Chaos Mesh node uses a separate ignored inventory so its reviewed
Kubernetes 1.35 pin cannot downgrade the existing v1.36 runtime:

```bash
cp automation/ansible/inventories/chaos-eval.example.yml \
  automation/ansible/inventories/chaos-eval.yml
export ANSIBLE_INVENTORY=automation/ansible/inventories/chaos-eval.yml
make bootstrap-kubernetes
make deploy-observability
make deploy-online-boutique
make deploy-chaos-mesh
make verify-chaos-mesh
```

Populate the ignored inventory only after applying the reviewed opt-in
Terraform plan. Chaos Mesh is namespace-scoped to `online-boutique`; its
dashboard remains ClusterIP with security mode enabled, and installation
verification refuses to pass while any PodChaos, NetworkChaos or StressChaos
resource is active. StateGraph and Incident Platform run in the RCA control
domain, not on this fault target.

## Three-domain deployment

Copy all three example inventories to their ignored counterparts and populate
only SSH/runtime inputs. Then `make deploy-three-domain` prepares a namespace-
bounded read-only ServiceAccount on the fault target, points the RCA control
workers at the remote Kubernetes API and central Prometheus/Loki, configures the
central authenticated Alertmanager webhook, and runs one non-fault delivery
probe. After successful verification, legacy copies are scaled to zero rather
than deleted; their PVCs remain Bound.

The target credential is a lab-scoped, long-lived ServiceAccount token stored
only in Kubernetes Secrets. Its RBAC denies Secret reads, but it still requires
explicit rotation and is not a production workload-identity design.

The Incident Viewer frontend runs only in the RCA control domain. Its Service is
`ClusterIP`, the deployment automation rejects any Incident Platform Ingress,
and its NetworkPolicy denies inbound Pod traffic while allowing only DNS and the
private Viewer API. `make deploy-viewer-frontend` updates this component without
rewiring the other two domains. Access it through an SSH-wrapped Kubernetes
port-forward; do not add a public firewall rule or Service type.

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
ServiceMonitors. Verification requires Bound PVCs, profile-specific private
Services, no Ingress, healthy Prometheus/Alertmanager/Grafana/Loki/Tempo endpoints,
Cilium/Hubble `up=1` targets and one normalized Kubernetes log stream from Loki.
A normal rerun does not create new Helm revisions.

The Online Boutique playbook renders the SHA-pinned Kustomize overlay on the
local controller and copies the rendered result to the VM before applying it.
The upstream external frontend and in-cluster load generator are removed, all
11 target services remain ClusterIP, and Redis is pinned by OCI digest. The
overlay also deploys an internal OpenTelemetry Collector and enables tracing
for all ten application services. Verification requires the exact
12-Deployment/service set, completed rollouts, the approved instrumentation
environment, a stable restart count without OOM termination, a working
frontend, no target PVC, a normalized Loki log, a healthy Prometheus telemetry
target, non-zero span-derived RED metrics and at least one Tempo trace.

This is a single-node development storage boundary. `Retain` protects a PV from
automatic deletion with its claim or release, but the data still resides on the
VM boot disk and is neither highly available nor backed up.
