###############################################################################
# LedgerLens — Google Cloud Run (always-free tier) + Secret Manager
#
# Provisions the API container declaratively: the service, its secrets, the
# runtime service account and the public invoker binding.
#
# The always-free allowance is 2,000,000 requests and 360,000 GB-seconds per
# month. The sizing below (256 MiB, scale-to-zero, capped at 2 instances) keeps
# a demo workload comfortably inside it.
#
#   cd infra/terraform
#   terraform init
#   terraform apply -var project_id=YOUR_PROJECT -var image=IMAGE_URL
###############################################################################

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  service_name = "ledgerlens-api"
  # Secrets are created here but their *values* are never in Terraform state:
  # each version is added out of band with `gcloud secrets versions add`.
  secret_ids = {
    anthropic_api_key   = "ledgerlens-anthropic-api-key"
    database_url        = "ledgerlens-database-url"
    langfuse_public_key = "ledgerlens-langfuse-public-key"
    langfuse_secret_key = "ledgerlens-langfuse-secret-key"
  }
}

# --- APIs ------------------------------------------------------------------

resource "google_project_service" "required" {
  for_each = toset([
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# --- Runtime identity ------------------------------------------------------

# A dedicated, least-privilege identity. The default compute service account is
# project-wide Editor, which this workload has no need for.
resource "google_service_account" "api" {
  account_id   = "ledgerlens-api"
  display_name = "LedgerLens API runtime"
  description  = "Runs the LedgerLens Cloud Run service; reads its own secrets only."
  depends_on   = [google_project_service.required]
}

# --- Secrets ---------------------------------------------------------------

resource "google_secret_manager_secret" "app" {
  for_each  = local.secret_ids
  secret_id = each.value

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_iam_member" "api_can_read" {
  for_each  = google_secret_manager_secret.app
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

# --- Service ---------------------------------------------------------------

resource "google_cloud_run_v2_service" "api" {
  name                = local.service_name
  location            = var.region
  deletion_protection = false
  ingress             = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.api.email
    # Scale to zero between demos: an idle service costs nothing.
    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }
    max_instance_request_concurrency = 40
    timeout                          = "300s"

    containers {
      image = var.image

      ports {
        container_port = 7860
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "ENVIRONMENT"
        value = "prod"
      }
      env {
        name  = "LOG_LEVEL"
        value = "INFO"
      }
      env {
        name  = "ALLOWED_ORIGINS"
        value = var.allowed_origins
      }
      # Cloud Run terminates TLS and forwards one hop, so client IPs used for
      # rate limiting come from the last X-Forwarded-For entry.
      env {
        name  = "TRUSTED_PROXY_COUNT"
        value = "1"
      }
      env {
        name  = "LLM_MODE"
        value = "auto"
      }

      dynamic "env" {
        for_each = local.secret_ids
        content {
          name = upper(env.key)
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.app[env.key].secret_id
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 12
        tcp_socket {
          port = 7860
        }
      }

      liveness_probe {
        initial_delay_seconds = 20
        period_seconds        = 30
        timeout_seconds       = 5
        http_get {
          path = "/health"
          port = 7860
        }
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  depends_on = [
    google_project_service.required,
    google_secret_manager_secret_iam_member.api_can_read,
  ]
}

# The demo UI calls this from a browser with no credentials, so the service is
# public. Rate limiting, the upload whitelist and the locked CORS origin are the
# controls that matter here.
resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = var.allow_public_access ? 1 : 0
  name     = google_cloud_run_v2_service.api.name
  location = google_cloud_run_v2_service.api.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}
