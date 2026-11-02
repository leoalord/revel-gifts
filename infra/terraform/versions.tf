terraform {
  required_version = ">= 1.9.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.9"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }

  # State is local (terraform.tfstate in this directory, gitignored) for the
  # hackathon. It contains generated database passwords and secret payloads:
  # keep it private, never commit it, and back it up out of band.
}

provider "google" {
  project = var.project_id
  region  = var.region
}
