# GCP Terraform Boundary

Status: **design ready; root not implemented; runtime inputs unverified**

Terraform will own two explicit state boundaries:

1. `bootstrap`: the pre-existing GCS state bucket contract, Object Versioning,
   and least-privilege access guidance;
2. `environments/dev`: APIs, VPC/subnet and secondary ranges, GKE Standard,
   node pools, IAM/Workload Identity, and cost-relevant lifecycle outputs.

Kubernetes application, observability, and Agent RCA workloads remain outside
Terraform and are applied through the Kubernetes deployment layer. No service
account key, token, project secret, or state file belongs in this repository.

The Google provider and GKE design are selected. Actual `plan/apply` remains
blocked until the runtime inputs in
[`../../config/gcp-readiness.yaml`](../../config/gcp-readiness.yaml) are verified.

`make terraform-fmt` and `make terraform-validate` intentionally return an
unimplemented status until the first active Terraform root is added.
