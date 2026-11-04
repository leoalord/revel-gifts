terraform {
  required_version = ">= 1.9.0"
  backend "local" {}
  required_providers {
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 8.9"
    }
  }
}

variable "project_id" {
  type    = string
  default = "revel-gifts"
}

provider "google-beta" {
  project = var.project_id
}

resource "google_project_service" "firebase" {
  provider = google-beta
  for_each = toset(["firebase.googleapis.com", "firebasehosting.googleapis.com"])

  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

resource "google_firebase_project" "public_site" {
  provider = google-beta
  project  = var.project_id

  depends_on = [google_project_service.firebase]
}

resource "google_firebase_hosting_site" "public_site" {
  provider = google-beta
  project  = google_firebase_project.public_site.project
  site_id  = "revel-gifts-public"
}

resource "google_firebase_hosting_custom_domain" "apex" {
  provider = google-beta
  project  = google_firebase_project.public_site.project
  site_id  = google_firebase_hosting_site.public_site.site_id

  custom_domain         = "revel.gifts"
  wait_dns_verification = false
}

resource "google_firebase_hosting_custom_domain" "www" {
  provider = google-beta
  project  = google_firebase_project.public_site.project
  site_id  = google_firebase_hosting_site.public_site.site_id

  custom_domain         = "www.revel.gifts"
  redirect_target       = google_firebase_hosting_custom_domain.apex.custom_domain
  wait_dns_verification = false
}

output "required_dns_updates" {
  value = {
    apex = google_firebase_hosting_custom_domain.apex.required_dns_updates
    www  = google_firebase_hosting_custom_domain.www.required_dns_updates
  }
}
