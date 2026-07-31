"use client";

import { AnimatePresence, motion } from "framer-motion";
import { Activity, CircleDot, RefreshCw, Sparkles } from "lucide-react";
import * as React from "react";

import { AnomalyQueue } from "@/components/dashboard/anomaly-queue";
import { AuditDrawer } from "@/components/dashboard/audit-drawer";
import { DocumentTable } from "@/components/dashboard/document-table";
import { KpiCards } from "@/components/dashboard/kpi-cards";
import { VendorChart } from "@/components/dashboard/vendor-chart";
import { Dropzone } from "@/components/pipeline/dropzone";
import { PipelineRail } from "@/components/pipeline/pipeline-rail";
import { ResultCard } from "@/components/pipeline/result-card";
import { Button } from "@/components/ui/button";
import { Badge, Panel } from "@/components/ui/primitives";
import { ApiRequestError, api, describeUploadProblem } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  Anomaly,
  DocumentStatusResponse,
  DocumentSummary,
  Health,
  Stats,
} from "@/types/api";

/**
 * Turn a failed review decision into something the reviewer can act on.
 *
 * `invalid_state_transition` is the interesting one: the API refuses a decision
 * on a flag someone else has already settled, rather than overwriting it and
 * recording the wrong reviewer in the audit trail. That is a normal outcome of
 * two people working the same queue, so it reads as information, not as a fault.
 */
function describeResolveFailure(error: unknown): string {
  if (error instanceof ApiRequestError) {
    if (error.code === "invalid_state_transition") {
      return "Another reviewer settled that flag first — your decision was not applied. The queue below is up to date.";
    }
    if (error.code === "rate_limited") {
      return "That came too soon after the last decision. Give it a moment and try again.";
    }
    return error.message;
  }
  return "That decision could not be saved. The queue below is up to date.";
}

/** How often the pipeline visual asks the backend what actually happened. */
const POLL_INTERVAL_MS = 650;
/** Stop polling rather than hammering a stuck document for ever. */
const MAX_POLLS = 180;

export default function MissionControl() {
  const [health, setHealth] = React.useState<Health | null>(null);
  const [stats, setStats] = React.useState<Stats | null>(null);
  const [anomalies, setAnomalies] = React.useState<Anomaly[]>([]);
  const [documents, setDocuments] = React.useState<DocumentSummary[]>([]);
  const [loading, setLoading] = React.useState(true);

  const [active, setActive] = React.useState<DocumentStatusResponse | null>(null);
  const [uploading, setUploading] = React.useState(false);
  const [uploadError, setUploadError] = React.useState<string | null>(null);
  const [auditTarget, setAuditTarget] = React.useState<string | null>(null);
  const [connectionError, setConnectionError] = React.useState<string | null>(null);
  const [reviewNotice, setReviewNotice] = React.useState<string | null>(null);

  const pollRef = React.useRef<number | null>(null);

  const refreshDashboard = React.useCallback(async () => {
    try {
      const [nextStats, nextAnomalies, nextDocuments, nextHealth] = await Promise.all([
        api.stats(),
        api.anomalies({ status: "OPEN", limit: 40 }),
        api.documents({ limit: 12 }),
        api.health(),
      ]);
      setStats(nextStats);
      setAnomalies(nextAnomalies);
      setDocuments(nextDocuments.items);
      setHealth(nextHealth);
      setConnectionError(null);
    } catch (error) {
      setConnectionError(
        error instanceof ApiRequestError
          ? error.message
          : "Could not reach the LedgerLens API.",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void refreshDashboard();
  }, [refreshDashboard]);

  // Clean up any in-flight poll when the component unmounts.
  React.useEffect(
    () => () => {
      if (pollRef.current !== null) window.clearTimeout(pollRef.current);
    },
    [],
  );

  /**
   * Poll `GET /v1/documents/{id}/status` until the document reaches a terminal
   * state. The pipeline visual renders whatever this returns — spec §6 requires
   * the animation to be driven by real backend status, not a client-side timer.
   */
  const trackDocument = React.useCallback(
    (documentId: string) => {
      let polls = 0;

      const tick = async () => {
        polls += 1;
        try {
          const status = await api.documentStatus(documentId);
          setActive(status);

          if (status.is_terminal || polls >= MAX_POLLS) {
            setUploading(false);
            await refreshDashboard();
            return;
          }
        } catch {
          setUploading(false);
          return;
        }
        pollRef.current = window.setTimeout(() => void tick(), POLL_INTERVAL_MS);
      };

      void tick();
    },
    [refreshDashboard],
  );

  const handleFiles = React.useCallback(
    async (files: File[]) => {
      const file = files[0];
      if (!file) return;

      const problem = describeUploadProblem(file);
      if (problem) {
        setUploadError(problem);
        return;
      }

      setUploadError(null);
      setUploading(true);
      setActive(null);
      if (pollRef.current !== null) window.clearTimeout(pollRef.current);

      try {
        const response = await api.uploadDocument(file);
        if (response.duplicate) {
          setUploadError(
            `Already ingested — the SHA-256 of ${response.filename} matches an existing ` +
              "record, so it was not processed again.",
          );
        }
        trackDocument(response.document_id);
      } catch (error) {
        setUploading(false);
        setUploadError(
          error instanceof ApiRequestError
            ? error.message
            : "Upload failed. Please try again.",
        );
      }
    },
    [trackDocument],
  );

  const handleResolve = React.useCallback(
    async (id: string, action: "approve" | "reject") => {
      // Optimistic: the row leaves the queue at once, and the refresh in `finally`
      // is what keeps that honest — if the write did not land, the flag comes back.
      setAnomalies((current) => current.filter((anomaly) => anomaly.id !== id));
      setReviewNotice(null);
      try {
        await api.resolveAnomaly(id, action);
      } catch (error) {
        // Never swallow this. The row has already gone, so a silent failure reads
        // as success — the reviewer walks away believing they cleared a flag that
        // is still open, or that they made a decision a colleague actually made
        // the other way. Two reviewers on one queue is the ordinary case, not an
        // exotic one, so the conflict needs to say so in plain words.
        setReviewNotice(describeResolveFailure(error));
      } finally {
        await refreshDashboard();
      }
    },
    [refreshDashboard],
  );

  const modeLabel = health?.llm_mode === "live" ? "Claude live" : "Offline engine";
  // Three states, not two. Before the first health response resolves we do not
  // know anything — and on a free tier that first response can take the better
  // part of a minute while the container and the database wake. Rendering
  // "unreachable" during that window states as fact something we have not
  // established, and it is the first thing a visitor sees.
  const ledgerState = health === null ? (loading ? "waking" : "unknown") : health.database;

  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-[1400px] flex-col gap-6 px-4 py-6 sm:px-6 lg:px-8">
      {/* ---------------------------------------------------------------- */}
      {/* Header                                                            */}
      {/* ---------------------------------------------------------------- */}
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="bevel-sm flex size-10 items-center justify-center border border-accent/40 bg-accent/10 glow-accent">
            <Sparkles className="size-5 text-accent" strokeWidth={1.9} />
          </div>
          <div>
            <h1 className="font-mono text-[17px] font-bold uppercase leading-tight tracking-[0.14em] text-ink">
              Ledger<span className="text-accent">Lens</span>
            </h1>
            <p className="text-[11px] text-ink-faint">
              Intelligent document processing &amp; financial anomaly detection
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Badge
            tone={
              ledgerState === "up" ? "pass" : ledgerState === "waking" ? "neutral" : "flag"
            }
          >
            <CircleDot className={cn("size-3", ledgerState === "waking" && "breathe")} />
            {ledgerState === "up"
              ? "Ledger online"
              : ledgerState === "waking"
                ? "Waking server"
                : "Ledger unreachable"}
          </Badge>
          <Badge tone={health?.llm_mode === "live" ? "accent" : "neutral"}>
            <Activity className="size-3" />
            {modeLabel}
          </Badge>
          {stats ? (
            <Badge tone="neutral">
              {stats.router_model} → {stats.extractor_model}
            </Badge>
          ) : null}
          <Button
            variant="ghost"
            size="icon"
            aria-label="Refresh dashboard"
            onClick={() => void refreshDashboard()}
          >
            <RefreshCw className={cn("size-4", loading && "animate-spin")} />
          </Button>
        </div>
      </header>

      {/* Suppressed while the first load is still in flight: the client already
          retries a cold start for ~50 s, so an error shown before that budget is
          spent would be reporting a failure that has not happened yet. */}
      {connectionError && !loading ? (
        <div className="border-l-2 border-flag bg-flag/[0.07] px-4 py-3 text-[13px] text-flag/90">
          {connectionError}
        </div>
      ) : null}

      {/* ---------------------------------------------------------------- */}
      {/* Hero: drop zone + live pipeline                                    */}
      {/* ---------------------------------------------------------------- */}
      <Panel edgeLight className="ticks p-5 sm:p-6">
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
          <div className="flex flex-col gap-4">
            <div>
              <p className="eyebrow">Ingestion edge</p>
              <h2 className="mt-1 text-[20px] font-semibold leading-snug tracking-tight text-ink">
                Drop a document. Watch it clear the pipeline.
              </h2>
              <p className="mt-1.5 text-[13px] leading-relaxed text-ink-muted">
                LLMs extract, code verifies. The model never checks its own math — a
                deterministic layer does, and anything that fails routes to the review
                queue instead of the database.
              </p>
            </div>
            <Dropzone
              onFiles={(files) => void handleFiles(files)}
              busy={uploading}
              error={uploadError}
            />
          </div>

          <div className="bevel-sm flex flex-col justify-center border border-hairline bg-black/25 p-5">
            <PipelineRail
              stages={active?.stages ?? null}
              caption={
                active
                  ? active.is_terminal
                    ? `Completed in ${active.latency_ms ?? 0} ms · ${active.anomaly_count} anomaly flag(s)`
                    : "Polling GET /v1/documents/{id}/status — every node reflects a recorded event"
                  : "Six stages, driven by the append-only audit log"
              }
            />
          </div>
        </div>

        <AnimatePresence>
          {active?.extraction ? (
            <motion.div
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.35 }}
              className="mt-6"
            >
              <ResultCard
                extraction={active.extraction}
                filename={documents.find((d) => d.id === active.document_id)?.filename}
                animate
              />
            </motion.div>
          ) : null}
        </AnimatePresence>
      </Panel>

      {/* ---------------------------------------------------------------- */}
      {/* KPIs                                                              */}
      {/* ---------------------------------------------------------------- */}
      <KpiCards stats={stats} loading={loading} />

      {/* ---------------------------------------------------------------- */}
      {/* Spend + review queue                                              */}
      {/* ---------------------------------------------------------------- */}
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <VendorChart data={stats?.vendor_spend ?? []} loading={loading} />
        <div className="min-h-[420px]">
          <AnomalyQueue
            anomalies={anomalies}
            loading={loading}
            notice={reviewNotice}
            onDismissNotice={() => setReviewNotice(null)}
            onResolve={handleResolve}
            onInspect={setAuditTarget}
          />
        </div>
      </div>

      {/* ---------------------------------------------------------------- */}
      {/* Ledger                                                            */}
      {/* ---------------------------------------------------------------- */}
      <DocumentTable documents={documents} loading={loading} onInspect={setAuditTarget} />

      <footer className="pb-2 pt-1 text-center text-[11px] text-ink-faint">
        Every state change is written to an append-only audit log · deterministic
        validation in pure Python · {stats?.llm_mode === "live" ? "Claude" : "offline"}{" "}
        extraction traced end to end
      </footer>

      <AuditDrawer documentId={auditTarget} onClose={() => setAuditTarget(null)} />
    </main>
  );
}
