"use client";

import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, CheckCircle2, FileText, Wrench } from "lucide-react";
import * as React from "react";

import { Badge, Panel, PanelHeader } from "@/components/ui/primitives";
import { cn, formatDate, formatMoney } from "@/lib/utils";
import type { Extraction } from "@/types/api";

/**
 * Types a string out one character at a time.
 *
 * Spec §6: "Extracted fields type themselves into a result card as they arrive."
 * The *arrival* is real — the value only exists once the backend returned it —
 * and this renders that arrival rather than inventing progress.
 */
function useTypedText(value: string, enabled: boolean, speedMs = 18): string {
  const [shown, setShown] = React.useState(enabled ? "" : value);

  React.useEffect(() => {
    if (!enabled) {
      setShown(value);
      return;
    }
    setShown("");
    if (!value) return;

    let index = 0;
    const timer = window.setInterval(() => {
      index += 1;
      setShown(value.slice(0, index));
      if (index >= value.length) window.clearInterval(timer);
    }, speedMs);
    return () => window.clearInterval(timer);
  }, [value, enabled, speedMs]);

  return shown;
}

function TypedField({
  label,
  value,
  delay,
  mono = false,
  animate,
}: {
  label: string;
  value: string;
  delay: number;
  mono?: boolean;
  animate: boolean;
}) {
  const [visible, setVisible] = React.useState(!animate);

  React.useEffect(() => {
    if (!animate) {
      setVisible(true);
      return;
    }
    const timer = window.setTimeout(() => setVisible(true), delay);
    return () => window.clearTimeout(timer);
  }, [animate, delay]);

  const typed = useTypedText(value, animate && visible);
  const isTyping = animate && visible && typed.length < value.length;

  return (
    <motion.div
      initial={animate ? { opacity: 0, y: 4 } : false}
      animate={{ opacity: visible ? 1 : 0, y: visible ? 0 : 4 }}
      transition={{ duration: 0.25 }}
      className="flex items-baseline justify-between gap-4 border-b border-hairline/60 py-2 last:border-b-0"
    >
      <span className="shrink-0 text-xs text-ink-faint">{label}</span>
      <span
        className={cn(
          "min-w-0 truncate text-right text-sm text-ink",
          mono && "font-mono tabular text-[13px]",
        )}
      >
        {visible ? typed : ""}
        {isTyping ? <span className="ml-px inline-block w-1.5 animate-pulse text-accent">▌</span> : null}
      </span>
    </motion.div>
  );
}

export function ResultCard({
  extraction,
  filename,
  animate = true,
  className,
}: {
  extraction: Extraction;
  filename?: string;
  animate?: boolean;
  className?: string;
}) {
  const failures = extraction.checks.filter((check) => !check.passed);

  const fields: Array<{ label: string; value: string; mono?: boolean }> = [
    { label: "Vendor", value: extraction.vendor ?? "—" },
    { label: "Invoice no.", value: extraction.invoice_number ?? "—", mono: true },
    { label: "Issue date", value: formatDate(extraction.issue_date), mono: true },
    { label: "Due date", value: formatDate(extraction.due_date), mono: true },
    { label: "Subtotal", value: formatMoney(extraction.subtotal), mono: true },
    { label: "Tax", value: formatMoney(extraction.tax), mono: true },
    {
      label: "Total",
      value: formatMoney(extraction.total, extraction.currency),
      mono: true,
    },
    { label: "Terms", value: extraction.payment_terms ?? "—" },
  ];

  return (
    <Panel edgeLight className={cn("overflow-hidden", className)}>
      <PanelHeader
        eyebrow="Structured extraction"
        title={filename ?? "Extracted fields"}
        action={
          extraction.is_valid ? (
            <Badge tone="pass">
              <CheckCircle2 className="size-3" />
              Validated
            </Badge>
          ) : (
            <Badge tone="warn">
              <AlertTriangle className="size-3" />
              Needs review
            </Badge>
          )
        }
      />

      <div className="px-5 pb-2 pt-3">
        {fields.map((field, index) => (
          <TypedField
            key={field.label}
            label={field.label}
            value={field.value}
            mono={field.mono ?? false}
            delay={index * 110}
            animate={animate}
          />
        ))}
      </div>

      {extraction.line_items.length > 0 ? (
        <div className="border-t border-hairline px-5 py-3">
          <p className="eyebrow mb-2">Line items · {extraction.line_items.length}</p>
          <div className="space-y-1">
            {extraction.line_items.slice(0, 6).map((item, index) => (
              <div
                key={`${item.description ?? "line"}-${index}`}
                className="flex items-baseline justify-between gap-3 text-[13px]"
              >
                <span className="min-w-0 truncate text-ink-muted">
                  {item.description ?? "—"}
                </span>
                <span className="shrink-0 font-mono tabular text-ink">
                  {item.qty ?? "—"} × {item.unit_price ?? "—"}
                  <span className="ml-2 text-ink-muted">{item.amount ?? "—"}</span>
                </span>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2 border-t border-hairline px-5 py-3 text-[11px] text-ink-faint">
        <Badge tone="neutral">
          <FileText className="size-3" />
          {extraction.lane === "vision" ? "Vision lane" : "Text lane"}
        </Badge>
        {extraction.model ? <Badge tone="neutral">{extraction.model}</Badge> : null}
        {extraction.repair_attempts > 0 ? (
          <Badge tone="warn">
            <Wrench className="size-3" />
            {extraction.repair_attempts} self-correction
            {extraction.repair_attempts === 1 ? "" : "s"}
          </Badge>
        ) : null}
        <span className="ml-auto">
          {extraction.checks.length} deterministic checks · {failures.length} failed
        </span>
      </div>

      <AnimatePresence>
        {failures.length > 0 ? (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden border-t border-warn/25 bg-warn/[0.06]"
          >
            <div className="space-y-1.5 px-5 py-3">
              {failures.map((check) => (
                <div key={check.rule} className="flex gap-2 text-[12px] leading-relaxed">
                  <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warn" />
                  <span className="text-ink-muted">
                    <span className="font-mono text-[11px] text-warn">{check.rule}</span>
                    {" — "}
                    {check.message}
                  </span>
                </div>
              ))}
            </div>
          </motion.div>
        ) : null}
      </AnimatePresence>
    </Panel>
  );
}
