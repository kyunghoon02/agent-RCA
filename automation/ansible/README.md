# GCP kubeadm bootstrap automation

This layer configures the already-provisioned Compute Engine VM. Terraform
continues to own the GCP network, firewall, identity, VM, disk and address; this
Ansible layer owns host prerequisites, containerd, pinned Kubernetes packages,
single-node `kubeadm init`, Cilium/Hubble and runtime verification.

It intentionally does not run `kubeadm reset`, distribute kubeconfig off the VM,
open firewall rules, or deploy Online Boutique and Agent RCA workloads.

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
```

The bootstrap is intentionally fail-fast on a host other than Ubuntu 24.04
x86_64 with cgroup v2. Package and binary versions plus download checksums are
pinned in `group_vars/all.yml` and mirrored in `platform/versions.yaml`.

`kubeadm init` is guarded by `/etc/kubernetes/admin.conf`, so a normal rerun does
not reset or reinitialize the cluster. Cilium reconciliation runs only when its
Helm release is absent or the managed values file changes. The verification
playbook performs read-only Kubernetes, Cilium and Hubble checks; its local
Hubble CLI creates and cleans up a per-query port-forward that is never exposed
publicly.

Ansible package tasks wait for the apt/dpkg lock. Never delete dpkg lock files;
if unattended upgrades are active, let them finish and rerun the playbook.
