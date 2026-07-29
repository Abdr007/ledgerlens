"use client";

import { animate, motion, useMotionValue, useTransform } from "framer-motion";
import { AlertTriangle, Coins, FileCheck2, Timer } from "lucide-react";
import * as React from "react";

import { Panel, Skeleton } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import type { Stats } from "@/types/api";

/** A number that counts up to its value, and re-counts only when it changes. */
function Counter({
  value,
  format,
  className,
}: {
  value: number;
  format: (value: number) => string;
  className?: string;
}) {
  const motionValue = useMotionValue(0);
  const text = useTransform(motionValue, (latest) => format(latest));

  React.useEffect(() => {
    const controls = animate(motionValue, value, {
      duration: 0.9,
      ease: [0.22, 1, 0.36, 1],
    });
    return () => controls.stop();
  }, [motionValue, value]);

  return <motion.span className={cn("tabular", className)}>{text}</motion.span>;
}

const METER_CELLS = 16;

/**
 * A segmented readout, not a progress bar.
 *
 * `ratio` must be a real fraction of a real denominator — the caption names it
 * so nobody has to guess what the bar is measuring. Cards with no honest
 * denominator pass `ratio: null` and get a plain rule instead of invented fill.
 */
function Meter({
  ratio,
  caption,
  tone,
}: {
  ratio: number | null;
  caption: string;
  tone: KpiDefinition["tone"];
}) {
  if (ratio === null) {
    return (
      <div className="mt-3.5 flex items-center gap-2">
        <div className="h-px flex-1 bg-hairline" />
        <span className="font-mono text-[9px] uppercase tracking-[0.14em] text-ink-faint">
          {caption}
        </span>
      </div>
    );
  }

  const clamped = Math.max(0, Math.min(1, ratio));
  // Any non-zero ratio lights at least one cell, so "small but present" never
  // renders as "nothing".
  const lit = clamped === 0 ? 0 : Math.max(1, Math.round(clamped * METER_CELLS));

  return (
    <div className="mt-3.5">
      <div
        className="meter"
        role="meter"
        aria-valuenow={Math.round(clamped * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={caption}
      >
        {Array.from({ length: METER_CELLS }, (_, cell) => (
          <span
            key={cell}
            className="meter-cell"
            data-on={cell < lit}
            data-tone={tone}
            aria-hidden
          />
        ))}
      </div>
      <p className="mt-1.5 font-mono text-[9px] uppercase tracking-[0.14em] text-ink-faint">
        {caption}
      </p>
    </div>
  );
}

interface KpiDefinition {
  key: string;
  label: string;
  icon: React.ElementType;
  value: number;
  format: (value: number) => string;
  footnote: string;
  tone: "accent" | "pass" | "warn" | "flag";
  ratio: number | null;
  caption: string;
}

const TONE_RING: Record<KpiDefinition["tone"], string> = {
  accent: "text-accent",
  pass: "text-pass",
  warn: "text-warn",
  flag: "text-flag",
};

/** The spec's own end-to-end latency target, in milliseconds. */
const LATENCY_TARGET_MS = 30_000;

function formatMs(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${Math.round(ms)}ms`;
}

export function KpiCards({ stats, loading }: { stats: Stats | null; loading: boolean }) {
  if (loading || !stats) {
    return (
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[0, 1, 2, 3].map((index) => (
          <Panel key={index} className="p-5">
            <Skeleton className="h-3 w-24" />
            <Skeleton className="mt-4 h-8 w-20" />
            <Skeleton className="mt-4 h-1 w-full" />
            <Skeleton className="mt-3 h-2.5 w-28" />
          </Panel>
        ))}
      </div>
    );
  }

  const cards: KpiDefinition[] = [
    {
      key: "processed",
      label: "Documents processed",
      icon: FileCheck2,
      value: stats.documents_processed,
      format: (value) => Math.round(value).toLocaleString(),
      footnote: `${stats.documents_total} ingested · ${stats.documents_needs_review} in review`,
      tone: "accent",
      ratio: stats.documents_total
        ? stats.documents_processed / stats.documents_total
        : 0,
      caption: "of ingested",
    },
    {
      key: "latency",
      label: "Avg latency",
      icon: Timer,
      value: stats.avg_latency_ms,
      format: formatMs,
      footnote: `p95 ${formatMs(stats.p95_latency_ms)} · target < 30s`,
      tone: "pass",
      ratio: stats.p95_latency_ms / LATENCY_TARGET_MS,
      caption: "p95 vs 30s target",
    },
    {
      key: "cost",
      label: "Estimated cost",
      icon: Coins,
      value: stats.est_cost_usd,
      format: (value) => (value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`),
      footnote: `$${stats.avg_cost_per_document_usd.toFixed(5)} per document`,
      tone: "accent",
      // No fixed spend ceiling exists, so there is nothing honest to fill against.
      ratio: null,
      caption: "run total",
    },
    {
      key: "anomalies",
      label: "Anomalies open",
      icon: AlertTriangle,
      value: stats.anomalies_open,
      format: (value) => Math.round(value).toLocaleString(),
      footnote: `${stats.anomalies_total} raised in total`,
      tone: stats.anomalies_open > 0 ? "flag" : "pass",
      ratio: stats.anomalies_total ? stats.anomalies_open / stats.anomalies_total : 0,
      caption: "open of raised",
    },
  ];

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {cards.map((card, index) => {
        const Icon = card.icon;
        return (
          <motion.div
            key={card.key}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: index * 0.06, duration: 0.35, ease: "easeOut" }}
          >
            <Panel edgeLight className="panel-hover h-full p-5">
              <div className="flex items-center justify-between">
                <p className="eyebrow">{card.label}</p>
                <Icon className={cn("size-4", TONE_RING[card.tone])} strokeWidth={1.9} />
              </div>
              <p className="stat mt-3 text-[28px] font-semibold leading-none text-ink">
                <Counter value={card.value} format={card.format} />
              </p>
              <Meter ratio={card.ratio} caption={card.caption} tone={card.tone} />
              <p className="mt-2.5 text-[11px] text-ink-faint">{card.footnote}</p>
            </Panel>
          </motion.div>
        );
      })}
    </div>
  );
}
