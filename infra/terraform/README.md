# KT Cloud Terraform Boundary

Status: **implementation blocked by capability verification**

This directory intentionally contains no active provider or resource HCL yet.
The project does not assume that a DX-M1 tenant exposes standard OpenStack APIs or
that `terraform-provider-openstack` is compatible.

Implementation starts only after the required entries in
[`../../config/kt-cloud-capabilities.yaml`](../../config/kt-cloud-capabilities.yaml)
are verified. The future root will own KT Cloud network, compute, storage and
access resources and will expose a secret-free inventory contract to Ansible.

`make terraform-fmt` and `make terraform-validate` intentionally remain blocked
until that root exists.
