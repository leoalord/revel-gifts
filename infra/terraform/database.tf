resource "google_sql_database_instance" "main" {
  name             = "revel-db"
  database_version = "POSTGRES_16"
  region           = var.region

  deletion_protection = var.deletion_protection

  settings {
    tier                        = var.db_tier
    edition                     = "ENTERPRISE" # shared-core tiers are Enterprise-only
    availability_type           = "ZONAL"
    disk_type                   = "PD_SSD"
    disk_size                   = 10
    disk_autoresize             = true
    disk_autoresize_limit       = 20
    deletion_protection_enabled = var.deletion_protection

    # Public IP with no authorized networks: only reachable through the Cloud
    # SQL connector (IAM-checked, encrypted), e.g. the Cloud Run /cloudsql
    # volume or the Cloud SQL Auth Proxy. Avoids VPC / private service access.
    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      start_time                     = "08:00"
      point_in_time_recovery_enabled = false
      backup_retention_settings {
        retained_backups = 7
      }
    }

    maintenance_window {
      day  = 7
      hour = 9
    }
  }

  depends_on = [google_project_service.enabled]
}

resource "google_sql_database" "app" {
  name     = "revel"
  instance = google_sql_database_instance.main.name
}

resource "google_sql_database" "openfga" {
  name     = "openfga"
  instance = google_sql_database_instance.main.name
}

# URL-safe generated passwords (no special characters) so they can be embedded
# in connection URIs without escaping. Stored in Terraform state.
resource "random_password" "app_db" {
  length  = 32
  special = false
}

resource "random_password" "openfga_db" {
  length  = 32
  special = false
}

resource "google_sql_user" "app" {
  name     = "revel_app"
  instance = google_sql_database_instance.main.name
  password = random_password.app_db.result
}

resource "google_sql_user" "openfga" {
  name     = "openfga"
  instance = google_sql_database_instance.main.name
  password = random_password.openfga_db.result
}

locals {
  sql_connection_name = google_sql_database_instance.main.connection_name
  sql_socket_dir      = "/cloudsql/${local.sql_connection_name}"
}
