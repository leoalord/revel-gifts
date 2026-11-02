resource "google_service_account" "app" {
  account_id   = "revel-app"
  display_name = "Revel app (Cloud Run runtime and migrate job)"

  depends_on = [google_project_service.enabled]
}

resource "google_service_account" "openfga" {
  account_id   = "revel-openfga"
  display_name = "Revel OpenFGA (Cloud Run runtime and migrate job)"

  depends_on = [google_project_service.enabled]
}

# Cloud SQL connector access. Cloud SQL has no instance-level IAM, so this is
# project-level; both runtimes only receive database credentials for their own
# database via Secret Manager.
resource "google_project_iam_member" "app_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = google_service_account.app.member
}

resource "google_project_iam_member" "openfga_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = google_service_account.openfga.member
}

# Only revel-app may invoke OpenFGA. No allUsers/allAuthenticatedUsers binding.
resource "google_cloud_run_v2_service_iam_member" "openfga_invoker_app" {
  name     = google_cloud_run_v2_service.openfga.name
  location = google_cloud_run_v2_service.openfga.location
  role     = "roles/run.invoker"
  member   = google_service_account.app.member
}

# revel-app is the public API; application-level auth (WorkOS) protects it.
resource "google_cloud_run_v2_service_iam_member" "app_public" {
  name     = google_cloud_run_v2_service.app.name
  location = google_cloud_run_v2_service.app.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}
