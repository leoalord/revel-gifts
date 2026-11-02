# Scheduled OpenFGA reconcile: Cloud Scheduler runs the revel-fga-reconcile job (run.tf) through
# the Cloud Run Admin API. Gated by enable_fga_reconcile_schedule, and only once both OpenFGA IDs
# are set: reconcile exits 2 while OpenFGA is unconfigured, so earlier runs could only fail.

locals {
  fga_reconcile_enabled = (
    var.enable_fga_reconcile_schedule
    && var.openfga_store_id != ""
    && var.openfga_authorization_model_id != ""
  )
}

# Identity Cloud Scheduler uses to call the Cloud Run Admin API. Its only grant is
# roles/run.invoker on the revel-fga-reconcile job (no project-level roles).
resource "google_service_account" "scheduler" {
  count = local.fga_reconcile_enabled ? 1 : 0

  account_id   = "revel-scheduler"
  display_name = "Revel Cloud Scheduler (runs revel-fga-reconcile)"

  depends_on = [google_project_service.enabled]
}

# run.invoker on a job includes run.jobs.run (execute with the job's own args) but not
# run.jobs.runWithOverrides, so the scheduler cannot change what the job runs.
resource "google_cloud_run_v2_job_iam_member" "fga_reconcile_scheduler_invoker" {
  count = local.fga_reconcile_enabled ? 1 : 0

  project  = var.project_id
  location = google_cloud_run_v2_job.fga_reconcile[0].location
  name     = google_cloud_run_v2_job.fga_reconcile[0].name
  role     = "roles/run.invoker"
  member   = google_service_account.scheduler[0].member
}

resource "google_cloud_scheduler_job" "fga_reconcile" {
  count = local.fga_reconcile_enabled ? 1 : 0

  name        = "revel-fga-reconcile"
  description = "Runs the revel-fga-reconcile Cloud Run job (backstop OpenFGA reconcile)."
  region      = var.region
  schedule    = var.fga_reconcile_schedule
  time_zone   = "Etc/UTC"

  # Bounds the jobs:run call, which returns once the execution has started, not the run itself.
  attempt_deadline = "60s"

  # Retries a failed jobs:run call (for example a transient 5xx). A failed execution (non-zero
  # exit, such as 3 for "re-run reconcile") is not retried here; the next scheduled run repairs it.
  retry_config {
    retry_count          = 2
    min_backoff_duration = "30s"
    max_backoff_duration = "120s"
    max_doublings        = 2
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.fga_reconcile[0].name}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler[0].email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_cloud_run_v2_job_iam_member.fga_reconcile_scheduler_invoker,
  ]
}
