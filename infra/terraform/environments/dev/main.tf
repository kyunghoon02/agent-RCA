locals {
  name_prefix = "agent-rca-${var.environment}"
  labels = {
    environment = var.environment
    managed_by  = "terraform"
    project     = "agent-rca"
  }
  required_services = toset([
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "serviceusage.googleapis.com",
    "storage.googleapis.com",
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

resource "google_service_account" "image_builder" {
  project      = var.project_id
  account_id   = "${local.name_prefix}-image-builder"
  display_name = "Agent RCA ${var.environment} image builder"

  depends_on = [google_project_service.required]
}

resource "google_artifact_registry_repository" "online_boutique" {
  project       = var.project_id
  location      = var.region
  repository_id = "${local.name_prefix}-workloads"
  description   = "Digest-pinned Agent RCA reference workload images"
  format        = "DOCKER"
  labels        = local.labels

  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "delete-old-untagged"
    action = "DELETE"

    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s"
    }
  }

  cleanup_policies {
    id     = "delete-old-instrumented-tags"
    action = "DELETE"

    condition {
      tag_state    = "TAGGED"
      tag_prefixes = ["v0.10.6-otel-"]
      older_than   = "2592000s"
    }
  }

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"

    most_recent_versions {
      keep_count = 3
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket" "cloud_build_source" {
  name                        = "${var.project_id}-${local.name_prefix}-cloudbuild-source"
  project                     = var.project_id
  location                    = var.region
  force_destroy               = true
  public_access_prevention    = "enforced"
  uniform_bucket_level_access = true
  labels                      = local.labels

  lifecycle_rule {
    action {
      type = "Delete"
    }

    condition {
      age = 1
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "image_builder_source_reader" {
  bucket = google_storage_bucket.cloud_build_source.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.image_builder.email}"
}

resource "google_artifact_registry_repository_iam_member" "vm_reader" {
  project    = var.project_id
  location   = google_artifact_registry_repository.online_boutique.location
  repository = google_artifact_registry_repository.online_boutique.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.vm.email}"
}

resource "google_artifact_registry_repository_iam_member" "image_builder_writer" {
  project    = var.project_id
  location   = google_artifact_registry_repository.online_boutique.location
  repository = google_artifact_registry_repository.online_boutique.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.image_builder.email}"
}

resource "google_project_iam_member" "image_builder_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.image_builder.email}"
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

resource "google_compute_address" "chaos_evaluation" {
  count = var.enable_chaos_evaluation_node && var.enable_external_ip ? 1 : 0

  name         = "${local.name_prefix}-chaos-eval-ipv4"
  project      = var.project_id
  region       = var.region
  address_type = "EXTERNAL"
  network_tier = "PREMIUM"

  depends_on = [google_project_service.required]
}

resource "google_compute_address" "observability" {
  count = var.enable_observability_node && var.enable_external_ip ? 1 : 0

  name         = "${local.name_prefix}-observability-ipv4"
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

resource "google_compute_firewall" "observability_ingest" {
  count = var.enable_observability_node ? 1 : 0

  name        = "${local.name_prefix}-allow-observability-ingest"
  project     = var.project_id
  network     = google_compute_network.main.name
  direction   = "INGRESS"
  priority    = 1000
  source_tags = ["${local.name_prefix}-chaos-target"]
  target_tags = ["${local.name_prefix}-observability"]

  allow {
    protocol = "tcp"
    ports    = ["30090", "30100", "30317"]
  }

  dynamic "log_config" {
    for_each = var.enable_firewall_logging ? [1] : []
    content {
      metadata = "EXCLUDE_ALL_METADATA"
    }
  }
}

resource "google_compute_firewall" "observability_query" {
  count = var.enable_observability_node ? 1 : 0

  name        = "${local.name_prefix}-allow-observability-query"
  project     = var.project_id
  network     = google_compute_network.main.name
  direction   = "INGRESS"
  priority    = 1000
  source_tags = ["${local.name_prefix}-rca-control"]
  target_tags = ["${local.name_prefix}-observability"]

  allow {
    protocol = "tcp"
    ports    = ["30090", "30100"]
  }

  dynamic "log_config" {
    for_each = var.enable_firewall_logging ? [1] : []
    content {
      metadata = "EXCLUDE_ALL_METADATA"
    }
  }
}

resource "google_compute_firewall" "rca_control_webhook" {
  count = var.enable_observability_node ? 1 : 0

  name        = "${local.name_prefix}-allow-rca-webhook"
  project     = var.project_id
  network     = google_compute_network.main.name
  direction   = "INGRESS"
  priority    = 1000
  source_tags = ["${local.name_prefix}-observability"]
  target_tags = ["${local.name_prefix}-rca-control"]

  allow {
    protocol = "tcp"
    ports    = ["30080"]
  }

  dynamic "log_config" {
    for_each = var.enable_firewall_logging ? [1] : []
    content {
      metadata = "EXCLUDE_ALL_METADATA"
    }
  }
}

resource "google_compute_firewall" "fault_target_kubernetes_api" {
  count = var.enable_observability_node ? 1 : 0

  name        = "${local.name_prefix}-allow-target-api-from-rca"
  project     = var.project_id
  network     = google_compute_network.main.name
  direction   = "INGRESS"
  priority    = 1000
  source_tags = ["${local.name_prefix}-rca-control"]
  target_tags = ["${local.name_prefix}-chaos-target"]

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

resource "google_compute_firewall" "fault_target_hubble_relay" {
  count = var.enable_chaos_evaluation_node ? 1 : 0

  name        = "${local.name_prefix}-allow-target-hubble-from-rca"
  project     = var.project_id
  network     = google_compute_network.main.name
  direction   = "INGRESS"
  priority    = 1000
  source_tags = ["${local.name_prefix}-rca-control"]
  target_tags = ["${local.name_prefix}-chaos-target"]

  allow {
    protocol = "tcp"
    ports    = ["31234"]
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
  tags = [
    "${local.name_prefix}-node",
    "${local.name_prefix}-rca-control",
  ]

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

resource "google_compute_instance" "chaos_evaluation" {
  count = var.enable_chaos_evaluation_node ? 1 : 0

  name         = "${local.name_prefix}-chaos-eval-01"
  project      = var.project_id
  zone         = var.zone
  machine_type = var.chaos_evaluation_machine_type

  allow_stopping_for_update = true
  can_ip_forward            = false
  deletion_protection       = var.deletion_protection
  labels = merge(local.labels, {
    purpose            = "chaos-evaluation"
    kubernetes_version = "1-35"
  })
  tags = [
    "${local.name_prefix}-node",
    "${local.name_prefix}-chaos-target",
  ]

  boot_disk {
    auto_delete = true

    initialize_params {
      image = var.source_image
      size  = var.chaos_evaluation_boot_disk_size_gb
      type  = "pd-balanced"
      labels = merge(local.labels, {
        purpose = "chaos-evaluation"
      })
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.main.id
    stack_type = "IPV4_ONLY"

    dynamic "access_config" {
      for_each = var.enable_external_ip ? [1] : []
      content {
        nat_ip       = google_compute_address.chaos_evaluation[0].address
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
}

resource "google_compute_instance" "observability" {
  count = var.enable_observability_node ? 1 : 0

  name         = "${local.name_prefix}-observability-01"
  project      = var.project_id
  zone         = var.zone
  machine_type = var.observability_machine_type

  allow_stopping_for_update = true
  can_ip_forward            = false
  deletion_protection       = var.deletion_protection
  labels = merge(local.labels, {
    purpose = "observability"
  })
  tags = [
    "${local.name_prefix}-node",
    "${local.name_prefix}-observability",
  ]

  boot_disk {
    auto_delete = true

    initialize_params {
      image = var.source_image
      size  = var.observability_boot_disk_size_gb
      type  = "pd-balanced"
      labels = merge(local.labels, {
        purpose = "observability"
      })
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.main.id
    stack_type = "IPV4_ONLY"

    dynamic "access_config" {
      for_each = var.enable_external_ip ? [1] : []
      content {
        nat_ip       = google_compute_address.observability[0].address
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
}
