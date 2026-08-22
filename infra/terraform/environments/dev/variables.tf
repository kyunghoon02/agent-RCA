variable "project_id" {
  description = "GCP project that owns the Agent RCA dev runtime."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid GCP project ID."
  }
}

variable "region" {
  description = "GCP region for the dev runtime."
  type        = string
  default     = "asia-northeast3"
}

variable "zone" {
  description = "GCP zone for the single-node dev runtime."
  type        = string
  default     = "asia-northeast3-a"
}

variable "environment" {
  description = "Short environment label used in names and labels."
  type        = string
  default     = "dev"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,10}$", var.environment))
    error_message = "environment must be a short lowercase label."
  }
}

variable "subnet_cidr" {
  description = "Primary IPv4 CIDR for the Agent RCA subnet."
  type        = string
  default     = "10.42.0.0/24"

  validation {
    condition     = can(cidrhost(var.subnet_cidr, 1))
    error_message = "subnet_cidr must be a valid IPv4 CIDR."
  }
}

variable "machine_type" {
  description = "Compute Engine machine type for the single-node cluster."
  type        = string
  default     = "e2-standard-4"
}

variable "boot_disk_size_gb" {
  description = "Balanced persistent boot disk size in GiB."
  type        = number
  default     = 100

  validation {
    condition     = var.boot_disk_size_gb >= 50 && var.boot_disk_size_gb <= 500
    error_message = "boot_disk_size_gb must be between 50 and 500 GiB."
  }
}

variable "source_image" {
  description = "Pinned image family used for host bootstrap."
  type        = string
  default     = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
}

variable "enable_external_ip" {
  description = "Reserve and attach a static external IPv4 address."
  type        = bool
  default     = true
}

variable "ssh_source_ranges" {
  description = "Trusted IPv4 CIDRs allowed to reach SSH. Keep this to operator /32 ranges."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for cidr in var.ssh_source_ranges :
      can(cidrhost(cidr, 0)) && can(regex("^[0-9.]+/32$", cidr))
    ])
    error_message = "ssh_source_ranges must contain valid IPv4 /32 operator addresses."
  }
}

variable "kubernetes_api_source_ranges" {
  description = "Trusted IPv4 CIDRs allowed to reach the kube-apiserver on TCP 6443."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for cidr in var.kubernetes_api_source_ranges :
      can(cidrhost(cidr, 0)) && can(regex("^[0-9.]+/32$", cidr))
    ])
    error_message = "kubernetes_api_source_ranges must contain valid IPv4 /32 operator addresses."
  }
}

variable "enable_firewall_logging" {
  description = "Enable VPC firewall logging. This can add Cloud Logging cost."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Protect the dev VM from deletion. Keep false for reproducible destroy tests."
  type        = bool
  default     = false
}
