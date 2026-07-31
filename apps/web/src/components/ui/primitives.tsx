"use client";

import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";

import { cn } from "@/lib/utils";
import type { AnomalySeverity, DocumentStatus } from "@/types/api";

/* -------------------------------------------------------------------------- */
/* Panel — the chamfered, hard-edged surface everything sits on                */
/* -------------------------------------------------------------------------- */

export function Panel({
  className,
  edgeLight = false,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { edgeLight?: boolean }) {
  return (
    <div className={cn("panel", edgeLight && "edge-light", className)} {...props} />
  );
}

export function PanelHeader({
  title,
  eyebrow,
  action,
  className,
}: {
  title: React.ReactNode;
  eyebrow?: string;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex items-start justify-between gap-4 px-5 pt-5", className)}>
      <div className="min-w-0">
        {eyebrow ? <p className="eyebrow mb-1.5">{eyebrow}</p> : null}
        <h2 className="truncate font-engraved text-[19px] leading-tight tracking-tight text-ink">{title}</h2>
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Badge                                                                       */
/* -------------------------------------------------------------------------- */

// Square, mono, with a solid leading rule. A pill would read as a web app;
// this reads as a readout.
const badgeVariants = cva(
  "inline-flex items-center gap-1.5 border-l-2 px-2 py-[3px] font-mono text-[10px] " +
    "font-medium uppercase tracking-[0.12em] whitespace-nowrap",
  {
    variants: {
      tone: {
        neutral: "border-l-ink-faint bg-white/[0.035] text-ink-muted",
        accent: "border-l-accent bg-accent/10 text-accent",
        pass: "border-l-pass bg-pass/10 text-pass",
        warn: "border-l-warn bg-warn/10 text-warn",
        flag: "border-l-flag bg-flag/10 text-flag",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export function Badge({
  className,
  tone,
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}

const STATUS_TONE: Record<DocumentStatus, VariantProps<typeof badgeVariants>["tone"]> = {
  PENDING: "neutral",
  PROCESSING: "accent",
  DONE: "pass",
  NEEDS_REVIEW: "warn",
  FAILED: "flag",
};

const STATUS_LABEL: Record<DocumentStatus, string> = {
  PENDING: "Pending",
  PROCESSING: "Processing",
  DONE: "Done",
  NEEDS_REVIEW: "Needs review",
  FAILED: "Failed",
};

export function StatusBadge({ status }: { status: DocumentStatus }) {
  return (
    <Badge tone={STATUS_TONE[status]}>
      <span
        className={cn("size-1.5 bg-current", status === "PROCESSING" && "breathe")}
      />
      {STATUS_LABEL[status]}
    </Badge>
  );
}

const SEVERITY_TONE: Record<AnomalySeverity, VariantProps<typeof badgeVariants>["tone"]> = {
  LOW: "accent",
  MEDIUM: "warn",
  HIGH: "flag",
};

export function SeverityBadge({ severity }: { severity: AnomalySeverity }) {
  return <Badge tone={SEVERITY_TONE[severity]}>{severity}</Badge>;
}

/* -------------------------------------------------------------------------- */
/* Skeleton & empty state                                                      */
/* -------------------------------------------------------------------------- */

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("animate-pulse bg-white/[0.06]", className)} />;
}

export function EmptyState({
  icon,
  title,
  hint,
  className,
}: {
  icon?: React.ReactNode;
  title: string;
  hint?: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-2 px-6 py-12 text-center",
        className,
      )}
    >
      {icon ? <div className="mb-1 text-ink-faint">{icon}</div> : null}
      <p className="text-sm font-medium text-ink-muted">{title}</p>
      {hint ? <p className="max-w-sm text-xs text-ink-faint">{hint}</p> : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Key/value row                                                               */
/* -------------------------------------------------------------------------- */

export function Field({
  label,
  value,
  mono = false,
  className,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("flex items-baseline justify-between gap-4 py-1.5", className)}>
      <span className="shrink-0 text-xs text-ink-faint">{label}</span>
      <span
        className={cn(
          "min-w-0 truncate text-right text-sm text-ink",
          mono && "font-mono tabular text-[13px]",
        )}
      >
        {value}
      </span>
    </div>
  );
}
