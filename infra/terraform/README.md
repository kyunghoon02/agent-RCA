# GCP Terraform Boundary

Status: **dev root implemented and applied; Kubernetes bootstrap not started**

Terraform will own two explicit state boundaries:

1. `bootstrap`: a pre-existing GCS state bucket with Object Versioning,
   Uniform bucket-level access and Public Access Prevention;
2. `environments/dev`: APIs, VPC/subnet, firewall, dedicated VM service
   account, Compute Engine VM, disk, optional external IP, and cost-relevant
   lifecycle outputs.

Kubernetes application, observability, and Agent RCA workloads remain outside
Terraform and are applied through the Kubernetes deployment layer. No service
account key, token, project secret, or state file belongs in this repository.

The Google provider and Compute Engine root are implemented under
`environments/dev`. Host prerequisites, containerd, kubeadm, Cilium/Hubble and
Kubernetes workloads remain separate bootstrap/deployment responsibilities.
The first dev apply was verified on 2026-08-22. Runtime inputs and remaining
operational gaps are recorded in
[`../../config/gcp-readiness.yaml`](../../config/gcp-readiness.yaml).

The GCS bucket must exist before backend initialization. Do not put credentials
or the bucket name in committed backend files. Copy the example files locally,
then supply their ignored values to `terraform init` and `terraform plan`.

```bash
make terraform-fmt
make terraform-validate

terraform -chdir=infra/terraform/environments/dev init \
  -backend-config=backend.tfbackend
terraform -chdir=infra/terraform/environments/dev plan \
  -var-file=terraform.tfvars \
  -out=agent-rca-dev.tfplan
terraform -chdir=infra/terraform/environments/dev apply agent-rca-dev.tfplan
```

`terraform.tfvars`, `backend.tfbackend`, `.terraform/`, state and saved plans are
ignored. Review a saved plan before applying it. Destroy the Kubernetes workload
layer first, then destroy this environment root; the remote state bucket is a
separate bootstrap boundary and is not destroyed with the VM.
