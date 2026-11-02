locals {
  services = [
    "artifactregistry.googleapis.com",
    "cloudscheduler.googleapis.com",       # scheduled revel-fga-reconcile runs
    "cloudresourcemanager.googleapis.com", # project IAM bindings
    "iam.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "sqladmin.googleapis.com", # also required by the Cloud Run Cloud SQL connector
  ]
}

resource "google_project_service" "enabled" {
  for_each = toset(local.services)

  service            = each.value
  disable_on_destroy = false
}
