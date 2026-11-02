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
  description = "Placeholder secrets (by secret ID, used as the env var name) to expose to revel-app. Add a name only after a version exists, or the Cloud Run deploy fails. Allowed: WORKOS_API_KEY, WORKOS_COOKIE_PASSWORD, FIREWORKS_API_KEY, META_API_KEY. WORKOS_CLIENT_ID is not a secret; use workos_client_id."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([for s in var.app_secret_env : contains([
      "WORKOS_API_KEY", "WORKOS_COOKIE_PASSWORD", "FIREWORKS_API_KEY", "META_API_KEY",
    ], s)])
    error_message = "app_secret_env may only contain the placeholder secret IDs defined in secrets.tf."
  }
}

variable "app_max_instances" {
  description = "Maximum revel-app instances. Keep (instances x DB pool) within the Cloud SQL connection budget (db-f1-micro allows 25)."
  type        = number
  default     = 3
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
