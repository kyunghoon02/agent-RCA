# Agent RCA GCP dev environment

This Terraform root owns only the GCP foundation for the single-node reference
runtime:

- required project services;
- custom VPC and regional subnet;
- a dedicated VM service account without project IAM grants;
- static external IPv4 when enabled;
- source-restricted SSH and kube-apiserver firewall rules;
- one Shielded Ubuntu Compute Engine VM and balanced boot disk.

It does not install containerd, Kubernetes, Cilium/Hubble, observability,
Online Boutique or Agent RCA. Those belong to later bootstrap and deployment
layers.

## Local inputs

Copy the two example files without committing the copies:

```bash
cp backend.tfbackend.example backend.tfbackend
cp terraform.tfvars.example terraform.tfvars
```

Use the pre-existing versioned GCS bucket in `backend.tfbackend`. Set
`project_id` and replace the documentation-only IP with the operator's current
public IPv4 `/32`. The variable contracts reject `0.0.0.0/0`.

## Plan and apply

```bash
terraform init -backend-config=backend.tfbackend
terraform validate
terraform plan -var-file=terraform.tfvars -out=agent-rca-dev.tfplan
terraform show agent-rca-dev.tfplan
terraform apply agent-rca-dev.tfplan
```

Apply only the saved, reviewed plan. The static address and running VM can incur
cost. Before teardown, remove Kubernetes workloads and any source-retained data,
then run `terraform destroy -var-file=terraform.tfvars`. The GCS state bucket is
not part of this root and must be reviewed separately after all state prefixes
are no longer needed.
