locals {
  name_prefix = "agent-rca-${var.environment}"
  labels = {
    environment = var.environment
    managed_by  = "terraform"
    project     = "agent-rca"
  }
  required_services = toset([
    "compute.googleapis.com",
    "iam.googleapis.com",
    "serviceusage.googleapis.com",
  ])
}

resource "google_project_service" "required" {
  for_each = local.required_services

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "main" {
  name                    = "${local.name_prefix}-vpc"
  project                 = var.project_id
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"

  depends_on = [google_project_service.required]
}

resource "google_compute_subnetwork" "main" {
  name                     = "${local.name_prefix}-subnet"
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.main.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true
  stack_type               = "IPV4_ONLY"
}

resource "google_service_account" "vm" {
  project      = var.project_id
  account_id   = "${local.name_prefix}-vm"
  display_name = "Agent RCA ${var.environment} VM"

  depends_on = [google_project_service.required]
}

resource "google_compute_address" "vm" {
  count = var.enable_external_ip ? 1 : 0

  name         = "${local.name_prefix}-ipv4"
  project      = var.project_id
  region       = var.region
  address_type = "EXTERNAL"
  network_tier = "PREMIUM"

  depends_on = [google_project_service.required]
}

resource "google_compute_firewall" "ssh" {
  count = length(var.ssh_source_ranges) > 0 ? 1 : 0

  name          = "${local.name_prefix}-allow-ssh"
  project       = var.project_id
  network       = google_compute_network.main.name
  direction     = "INGRESS"
  priority      = 1000
  source_ranges = var.ssh_source_ranges
  target_tags   = ["${local.name_prefix}-node"]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  dynamic "log_config" {
    for_each = var.enable_firewall_logging ? [1] : []
    content {
      metadata = "EXCLUDE_ALL_METADATA"
    }
  }
}

resource "google_compute_firewall" "kubernetes_api" {
  count = length(var.kubernetes_api_source_ranges) > 0 ? 1 : 0

  name          = "${local.name_prefix}-allow-kube-api"
  project       = var.project_id
  network       = google_compute_network.main.name
  direction     = "INGRESS"
  priority      = 1000
  source_ranges = var.kubernetes_api_source_ranges
  target_tags   = ["${local.name_prefix}-node"]

  allow {
    protocol = "tcp"
    ports    = ["6443"]
  }

  dynamic "log_config" {
    for_each = var.enable_firewall_logging ? [1] : []
    content {
      metadata = "EXCLUDE_ALL_METADATA"
    }
  }
}

resource "google_compute_instance" "node" {
  name         = "${local.name_prefix}-node-01"
  project      = var.project_id
  zone         = var.zone
  machine_type = var.machine_type

  allow_stopping_for_update = true
  can_ip_forward            = false
  deletion_protection       = var.deletion_protection
  labels                    = local.labels
  tags                      = ["${local.name_prefix}-node"]

  boot_disk {
    auto_delete = true

    initialize_params {
      image = var.source_image
      size  = var.boot_disk_size_gb
      type  = "pd-balanced"
      labels = merge(local.labels, {
        purpose = "kubernetes-node"
      })
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.main.id
    stack_type = "IPV4_ONLY"

    dynamic "access_config" {
      for_each = var.enable_external_ip ? [1] : []
      content {
        nat_ip       = google_compute_address.vm[0].address
        network_tier = "PREMIUM"
      }
    }
  }

  metadata = {
    block-project-ssh-keys = "TRUE"
    enable-oslogin         = "TRUE"
    serial-port-enable     = "FALSE"
  }

  service_account {
    email  = google_service_account.vm.email
    scopes = ["cloud-platform"]
  }

  scheduling {
    automatic_restart   = true
    on_host_maintenance = "MIGRATE"
    provisioning_model  = "STANDARD"
  }

  shielded_instance_config {
    enable_integrity_monitoring = true
    enable_secure_boot          = true
    enable_vtpm                 = true
  }

  lifecycle {
    precondition {
      condition     = !var.enable_external_ip || length(var.ssh_source_ranges) > 0
      error_message = "At least one trusted SSH source CIDR is required when external IP is enabled."
    }
  }

  depends_on = [google_project_service.required]
}
