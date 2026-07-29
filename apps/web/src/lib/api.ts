/**
 * Typed client for the LedgerLens API.
 *
 * Every failure is normalised into `ApiRequestError` carrying the server's
 * error code, so the UI can react to `rate_limited` or `file_too_large`
 * specifically instead of showing "something went wrong".
 */

import type {
  Anomaly,
  AnomalyStatus,
  ApiError,
  AuditEntry,
  DocumentDetail,
  DocumentListResponse,
  DocumentStatusResponse,
  Health,
  Stats,
  UploadResponse,
} from "@/types/api";

export const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:7860"
).replace(/\/$/, "");

export class ApiRequestError extends Error {
  readonly code: string;
  readonly status: number;
  readonly details: Record<string, unknown>;

  constructor(message: string, code: string, status: number, details: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiRequestError";
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

function isApiError(value: unknown): value is ApiError {
  if (typeof value !== "object" || value === null || !("error" in value)) return false;
  const inner = (value as { error: unknown }).error;
  return typeof inner === "object" && inner !== null && "code" in inner && "message" in inner;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      cache: "no-store",
      headers: { Accept: "application/json", ...(init?.headers ?? {}) },
    });
  } catch (cause) {
    throw new ApiRequestError(
      `Cannot reach the LedgerLens API at ${API_BASE_URL}. Is it running?`,
      "network_error",
      0,
      { cause: String(cause) },
    );
  }

  if (response.status === 204) return undefined as T;

  const payload: unknown = await response.json().catch(() => null);

  if (!response.ok) {
    if (isApiError(payload)) {
      throw new ApiRequestError(
        payload.error.message,
        payload.error.code,
        response.status,
        payload.error.details,
      );
    }
    throw new ApiRequestError(
      `Request failed with status ${response.status}.`,
      "unknown_error",
      response.status,
    );
  }

  return payload as T;
}

export const api = {
  health: () => request<Health>("/health"),

  stats: () => request<Stats>("/v1/stats"),

  uploadDocument: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<UploadResponse>("/v1/documents", { method: "POST", body });
  },

  documentStatus: (id: string) => request<DocumentStatusResponse>(`/v1/documents/${id}/status`),

  document: (id: string) => request<DocumentDetail>(`/v1/documents/${id}`),

  documentAudit: (id: string) => request<AuditEntry[]>(`/v1/documents/${id}/audit`),

  documents: (params: { limit?: number; offset?: number; status?: string } = {}) => {
    const query = new URLSearchParams();
    if (params.limit !== undefined) query.set("limit", String(params.limit));
    if (params.offset !== undefined) query.set("offset", String(params.offset));
    if (params.status) query.set("status", params.status);
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return request<DocumentListResponse>(`/v1/documents${suffix}`);
  },

  anomalies: (params: { status?: AnomalyStatus; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.status) query.set("status", params.status);
    if (params.limit !== undefined) query.set("limit", String(params.limit));
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return request<Anomaly[]>(`/v1/anomalies${suffix}`);
  },

  resolveAnomaly: (id: string, action: "approve" | "reject", note?: string) =>
    request<Anomaly>(`/v1/anomalies/${id}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, note: note ?? null }),
    }),
};

/** Client-side pre-check so an obviously invalid file never costs a round trip. */
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
export const ACCEPTED_TYPES = ["application/pdf", "image/png", "image/jpeg"] as const;

export function describeUploadProblem(file: File): string | null {
  if (file.size === 0) return `${file.name} is empty.`;
  if (file.size > MAX_UPLOAD_BYTES) {
    return `${file.name} is ${(file.size / 1024 / 1024).toFixed(1)} MB — the limit is 10 MB.`;
  }
  const type = file.type.split(";")[0]?.trim().toLowerCase() ?? "";
  const looksAccepted =
    (ACCEPTED_TYPES as readonly string[]).includes(type) ||
    /\.(pdf|png|jpe?g)$/i.test(file.name);
  if (!looksAccepted) return `${file.name} is not a PDF, PNG or JPEG.`;
  return null;
}
