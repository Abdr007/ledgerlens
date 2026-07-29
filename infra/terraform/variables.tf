variable "project_id" {
  type        = string
  description = "Google Cloud project that will host the Cloud Run service."
}

variable "region" {
  type        = string
  description = "Cloud Run region. Pick one near your users; europe-west1 suits the UAE/EU demo."
  default     = "europe-west1"
}

variable "image" {
  type        = string
  description = <<-EOT
    Fully-qualified container image for the API, e.g.
    europe-west1-docker.pkg.dev/PROJECT/ledgerlens/api:1.0.0
    Build it with: docker build -f infra/Dockerfile -t IMAGE .
  EOT
}

variable "allowed_origins" {
  type        = string
  description = "Comma-separated CORS allow-list. Your Vercel domain, never '*'."
  default     = "https://ledgerlens.vercel.app"

  validation {
    condition     = !can(regex("\\*", var.allowed_origins))
    error_message = "allowed_origins must name explicit origins; a wildcard defeats the CORS lock."
  }
}

variable "max_instances" {
  type        = number
  description = "Instance ceiling. Low by design: it bounds spend on the always-free tier."
  default     = 2

  validation {
    condition     = var.max_instances >= 1 && var.max_instances <= 10
    error_message = "max_instances must be between 1 and 10 to stay inside the free allowance."
  }
}

variable "allow_public_access" {
  type        = bool
  description = "Grant roles/run.invoker to allUsers so the browser demo can reach the API."
  default     = true
}
