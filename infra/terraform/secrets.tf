locals {
  # Secrets whose values are supplied out of band (gcloud secrets versions add).
  # Terraform creates the containers only; it never holds these values.
  # WORKOS_CLIENT_ID is not a secret; it is a plain env var (var.workos_client_id).
  placeholder_secrets = concat([
    "WORKOS_API_KEY",
    "WORKOS_COOKIE_PASSWORD",
    "FIREWORKS_API_KEY",
    "META_API_KEY",
  ], var.enable_resend_secret ? ["RESEND_API_KEY"] : [])
}

resource "google_secret_manager_secret" "database_url" {
  secret_id = "DATABASE_URL"
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_version" "database_url" {
  secret      = google_secret_manager_secret.database_url.id
  secret_data = "${var.app_database_url_scheme}://${google_sql_user.app.name}:${random_password.app_db.result}@/${google_sql_database.app.name}?host=${local.sql_socket_dir}"
}

resource "google_secret_manager_secret" "openfga_datastore_uri" {
  secret_id = "OPENFGA_DATASTORE_URI"
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled]
}

# pgx (OpenFGA's driver) accepts a unix-socket directory via the host query
# parameter; TLS is not used over the socket (the connector encrypts the hop).
resource "google_secret_manager_secret_version" "openfga_datastore_uri" {
  secret      = google_secret_manager_secret.openfga_datastore_uri.id
  secret_data = "postgres://${google_sql_user.openfga.name}:${random_password.openfga_db.result}@/${google_sql_database.openfga.name}?host=${local.sql_socket_dir}&sslmode=disable"
}

resource "google_secret_manager_secret" "placeholder" {
  for_each = toset(local.placeholder_secrets)

  secret_id = each.value
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled]
}

# Per-secret accessor grants (no project-wide secretAccessor).
resource "google_secret_manager_secret_iam_member" "app_database_url" {
  secret_id = google_secret_manager_secret.database_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.app.member
}

resource "google_secret_manager_secret_iam_member" "app_placeholder" {
  for_each = google_secret_manager_secret.placeholder

  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.app.member
}

resource "google_secret_manager_secret_iam_member" "openfga_datastore_uri" {
  secret_id = google_secret_manager_secret.openfga_datastore_uri.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.openfga.member
}
