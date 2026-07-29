output "api_url" {
  description = "Public HTTPS endpoint. Set NEXT_PUBLIC_API_BASE_URL to this in Vercel."
  value       = google_cloud_run_v2_service.api.uri
}

output "service_account_email" {
  description = "Least-privilege runtime identity for the Cloud Run service."
  value       = google_service_account.api.email
}

output "secret_ids" {
  description = "Secret Manager entries to populate before the first deploy."
  value       = { for key, secret in google_secret_manager_secret.app : key => secret.secret_id }
}

output "next_steps" {
  description = "Commands to run once `terraform apply` completes."
  value       = <<-EOT
    Secrets are created empty — Terraform never holds their values in state.
    Add a version to each before the service will start:

      printf '%s' "$ANTHROPIC_API_KEY" | gcloud secrets versions add ledgerlens-anthropic-api-key --data-file=-
      printf '%s' "$DATABASE_URL"      | gcloud secrets versions add ledgerlens-database-url --data-file=-
      printf '%s' "$LANGFUSE_PUBLIC_KEY" | gcloud secrets versions add ledgerlens-langfuse-public-key --data-file=-
      printf '%s' "$LANGFUSE_SECRET_KEY" | gcloud secrets versions add ledgerlens-langfuse-secret-key --data-file=-

    Then point the UI at the API:

      vercel env add NEXT_PUBLIC_API_BASE_URL production   # ${google_cloud_run_v2_service.api.uri}
  EOT
}
