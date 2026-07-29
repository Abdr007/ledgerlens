/**
 * Mirrors of the FastAPI response models.
 *
 * These are hand-maintained against `apps/api/app/models/api.py`. The enum
 * string values are part of the API contract on both sides, so widening one
 * without the other is a compile error here rather than a runtime surprise.
 */

export type DocumentStatus =
  | "PENDING"
  | "PROCESSING"
  | "DONE"
  | "NEEDS_REVIEW"
  | "FAILED";

export type DocumentKind = "invoice" | "receipt" | "contract" | "unknown";
export type Lane = "text" | "vision";

/** The six nodes of the pipeline visual, in order (spec §6). */
export type PipelineStage =
  | "ingest"
  | "route"
  | "extract"
  | "validate"
  | "screen"
  | "ledger";

export type StageState = "pending" | "active" | "passed" | "flagged" | "failed";

export type AnomalyType =
  | "duplicate"
  | "amount_zscore"
  | "term_drift"
  | "round_number";

export type AnomalySeverity = "LOW" | "MEDIUM" | "HIGH";
export type AnomalyStatus = "OPEN" | "APPROVED" | "REJECTED";

export interface ApiError {
  error: { code: string; message: string; details: Record<string, unknown> };
}

export interface UploadResponse {
  document_id: string;
  file_hash: string;
  filename: string;
  status: DocumentStatus;
  duplicate: boolean;
}

export interface StageProgress {
  stage: PipelineStage;
  state: StageState;
  detail: string | null;
  at: string | null;
}

export interface ValidationCheck {
  rule: string;
  passed: boolean;
  message: string;
  expected: string | null;
  observed: string | null;
}

export interface LineItem {
  description?: string | null;
  qty?: string | number | null;
  unit_price?: string | number | null;
  amount?: string | number | null;
}

export interface Extraction {
  vendor: string | null;
  invoice_number: string | null;
  issue_date: string | null;
  due_date: string | null;
  line_items: LineItem[];
  subtotal: number | null;
  tax: number | null;
  total: number | null;
  currency: string | null;
  payment_terms: string | null;
  is_valid: boolean;
  repair_attempts: number;
  model: string | null;
  lane: Lane | null;
  checks: ValidationCheck[];
}

export interface DocumentStatusResponse {
  document_id: string;
  status: DocumentStatus;
  status_reason: string | null;
  doc_kind: DocumentKind | null;
  lane: Lane | null;
  stages: StageProgress[];
  progress: number;
  is_terminal: boolean;
  latency_ms: number | null;
  cost_usd: number;
  anomaly_count: number;
  highest_severity: AnomalySeverity | null;
  extraction: Extraction | null;
  updated_at: string;
}

export interface Anomaly {
  id: string;
  document_id: string;
  anomaly_type: AnomalyType;
  severity: AnomalySeverity;
  reason: string;
  score: number | null;
  evidence: Record<string, unknown>;
  status: AnomalyStatus;
  created_at: string;
  resolved_at: string | null;
  resolved_note: string | null;
  vendor: string | null;
  total: number | null;
  currency: string | null;
  filename: string | null;
}

export interface DocumentSummary {
  id: string;
  filename: string;
  status: DocumentStatus;
  doc_kind: DocumentKind | null;
  lane: Lane | null;
  media_type: string;
  size_bytes: number;
  vendor: string | null;
  total: number | null;
  currency: string | null;
  issue_date: string | null;
  anomaly_count: number;
  highest_severity: AnomalySeverity | null;
  latency_ms: number | null;
  cost_usd: number;
  created_at: string;
}

export interface DocumentListResponse {
  items: DocumentSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface AuditEntry {
  id: number;
  event: string;
  stage: PipelineStage | null;
  actor: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface Trace {
  id: string;
  stage: PipelineStage;
  purpose: string;
  model: string;
  mode: string;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
  attempts: number;
  cost_usd: number;
  created_at: string;
}

export interface DocumentDetail {
  id: string;
  file_hash: string;
  filename: string;
  media_type: string;
  size_bytes: number;
  status: DocumentStatus;
  status_reason: string | null;
  doc_kind: DocumentKind | null;
  lane: Lane | null;
  page_count: number | null;
  latency_ms: number | null;
  cost_usd: number;
  llm_mode: string | null;
  created_at: string;
  updated_at: string;
  extraction: Extraction | null;
  anomalies: Anomaly[];
  audit: AuditEntry[];
  traces: Trace[];
}

export interface VendorSpend {
  vendor: string;
  total: number;
  invoice_count: number;
  currency: string | null;
}

export interface Stats {
  documents_total: number;
  documents_processed: number;
  documents_needs_review: number;
  documents_failed: number;
  anomalies_open: number;
  anomalies_total: number;
  avg_latency_ms: number;
  p95_latency_ms: number;
  est_cost_usd: number;
  avg_cost_per_document_usd: number;
  llm_mode: string;
  router_model: string;
  extractor_model: string;
  vendor_spend: VendorSpend[];
  status_breakdown: Record<string, number>;
  severity_breakdown: Record<string, number>;
  anomaly_type_breakdown: Record<string, number>;
}

export interface Health {
  status: "ok" | "degraded";
  version: string;
  environment: string;
  database: "up" | "down";
  llm_mode: string;
  langfuse: "enabled" | "disabled";
}

/** The six stages, in pipeline order, with the labels the UI renders. */
export const PIPELINE_STAGES: ReadonlyArray<{
  id: PipelineStage;
  label: string;
  blurb: string;
}> = [
  { id: "ingest", label: "Ingest", blurb: "SHA-256 hash · idempotency key" },
  { id: "route", label: "Route", blurb: "Haiku 4.5 · cost-aware routing" },
  { id: "extract", label: "Extract", blurb: "Sonnet 4.6 · schema-forced tool use" },
  { id: "validate", label: "Validate", blurb: "Pure Python · never an LLM" },
  { id: "screen", label: "Screen", blurb: "Duplicates · z-scores · terms" },
  { id: "ledger", label: "Ledger", blurb: "Postgres · append-only audit" },
] as const;
