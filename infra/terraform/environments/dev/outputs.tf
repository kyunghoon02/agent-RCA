output "instance_name" {
  description = "Compute Engine instance name used by gcloud and bootstrap automation."
  value       = google_compute_instance.node.name
}

output "instance_zone" {
  description = "Zone containing the single-node dev runtime."
  value       = google_compute_instance.node.zone
}

output "machine_type" {
  description = "Selected Compute Engine machine type."
  value       = google_compute_instance.node.machine_type
}

output "internal_ip" {
  description = "Private IPv4 address assigned to the node."
  value       = google_compute_instance.node.network_interface[0].network_ip
}

output "external_ip" {
  description = "Static public IPv4 address when external access is enabled."
  value       = var.enable_external_ip ? google_compute_address.vm[0].address : null
  sensitive   = true
}

output "chaos_evaluation_instance_name" {
  description = "Parallel Kubernetes 1.35 Chaos Mesh evaluation instance when enabled."
  value       = try(google_compute_instance.chaos_evaluation[0].name, null)
}

output "chaos_evaluation_internal_ip" {
  description = "Private IPv4 address of the Chaos Mesh evaluation node when enabled."
  value       = try(google_compute_instance.chaos_evaluation[0].network_interface[0].network_ip, null)
}

output "chaos_evaluation_external_ip" {
  description = "Static public IPv4 address of the Chaos Mesh evaluation node when enabled."
  value       = try(google_compute_address.chaos_evaluation[0].address, null)
  sensitive   = true
}

output "observability_instance_name" {
  description = "Isolated observability instance when enabled."
  value       = try(google_compute_instance.observability[0].name, null)
}

output "observability_internal_ip" {
  description = "Private IPv4 address of the isolated observability node when enabled."
  value       = try(google_compute_instance.observability[0].network_interface[0].network_ip, null)
}

output "observability_external_ip" {
  description = "Static public IPv4 address of the observability node when enabled."
  value       = try(google_compute_address.observability[0].address, null)
  sensitive   = true
}

output "network_name" {
  description = "VPC network name."
  value       = google_compute_network.main.name
}

output "subnetwork_name" {
  description = "Subnetwork name."
  value       = google_compute_subnetwork.main.name
}

output "vm_service_account" {
  description = "Dedicated identity attached to the VM."
  value       = google_service_account.vm.email
  sensitive   = true
}

output "artifact_registry_repository" {
  description = "Artifact Registry repository ID for custom reference workload images."
  value       = google_artifact_registry_repository.online_boutique.repository_id
}

output "image_builder_service_account" {
  description = "Least-privilege service account used by Cloud Build."
  value       = google_service_account.image_builder.email
  sensitive   = true
}
