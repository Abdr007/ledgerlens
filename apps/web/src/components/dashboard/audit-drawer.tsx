"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { AnimatePresence, motion } from "framer-motion";
import { Activity, Loader2, ScrollText, X } from "lucide-react";
import * as React from "react";

import { Badge, Field, StatusBadge } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { cn, formatBytes, formatCost, formatLatency, formatTime, humaniseEvent } from "@/lib/utils";
import type { AuditEntry, DocumentDetail, PipelineStage } from "@/types/api";

const STAGE_TONE: Record<PipelineStage, string> = {
  ingest: "text-ink-muted",
  route: "text-accent",
  extract: "text-accent",
  validate: "text-pass",
  screen: "text-warn",
  ledger: "text-pass",
};

function eventTone(event: string): string {
  if (event.includes("failed")) return "text-flag";
  if (event.includes("anomaly")) return "text-warn";
  if (event.includes("passed") || event.includes("committed")) return "text-pass";
  return "text-ink-muted";
}

function AuditRow({ entry, isLast }: { entry: AuditEntry; isLast: boolean }) {
  const [open, setOpen] = React.useState(false);
  const hasPayload = Object.keys(entry.payload).length > 0;

  return (
    <li className="relative pl-6">
      {!isLast ? (
        <span className="absolute left-[7px] top-4 h-full w-px bg-hairline" aria-hidden />
      ) : null}
      <span
        className={cn(
          "absolute left-0 top-[7px] size-[11px] rotate-45 border-2 border-base bg-current",
          eventTone(entry.event),
        )}
        aria-hidden
      />
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        disabled={!hasPayload}
        className={cn(
          "w-full px-1 py-1 text-left transition-colors",
          hasPayload && "hover:bg-white/[0.04]",
        )}
      >
        <div className="flex items-baseline justify-between gap-3">
          <span className={cn("text-[13px] font-medium", eventTone(entry.event))}>
            {humaniseEvent(entry.event)}
          </span>
          <span className="shrink-0 font-mono tabular text-[11px] text-ink-faint">
            {formatTime(entry.created_at)}
          </span>
        </div>
        <div className="mt-0.5 flex items-center gap-2">
          {entry.stage ? (
            <span className={cn("text-[11px] font-medium", STAGE_TONE[entry.stage])}>
              {entry.stage}
            </span>
          ) : null}
          <span className="text-[11px] text-ink-faint">by {entry.actor}</span>
        </div>
      </button>

      <AnimatePresence>
        {open && hasPayload ? (
          <motion.pre
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="bevel-sm mt-1.5 overflow-x-auto border border-hairline bg-black/40 px-3 py-2 font-mono text-[11px] leading-relaxed text-ink-muted"
          >
            {JSON.stringify(entry.payload, null, 2)}
          </motion.pre>
        ) : null}
      </AnimatePresence>
    </li>
  );
}

export function AuditDrawer({
  documentId,
  onClose,
}: {
  documentId: string | null;
  onClose: () => void;
}) {
  const [detail, setDetail] = React.useState<DocumentDetail | null>(null);
  const [loading, setLoading] = React.useState(false);

  React.useEffect(() => {
    if (!documentId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    api
      .document(documentId)
      .then((result) => {
        if (!cancelled) setDetail(result);
      })
      .catch(() => {
        if (!cancelled) setDetail(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [documentId]);

  const totalTokens = (detail?.traces ?? []).reduce(
    (sum, trace) => sum + trace.input_tokens + trace.output_tokens,
    0,
  );

  return (
    <Dialog.Root open={documentId !== null} onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-base-sunken/85 data-[state=open]:animate-in data-[state=open]:fade-in" />
        <Dialog.Content
          aria-describedby={undefined}
          className="fixed inset-y-0 right-0 z-50 flex w-full max-w-[520px] flex-col border-l border-accent/25 bg-base-raised shadow-2xl outline-none"
        >
          <div className="flex items-start justify-between gap-4 border-b border-hairline px-5 py-4">
            <div className="min-w-0">
              <p className="eyebrow mb-1">Append-only audit trail</p>
              <Dialog.Title className="truncate text-[15px] font-semibold tracking-tight text-ink">
                {detail?.filename ?? "Document"}
              </Dialog.Title>
            </div>
            <Dialog.Close asChild>
              <button
                type="button"
                aria-label="Close audit trail"
                className="p-1.5 text-ink-muted transition-colors hover:bg-white/[0.06] hover:text-accent"
              >
                <X className="size-4" />
              </button>
            </Dialog.Close>
          </div>

          <div className="flex-1 overflow-y-auto">
            {loading ? (
              <div className="flex items-center justify-center gap-2 py-16 text-sm text-ink-muted">
                <Loader2 className="size-4 animate-spin" />
                Loading trail…
              </div>
            ) : !detail ? (
              <p className="px-5 py-16 text-center text-sm text-ink-muted">
                Could not load this document.
              </p>
            ) : (
              <>
                <section className="border-b border-hairline px-5 py-4">
                  <div className="mb-3 flex flex-wrap items-center gap-2">
                    <StatusBadge status={detail.status} />
                    {detail.lane ? (
                      <Badge tone="neutral">{detail.lane} lane</Badge>
                    ) : null}
                    {detail.doc_kind ? (
                      <Badge tone="neutral">{detail.doc_kind}</Badge>
                    ) : null}
                    {detail.llm_mode ? (
                      <Badge tone={detail.llm_mode === "live" ? "accent" : "neutral"}>
                        {detail.llm_mode}
                      </Badge>
                    ) : null}
                  </div>
                  <Field label="SHA-256" value={detail.file_hash.slice(0, 24) + "…"} mono />
                  <Field label="Size" value={formatBytes(detail.size_bytes)} mono />
                  <Field label="Latency" value={formatLatency(detail.latency_ms)} mono />
                  <Field label="Cost" value={formatCost(detail.cost_usd)} mono />
                  {detail.status_reason ? (
                    <p className="mt-2 border-l-2 border-warn bg-warn/[0.06] px-3 py-2 text-[12px] leading-relaxed text-warn/90">
                      {detail.status_reason}
                    </p>
                  ) : null}
                </section>

                {detail.traces.length > 0 ? (
                  <section className="border-b border-hairline px-5 py-4">
                    <p className="eyebrow mb-3 flex items-center gap-1.5">
                      <Activity className="size-3" />
                      Model calls · {detail.traces.length} · {totalTokens.toLocaleString()} tokens
                    </p>
                    <div className="space-y-1.5">
                      {detail.traces.map((trace) => (
                        <div
                          key={trace.id}
                          className="flex items-baseline justify-between gap-3 text-[12px]"
                        >
                          <span className="min-w-0 truncate text-ink-muted">
                            <span className={cn("font-medium", STAGE_TONE[trace.stage])}>
                              {trace.stage}
                            </span>{" "}
                            {trace.model}
                          </span>
                          <span className="shrink-0 font-mono tabular text-[11px] text-ink-faint">
                            {trace.input_tokens}/{trace.output_tokens} tok ·{" "}
                            {formatLatency(trace.latency_ms)}
                            {trace.attempts > 1 ? ` · ${trace.attempts}×` : ""}
                          </span>
                        </div>
                      ))}
                    </div>
                  </section>
                ) : null}

                <section className="px-5 py-4">
                  <p className="eyebrow mb-4 flex items-center gap-1.5">
                    <ScrollText className="size-3" />
                    {detail.audit.length} immutable events
                  </p>
                  <ol className="space-y-3">
                    {detail.audit.map((entry, index) => (
                      <AuditRow
                        key={entry.id}
                        entry={entry}
                        isLast={index === detail.audit.length - 1}
                      />
                    ))}
                  </ol>
                </section>
              </>
            )}
          </div>

          <p className="border-t border-hairline px-5 py-3 text-[11px] leading-relaxed text-ink-faint">
            This log is append-only. A database trigger rejects <code>UPDATE</code> and{" "}
            <code>DELETE</code>, so history cannot be rewritten even from a direct SQL session.
          </p>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
