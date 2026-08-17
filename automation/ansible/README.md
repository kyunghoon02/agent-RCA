# KT Cloud Self-managed Kubernetes Automation

Status: **implementation gated by KT Cloud capability and topology decisions**

Ansible will configure existing KT Cloud VMs after Terraform provisioning.
It will not create or delete cloud resources.

Planned responsibilities:

- validate node OS, kernel, network and time synchronization prerequisites;
- install and pin the container runtime and Kubernetes packages;
- initialize the control plane and join workers idempotently;
- install and validate Cilium and Hubble;
- apply namespace and read-only RBAC boundaries;
- validate node, CNI, DNS and workload readiness;
- record rerun, reboot and recovery evidence.

Exact distribution, versions, inventory fields and network settings remain
unimplemented until the KT Cloud capability matrix and topology ADR are complete.
The pinned controller dependencies are retained so the automation environment can
be reproduced once implementation begins.

```bash
make bootstrap-ansible
```

`make validate-ansible` intentionally remains blocked until active playbooks exist.
