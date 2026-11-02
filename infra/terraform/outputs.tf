output "app_url" {
  description = "revel-app run.app URL."
  value       = google_cloud_run_v2_service.app.uri
}

output "app_urls" {
  description = "All revel-app URLs (deterministic and non-deterministic run.app). Add a host missing from app_allowed_hosts to app_extra_allowed_hosts."
  value       = google_cloud_run_v2_service.app.urls
}

output "app_allowed_hosts" {
  description = "run.app hosts passed to revel-app as ALLOWED_HOSTS (https:// origins go to ALLOWED_ORIGINS)."
  value       = local.app_run_app_hosts
}

output "openfga_url" {
  description = "revel-openfga run.app URL (IAM-authenticated; only revel-app may invoke)."
  value       = google_cloud_run_v2_service.openfga.uri
}

output "sql_connection_name" {
  description = "Cloud SQL instance connection name (for the Auth Proxy and /cloudsql sockets)."
  value       = local.sql_connection_name
}

output "artifact_repository" {
  description = "Docker repository path for application images."
  value       = local.app_repo_path
}

output "openfga_image" {
  description = "OpenFGA image as pulled through the Docker Hub remote repository."
  value       = local.openfga_image
}

output "service_accounts" {
  description = "Runtime service account emails."
  value = {
    app     = google_service_account.app.email
    openfga = google_service_account.openfga.email
  }
}

output "placeholder_secrets" {
  description = "Secret IDs that need versions added out of band before being listed in app_secret_env."
  value       = local.placeholder_secrets
}

output "dns_instructions" {
  description = "DNS record(s) required for the custom domain."
  value = var.enable_domain_mapping ? {
    records = [for r in google_cloud_run_domain_mapping.api[0].status[0].resource_records : "${r.name} ${r.type} ${r.rrdata}"]
    } : {
    records = ["(domain mapping disabled) after enabling: ${var.domain_name} CNAME ghs.googlehosted.com."]
  }
}
