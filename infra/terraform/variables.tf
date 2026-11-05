variable "project_id" {
  description = "GCP project ID."
  type        = string
  default     = "revel-gifts"
}

variable "region" {
  description = "Region for Cloud Run, Cloud SQL and Artifact Registry. Cloud Run domain mappings are available in us-central1."
  type        = string
  default     = "us-central1"
}

variable "app_image" {
  description = "Container image for revel-app and revel-migrate on first create. Defaults to Google's public hello image so the plan works before our image exists. Later deploys use gcloud (image changes are ignored by Terraform); see infra/README.md."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "openfga_image_tag" {
  description = "Pinned openfga/openfga image tag, pulled through the Artifact Registry Docker Hub remote repository. Keep in step with docker-compose.yml."
  type        = string
  default     = "v1.22.1"
}

variable "public_base_url" {
  description = "Public origin of the API (used for OAuth/MCP metadata)."
  type        = string
  default     = "https://api.revel.gifts"
}

variable "workos_authkit_domain" {
  description = "Public WorkOS AuthKit domain (issuer), e.g. https://example-tenant.authkit.app. Not a secret. Sets WORKOS_AUTHKIT_DOMAIN on revel-app only when non-empty; while it is empty, /mcp and /me fail closed with 401."
  type        = string
  default     = ""

  validation {
    condition     = var.workos_authkit_domain == "" || can(regex("^https://[A-Za-z0-9.-]+(:[0-9]+)?/?$", var.workos_authkit_domain))
    error_message = "workos_authkit_domain must be empty or an https URL with a hostname only (no path, query or fragment), e.g. https://example-tenant.authkit.app."
  }

  validation {
    condition     = !strcontains(lower(var.workos_authkit_domain), "replace-with")
    error_message = "workos_authkit_domain still contains the example placeholder (replace-with-...). Set the real AuthKit domain from the WorkOS dashboard, or leave it empty."
  }
}

variable "workos_client_id" {
  description = "WorkOS client ID (client_...) for the browser session. Not a secret. Sets WORKOS_CLIENT_ID on revel-app only when non-empty."
  type        = string
  default     = ""

  validation {
    condition     = var.workos_client_id == "" || can(regex("^[A-Za-z0-9_]+$", var.workos_client_id))
    error_message = "workos_client_id must be empty or a WorkOS client ID (letters, digits and underscores), e.g. client_01ABC."
  }

  validation {
    condition     = !strcontains(lower(var.workos_client_id), "replace-with")
    error_message = "workos_client_id still contains the example placeholder (replace-with-...). Set the real WorkOS client ID, or leave it empty."
  }
}

variable "workos_redirect_uri" {
  description = "WorkOS browser-session redirect URI (WORKOS_REDIRECT_URI). Empty means the default, public_base_url + /auth/callback (https://api.revel.gifts/auth/callback). Must be registered in the WorkOS dashboard."
  type        = string
  default     = ""

  validation {
    condition     = var.workos_redirect_uri == "" || can(regex("^https://[A-Za-z0-9.-]+(:[0-9]+)?/[^?#[:space:]]*$", var.workos_redirect_uri))
    error_message = "workos_redirect_uri must be empty (use the default) or an https URL with a path and no query or fragment."
  }
}

variable "fireworks_model" {
  description = "Fireworks model ID for Muse Spark (e.g. accounts/fireworks/models/<model>). Not a secret. Sets FIREWORKS_MODEL on revel-app only when non-empty. FIREWORKS_API_KEY is a secret, via app_secret_env."
  type        = string
  default     = ""

  validation {
    condition     = !can(regex("[[:space:]]", var.fireworks_model))
    error_message = "fireworks_model must not contain whitespace."
  }

  validation {
    condition     = !strcontains(lower(var.fireworks_model), "replace-with")
    error_message = "fireworks_model still contains the example placeholder (replace-with-...). Set the real Fireworks model ID, or leave it empty."
  }
}

variable "meta_model" {
  description = "Meta model ID for Muse Spark when muse_spark_provider is \"meta\". Not a secret. Sets META_MODEL on revel-app only when non-empty. META_API_KEY is a secret, via app_secret_env."
  type        = string
  default     = ""

  validation {
    condition     = !can(regex("[[:space:]]", var.meta_model))
    error_message = "meta_model must not contain whitespace."
  }

  validation {
    condition     = !strcontains(lower(var.meta_model), "replace-with")
    error_message = "meta_model still contains the example placeholder (replace-with-...). Set the real Meta model ID, or leave it empty."
  }
}

variable "muse_spark_provider" {
  description = "Muse Spark provider (MUSE_SPARK_PROVIDER): \"fireworks\" or \"meta\". Empty leaves it unset, so the app default (fireworks) applies. Sets MUSE_SPARK_PROVIDER on revel-app only when non-empty."
  type        = string
  default     = ""

  validation {
    condition     = contains(["", "fireworks", "meta"], var.muse_spark_provider)
    error_message = "muse_spark_provider must be empty, \"fireworks\" or \"meta\"."
  }
}

variable "muse_spark_timeout_seconds" {
  description = "Muse Spark request timeout in seconds (MUSE_SPARK_TIMEOUT_SECONDS), as a string, e.g. \"8\" or \"12.5\". Empty leaves it unset, so the app default (8.0) applies. Sets the env var on revel-app only when non-empty."
  type        = string
  default     = ""

  validation {
    condition     = var.muse_spark_timeout_seconds == "" || can(regex("^[0-9]+(\\.[0-9]+)?$", var.muse_spark_timeout_seconds))
    error_message = "muse_spark_timeout_seconds must be empty or a positive number of seconds, e.g. \"8\" or \"12.5\"."
  }

  validation {
    condition     = var.muse_spark_timeout_seconds == "" || try(tonumber(var.muse_spark_timeout_seconds) > 0, false)
    error_message = "muse_spark_timeout_seconds must be greater than zero."
  }
}

variable "openfga_store_id" {
  description = "OpenFGA store ID (a 26-character ULID) printed by `revel.auth.fga_bootstrap`. Not a secret. Sets OPENFGA_STORE_ID on revel-app and revel-fga-admin only when non-empty."
  type        = string
  default     = ""

  validation {
    condition     = var.openfga_store_id == "" || can(regex("^[0-7][0-9A-HJKMNP-TV-Z]{25}$", var.openfga_store_id))
    error_message = "openfga_store_id must be empty or an OpenFGA ULID: 26 uppercase Crockford base32 characters (no I, L, O or U), e.g. 01M6EH8A6JVN48ZKW179AHVMTC."
  }
}

variable "openfga_authorization_model_id" {
  description = "OpenFGA authorization model ID (a 26-character ULID) printed by `revel.auth.fga_bootstrap`. Not a secret. Sets OPENFGA_AUTHORIZATION_MODEL_ID on revel-app and revel-fga-admin only when non-empty."
  type        = string
  default     = ""

  validation {
    condition     = var.openfga_authorization_model_id == "" || can(regex("^[0-7][0-9A-HJKMNP-TV-Z]{25}$", var.openfga_authorization_model_id))
    error_message = "openfga_authorization_model_id must be empty or an OpenFGA ULID: 26 uppercase Crockford base32 characters (no I, L, O or U), e.g. 01M6EH8A8D7R8Z89NBZSFV1YNC."
  }
}

variable "app_extra_allowed_hosts" {
  description = "Extra Host values for revel-app's MCP transport allowlist, in addition to the PUBLIC_BASE_URL host and the computed deterministic run.app host. Use for the non-deterministic run.app hostname (from `terraform output app_urls`) after the first apply. Hostnames only, no scheme."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for h in var.app_extra_allowed_hosts : can(regex("^[a-z0-9.-]+(:[0-9]+)?$", h))])
    error_message = "app_extra_allowed_hosts entries must be bare hostnames (optionally :port), without scheme or path."
  }
}

variable "mcp_path" {
  description = "Public MCP endpoint path."
  type        = string
  default     = "/mcp"
}

variable "migrate_command" {
  description = "Command (entrypoint + args) the revel-migrate job runs. Final command is owned by F3."
  type        = list(string)
  default     = ["alembic", "upgrade", "head"]
}

variable "app_database_url_scheme" {
  description = "SQLAlchemy URL scheme for the app's DATABASE_URL (driver owned by F3/backend)."
  type        = string
  default     = "postgresql+psycopg"
}

variable "app_secret_env" {
  description = "Placeholder secrets (by secret ID, used as the env var name) to expose to revel-app. Add a name only after a version exists, or the Cloud Run deploy fails. Allowed: WORKOS_API_KEY, WORKOS_COOKIE_PASSWORD, FIREWORKS_API_KEY, META_API_KEY; RESEND_API_KEY only with enable_resend_secret. WORKOS_CLIENT_ID is not a secret; use workos_client_id."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([for s in var.app_secret_env : contains([
      "WORKOS_API_KEY", "WORKOS_COOKIE_PASSWORD", "FIREWORKS_API_KEY", "META_API_KEY",
    ], s) || (s == "RESEND_API_KEY" && var.enable_resend_secret)])
    error_message = "app_secret_env may only contain the placeholder secret IDs defined in secrets.tf."
  }
}

variable "app_max_instances" {
  description = "Maximum revel-app instances. Keep (instances x DB pool) within the Cloud SQL connection budget (db-f1-micro allows about 25; see infra/README.md, \"Connection budget\"). Raise db_tier to db-g1-small before going above 2."
  type        = number
  default     = 2
}

variable "db_tier" {
  description = "Cloud SQL machine tier. db-f1-micro (shared core, 0.6 GB, max_connections 25) is the cheapest; db-g1-small (1.7 GB) if memory/connections are short. Shared-core tiers require the ENTERPRISE edition."
  type        = string
  default     = "db-f1-micro"
}

variable "deletion_protection" {
  description = "Protect Cloud SQL and Cloud Run resources from deletion. Set false (and apply) before a deliberate terraform destroy."
  type        = bool
  default     = true
}

variable "enable_domain_mapping" {
  description = "Create the Cloud Run domain mapping for domain_name. Requires the deploying account to be a verified owner of the parent domain in Google Search Console; DNS is a separate user step."
  type        = bool
  default     = false
}

variable "domain_name" {
  description = "Custom domain mapped to revel-app."
  type        = string
  default     = "api.revel.gifts"
}

variable "enable_fga_reconcile_schedule" {
  description = "Run the revel-fga-reconcile job on fga_reconcile_schedule with Cloud Scheduler. Takes effect only once openfga_store_id and openfga_authorization_model_id are both set (reconcile exits 2 without them). Set false and apply to remove the job, the scheduler job and the revel-scheduler service account; to pause without an apply, use `gcloud scheduler jobs pause` (see infra/README.md)."
  type        = bool
  default     = true
}

variable "fga_reconcile_schedule" {
  description = "Cron schedule (time zone Etc/UTC) for revel-fga-reconcile. Keep the interval well above the job's 300s timeout so runs do not overlap."
  type        = string
  default     = "*/15 * * * *"
}

variable "invitation_background_delivery_ready" {
  description = "Opt in to instance-based CPU allocation for invitation dispatch and assert runtime readiness. Requires explicit user approval and deployed verification before true. This does not enable SMTP or provision a mail provider."
  type        = bool
  default     = false
}

variable "enable_resend_secret" {
  description = "Provision an empty RESEND_API_KEY secret and app-only accessor grant. Requires explicit infrastructure approval. Add the sending-only API key out of band; Terraform never stores its value."
  type        = bool
  default     = false
}

variable "invitation_mail_provider" {
  description = "Invitation mail provider; disabled until the approved domain/key/runtime are ready. Enabling Resend requires explicit configuration/apply approval."
  type        = string
  default     = "disabled"

  validation {
    condition     = contains(["disabled", "resend"], var.invitation_mail_provider)
    error_message = "invitation_mail_provider must be disabled or resend."
  }

  validation {
    condition = var.invitation_mail_provider != "resend" || (
      var.enable_resend_secret && var.invitation_background_delivery_ready &&
      contains(var.app_secret_env, "RESEND_API_KEY") && var.invitation_from_email != ""
    )
    error_message = "Resend requires approved secret provisioning, background CPU readiness, RESEND_API_KEY in app_secret_env after a version exists, and a verified sender."
  }
}

variable "invitation_from_email" {
  description = "Approved email address on the verified Resend sending domain. Empty until approved; no credentials."
  type        = string
  default     = ""

  validation {
    condition     = var.invitation_from_email == "" || can(regex("^[A-Za-z0-9._%+!-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}$", var.invitation_from_email))
    error_message = "Use a plain sender email address on the approved verified domain."
  }
}
