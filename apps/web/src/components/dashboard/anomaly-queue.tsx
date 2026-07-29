"use client";

import { AnimatePresence, motion } from "framer-motion";
import { CopyCheck, Hash, Loader2, ShieldCheck, TrendingUp, X } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import {
  Badge,
  EmptyState,
  Panel,
  PanelHeader,
  SeverityBadge,
  Skeleton,
} from "@/components/ui/primitives";
import { cn, formatMoney, relativeTime } from "@/lib/utils";
import type { Anomaly, AnomalySeverity, AnomalyType } from "@/types/api";

const TYPE_META: Record<AnomalyType, { label: string; icon: React.ElementType }> = {
  duplicate: { label: "Possible duplicate payment", icon: CopyCheck },
  amount_zscore: { label: "Amount outlier", icon: TrendingUp },
  term_drift: { label: "Payment terms changed", icon: Hash },
  round_number: { label: "Suspiciously round total", icon: Hash },
};

const SEVERITY_GLOW: Record<AnomalySeverity, string> = {
  HIGH: "glow-flag border-flag/30",
  MEDIUM: "glow-warn border-warn/25",
  LOW: "border-accent/20",
};

function EvidenceRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-[11px] text-ink-faint">{label}</span>
      <span className="font-mono tabular text-[11px] text-ink-muted">{value}</span>
    </div>
  );
}

function Evidence({ anomaly }: { anomaly: Anomaly }) {
  const evidence = anomaly.evidence;
  const rows: Array<[string, React.ReactNode]> = [];

  if (anomaly.anomaly_type === "duplicate") {
    if (typeof evidence.matched_invoice_number === "string") {
      rows.push(["Matched invoice", evidence.matched_invoice_number]);
    }
    if (typeof evidence.amount_gap_pct === "number") {
      rows.push(["Amount gap", `${evidence.amount_gap_pct.toFixed(2)}%`]);
    }
    if (typeof evidence.day_gap === "number") {
      rows.push(["Days apart", evidence.day_gap]);
    }
    if (typeof evidence.vendor_similarity === "number") {
      rows.push(["Vendor match", `${evidence.vendor_similarity.toFixed(0)}%`]);
    }
  } else if (anomaly.anomaly_type === "amount_zscore") {
    if (typeof evidence.history_mean === "number") {
      rows.push(["Vendor average", formatMoney(evidence.history_mean)]);
    }
    if (typeof evidence.zscore === "number") {
      rows.push(["Z-score", evidence.zscore.toFixed(2)]);
    }
    if (typeof evidence.history_count === "number") {
      rows.push(["History size", `${evidence.history_count} invoices`]);
    }
  } else if (anomaly.anomaly_type === "term_drift") {
    if (typeof evidence.modal_terms === "string") {
      rows.push(["Usual terms", evidence.modal_terms]);
    }
    if (typeof evidence.candidate_terms === "string") {
      rows.push(["This invoice", evidence.candidate_terms]);
    }
  } else if (typeof evidence.multiple_of === "number") {
    rows.push(["Multiple of", evidence.multiple_of.toLocaleString()]);
  }

  if (rows.length === 0) return null;

  return (
    <div className="mt-3 space-y-1 border-l border-hairline-strong bg-black/25 px-3 py-2">
      {rows.map(([label, value]) => (
        <EvidenceRow key={label} label={label} value={value} />
      ))}
    </div>
  );
}

export interface AnomalyQueueProps {
  anomalies: Anomaly[];
  loading: boolean;
  onResolve: (id: string, action: "approve" | "reject") => Promise<void>;
  onInspect?: (documentId: string) => void;
}

export function AnomalyQueue({
  anomalies,
  loading,
  onResolve,
  onInspect,
}: AnomalyQueueProps) {
  const [pending, setPending] = React.useState<Record<string, boolean>>({});

  const resolve = React.useCallback(
    async (id: string, action: "approve" | "reject") => {
      setPending((current) => ({ ...current, [id]: true }));
      try {
        await onResolve(id, action);
      } finally {
        setPending((current) => {
          const next = { ...current };
          delete next[id];
          return next;
        });
      }
    },
    [onResolve],
  );

  return (
    <Panel edgeLight className="flex h-full flex-col overflow-hidden">
      <PanelHeader
        eyebrow="Human in the loop"
        title="Anomaly review queue"
        action={
          anomalies.length > 0 ? (
            <Badge tone="flag">{anomalies.length} open</Badge>
          ) : null
        }
      />

      <div className="flex-1 space-y-3 overflow-y-auto px-5 py-4">
        {loading ? (
          [0, 1].map((index) => (
            <div key={index} className="bevel-sm border border-hairline p-4">
              <Skeleton className="h-3 w-32" />
              <Skeleton className="mt-3 h-3 w-full" />
              <Skeleton className="mt-2 h-3 w-4/5" />
            </div>
          ))
        ) : anomalies.length === 0 ? (
          <EmptyState
            icon={<ShieldCheck className="size-6" />}
            title="Nothing waiting for review"
            hint="Every processed document reconciled against its vendor history."
          />
        ) : (
          <AnimatePresence mode="popLayout">
            {anomalies.map((anomaly) => {
              const meta = TYPE_META[anomaly.anomaly_type];
              const Icon = meta.icon;
              const busy = pending[anomaly.id] === true;

              return (
                <motion.article
                  key={anomaly.id}
                  layout
                  initial={{ opacity: 0, y: 12 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, x: 40, transition: { duration: 0.22 } }}
                  transition={{ duration: 0.28, ease: "easeOut" }}
                  className={cn(
                    "bevel-sm border bg-white/[0.025] p-4",
                    SEVERITY_GLOW[anomaly.severity],
                  )}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex min-w-0 items-center gap-2">
                      <Icon
                        className={cn(
                          "size-4 shrink-0",
                          anomaly.severity === "HIGH"
                            ? "text-flag"
                            : anomaly.severity === "MEDIUM"
                              ? "text-warn"
                              : "text-accent",
                        )}
                      />
                      <p className="truncate text-[13px] font-semibold text-ink">
                        {meta.label}
                      </p>
                    </div>
                    <SeverityBadge severity={anomaly.severity} />
                  </div>

                  {/* The plain-English reason — spec §2: every flag carries one. */}
                  <p className="mt-2.5 text-[13px] leading-relaxed text-ink-muted">
                    {anomaly.reason}
                  </p>

                  <Evidence anomaly={anomaly} />

                  <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-ink-faint">
                    {anomaly.vendor ? <span className="truncate">{anomaly.vendor}</span> : null}
                    {anomaly.total !== null ? (
                      <span className="font-mono tabular">
                        {formatMoney(anomaly.total, anomaly.currency)}
                      </span>
                    ) : null}
                    <span className="ml-auto">{relativeTime(anomaly.created_at)}</span>
                  </div>

                  <div className="mt-3 flex items-center gap-2">
                    <Button
                      variant="approve"
                      size="sm"
                      disabled={busy}
                      onClick={() => void resolve(anomaly.id, "approve")}
                    >
                      {busy ? <Loader2 className="size-3.5 animate-spin" /> : null}
                      Approve
                    </Button>
                    <Button
                      variant="reject"
                      size="sm"
                      disabled={busy}
                      onClick={() => void resolve(anomaly.id, "reject")}
                    >
                      <X className="size-3.5" />
                      Reject
                    </Button>
                    {onInspect ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="ml-auto"
                        onClick={() => onInspect(anomaly.document_id)}
                      >
                        Audit trail
                      </Button>
                    ) : null}
                  </div>
                </motion.article>
              );
            })}
          </AnimatePresence>
        )}
      </div>
    </Panel>
  );
}
