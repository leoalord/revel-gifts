# Our application images.
resource "google_artifact_registry_repository" "revel" {
  location      = var.region
  repository_id = "revel"
  format        = "DOCKER"
  description   = "Revel Gifts application images"

  depends_on = [google_project_service.enabled]
}

# Pull-through cache for Docker Hub so Cloud Run pulls openfga/openfga from
# Artifact Registry (avoids Docker Hub rate limits and keeps image pulls in GCP).
resource "google_artifact_registry_repository" "dockerhub" {
  location      = var.region
  repository_id = "dockerhub"
  format        = "DOCKER"
  mode          = "REMOTE_REPOSITORY"
  description   = "Docker Hub remote (pull-through) repository"

  remote_repository_config {
    description = "Docker Hub"
    docker_repository {
      public_repository = "DOCKER_HUB"
    }
  }

  depends_on = [google_project_service.enabled]
}

locals {
  registry_host = "${var.region}-docker.pkg.dev"
  app_repo_path = "${local.registry_host}/${var.project_id}/${google_artifact_registry_repository.revel.repository_id}"
  openfga_image = "${local.registry_host}/${var.project_id}/${google_artifact_registry_repository.dockerhub.repository_id}/openfga/openfga:${var.openfga_image_tag}"
}
