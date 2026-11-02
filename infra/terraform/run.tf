############################################
# OpenFGA (internal authorization service)
############################################

locals {
  openfga_env = {
    OPENFGA_DATASTORE_ENGINE         = "postgres"
    OPENFGA_DATASTORE_MAX_OPEN_CONNS = "5"
    OPENFGA_DATASTORE_MAX_IDLE_CONNS = "2"
    OPENFGA_HTTP_ADDR                = "0.0.0.0:8080"
    OPENFGA_PLAYGROUND_ENABLED       = "false"
    OPENFGA_METRICS_ENABLED          = "false"
    OPENFGA_LOG_FORMAT               = "json"
  }
}

# Reachability: ingress is "all" but every request must carry a Google ID token
# for a principal with roles/run.invoker, and only revel-app's service account
# has it (iam.tf). Only port 8080 (HTTP API) is exposed by Cloud Run; gRPC and
# the playground are not reachable.
resource "google_cloud_run_v2_service" "openfga" {
  name                 = "revel-openfga"
  location             = var.region
  ingress              = "INGRESS_TRAFFIC_ALL"
  invoker_iam_disabled = false
  deletion_protection  = var.deletion_protection

  template {
    service_account                  = google_service_account.openfga.email
    max_instance_request_concurrency = 80

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = local.openfga_image
      args  = ["run"]

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      dynamic "env" {
        for_each = local.openfga_env
        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name = "OPENFGA_DATASTORE_URI"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.openfga_datastore_uri.secret_id
            version = "latest"
          }
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
      # Default TCP startup probe: OpenFGA's /healthz reports NOT_SERVING until
      # `openfga migrate` has run, so an HTTP probe would block the first apply.
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [local.sql_connection_name]
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_version.openfga_datastore_uri,
    google_secret_manager_secret_iam_member.openfga_datastore_uri,
    google_project_iam_member.openfga_sql_client,
  ]
}

# Explicit OpenFGA datastore migration step:
#   gcloud run jobs execute revel-openfga-migrate --region us-central1 --wait
resource "google_cloud_run_v2_job" "openfga_migrate" {
  name                = "revel-openfga-migrate"
  location            = var.region
  deletion_protection = var.deletion_protection

  template {
    task_count = 1
    template {
      service_account = google_service_account.openfga.email
      max_retries     = 0
      timeout         = "600s"

      containers {
        image = local.openfga_image
        args  = ["migrate"]

        env {
          name  = "OPENFGA_DATASTORE_ENGINE"
          value = "postgres"
        }
        env {
          name = "OPENFGA_DATASTORE_URI"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.openfga_datastore_uri.secret_id
              version = "latest"
            }
          }
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
      }

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [local.sql_connection_name]
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_version.openfga_datastore_uri,
    google_secret_manager_secret_iam_member.openfga_datastore_uri,
    google_project_iam_member.openfga_sql_client,
  ]
}

############################################
# Revel application (public API + MCP)
############################################

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  app_service_name = "revel-app"

  # The scaffold's MCP transport allowlist rejects any Host other than the PUBLIC_BASE_URL host.
  # Before DNS exists the app is reached at its run.app URL, so allow that host too. The
  # deterministic URL is https://SERVICE-PROJECT_NUMBER.REGION.run.app
  # (https://docs.cloud.google.com/run/docs/triggering/https-request). The non-deterministic
  # (hash) URL cannot be predicted; add it through app_extra_allowed_hosts after the first
  # apply if it is needed (see infra/README.md).
  app_run_app_hosts = distinct(concat(
    ["${local.app_service_name}-${data.google_project.this.number}.${var.region}.run.app"],
    var.app_extra_allowed_hosts,
  ))

  # MCP resource indicator / expected WorkOS access-token audience. Built the same way as
  # Settings.mcp_resource_url (base without trailing slash + "/" + path) and set explicitly so it
  # stays https://api.revel.gifts/mcp even when the app is reached on its run.app host. It must
  # equal the resource indicator configured in WorkOS.
  app_mcp_resource_url = "${trimsuffix(var.public_base_url, "/")}/${trim(var.mcp_path, "/")}"

  # WorkOS browser-session callback; defaults to https://api.revel.gifts/auth/callback.
  app_workos_redirect_uri = var.workos_redirect_uri != "" ? var.workos_redirect_uri : "${trimsuffix(var.public_base_url, "/")}/auth/callback"

  # OpenFGA store/model IDs from revel.auth.fga_bootstrap (not secrets). Shared by revel-app and
  # revel-fga-admin; each is set only when non-empty.
  openfga_ids_env = merge(
    var.openfga_store_id == "" ? {} : { OPENFGA_STORE_ID = var.openfga_store_id },
    var.openfga_authorization_model_id == "" ? {} : { OPENFGA_AUTHORIZATION_MODEL_ID = var.openfga_authorization_model_id },
  )

  app_env = merge(
    {
      APP_ENV               = "production"
      PUBLIC_BASE_URL       = var.public_base_url
      MCP_PATH              = var.mcp_path
      WORKOS_TOKEN_AUDIENCE = local.app_mcp_resource_url
      WORKOS_REDIRECT_URI   = local.app_workos_redirect_uri
      ALLOWED_HOSTS         = join(",", local.app_run_app_hosts)
      ALLOWED_ORIGINS       = join(",", [for h in local.app_run_app_hosts : "https://${h}"])
      # Canonical run.app URL; the app also uses it as the Google ID-token audience.
      OPENFGA_API_URL     = google_cloud_run_v2_service.openfga.uri
      OPENFGA_GOOGLE_AUTH = "true"
    },
    # Public AuthKit issuer (not a secret). Unset until configured: /mcp and /me fail closed.
    var.workos_authkit_domain == "" ? {} : { WORKOS_AUTHKIT_DOMAIN = var.workos_authkit_domain },
    # Public WorkOS client ID (not a secret), for the browser session.
    var.workos_client_id == "" ? {} : { WORKOS_CLIENT_ID = var.workos_client_id },
    local.openfga_ids_env,
  )

  # revel-fga-admin runs F4's bootstrap/reconcile CLIs with the app's OpenFGA client settings.
  fga_admin_env = merge(
    {
      APP_ENV             = "production"
      PUBLIC_BASE_URL     = var.public_base_url
      OPENFGA_API_URL     = google_cloud_run_v2_service.openfga.uri
      OPENFGA_GOOGLE_AUTH = "true"
    },
    local.openfga_ids_env,
  )
}

resource "google_cloud_run_v2_service" "app" {
  name                 = local.app_service_name
  location             = var.region
  ingress              = "INGRESS_TRAFFIC_ALL"
  invoker_iam_disabled = false
  deletion_protection  = var.deletion_protection

  template {
    service_account                  = google_service_account.app.email
    max_instance_request_concurrency = 80
    timeout                          = "300s"

    scaling {
      min_instance_count = 0
      max_instance_count = var.app_max_instances
    }

    containers {
      image = var.app_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      dynamic "env" {
        for_each = local.app_env
        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.database_url.secret_id
            version = "latest"
          }
        }
      }

      dynamic "env" {
        for_each = toset(var.app_secret_env)
        content {
          name = env.value
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.placeholder[env.value].secret_id
              version = "latest"
            }
          }
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
      # Default TCP startup probe on 8080 (kept deliberately: it works for the placeholder
      # image and our image alike). Any HTTP probe or deployed check must use /health, not
      # /healthz: Cloud Run reserves some paths ending in "z"
      # (https://docs.cloud.google.com/run/docs/known-issues, "Reserved URL paths").
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [local.sql_connection_name]
      }
    }
  }

  lifecycle {
    # Images are rolled out with `gcloud run deploy --image` (see infra/README.md).
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_secret_manager_secret_version.database_url,
    google_secret_manager_secret_iam_member.app_database_url,
    google_secret_manager_secret_iam_member.app_placeholder,
    google_project_iam_member.app_sql_client,
  ]
}

# Explicit application migration step (never run on replica start):
#   gcloud run jobs execute revel-migrate --region us-central1 --wait
resource "google_cloud_run_v2_job" "app_migrate" {
  name                = "revel-migrate"
  location            = var.region
  deletion_protection = var.deletion_protection

  template {
    task_count = 1
    template {
      service_account = google_service_account.app.email
      max_retries     = 0
      timeout         = "600s"

      containers {
        image   = var.app_image
        command = slice(var.migrate_command, 0, 1)
        args    = slice(var.migrate_command, 1, length(var.migrate_command))

        env {
          name  = "APP_ENV"
          value = "production"
        }
        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.database_url.secret_id
              version = "latest"
            }
          }
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
      }

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [local.sql_connection_name]
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_secret_manager_secret_version.database_url,
    google_secret_manager_secret_iam_member.app_database_url,
    google_project_iam_member.app_sql_client,
  ]
}

# OpenFGA store/model administration (F4's revel.auth.fga_bootstrap / fga_reconcile), run
# explicitly with per-execution args (see infra/README.md, "OpenFGA bootstrap"):
#   gcloud run jobs execute revel-fga-admin --region us-central1 --wait
# The default args are the read-only `--check` probe. Originally created with gcloud; adopted by
# the import block below.
import {
  to = google_cloud_run_v2_job.fga_admin
  id = "projects/${var.project_id}/locations/${var.region}/jobs/revel-fga-admin"
}

resource "google_cloud_run_v2_job" "fga_admin" {
  name                = "revel-fga-admin"
  location            = var.region
  deletion_protection = var.deletion_protection

  template {
    task_count = 1
    template {
      service_account = google_service_account.app.email
      max_retries     = 0
      timeout         = "300s"

      containers {
        image   = var.app_image
        command = ["python"]
        args    = ["-m", "revel.auth.fga_bootstrap", "--check"]

        dynamic "env" {
          for_each = local.fga_admin_env
          content {
            name  = env.key
            value = env.value
          }
        }
        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.database_url.secret_id
              version = "latest"
            }
          }
        }

        resources {
          limits = {
            # Same as "1"; written as the API stores it for this gcloud-created job, so the
            # import has no cosmetic diff.
            cpu    = "1000m"
            memory = "512Mi"
          }
        }

        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
      }

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [local.sql_connection_name]
        }
      }
    }
  }

  lifecycle {
    # Image rolls out with `gcloud run jobs update --image`. Args are NOT ignored: Terraform keeps
    # the job's default args at the read-only `--check`. Per-execution overrides
    # (`gcloud run jobs execute --args`) do not change the job spec, so they are not drift.
    ignore_changes = [
      template[0].template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_secret_manager_secret_version.database_url,
    google_secret_manager_secret_iam_member.app_database_url,
    google_project_iam_member.app_sql_client,
  ]
}

############################################
# Custom domain (gated; needs user steps)
############################################

resource "google_cloud_run_domain_mapping" "api" {
  count = var.enable_domain_mapping ? 1 : 0

  name     = var.domain_name
  location = var.region

  metadata {
    namespace = var.project_id
  }

  spec {
    route_name = google_cloud_run_v2_service.app.name
  }
}
