"use client";

import { AnimatePresence, motion } from "framer-motion";
import {
  AlertTriangle,
  Check,
  Database,
  FileUp,
  Radar,
  ScanText,
  ShieldCheck,
  Split,
  X,
} from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/utils";
import { PIPELINE_STAGES, type PipelineStage, type StageProgress, type StageState } from "@/types/api";

const STAGE_ICON: Record<PipelineStage, React.ElementType> = {
  ingest: FileUp,
  route: Split,
  extract: ScanText,
  validate: ShieldCheck,
  screen: Radar,
  ledger: Database,
};

const NODE_STYLE: Record<StageState, string> = {
  pending: "border-hairline bg-white/[0.02]",
  active: "border-accent/60 bg-accent/10 glow-accent",
  passed: "border-pass/45 bg-pass/10 glow-pass",
  flagged: "border-warn/50 bg-warn/10 glow-warn",
  failed: "border-flag/50 bg-flag/10 glow-flag",
};

const NODE_GLYPH: Record<StageState, string> = {
  pending: "text-ink-faint",
  active: "text-accent",
  passed: "text-pass",
  flagged: "text-warn",
  failed: "text-flag",
};

const LABEL_STYLE: Record<StageState, string> = {
  pending: "text-ink-faint",
  active: "text-accent",
  passed: "text-ink",
  flagged: "text-warn",
  failed: "text-flag",
};

function StageIcon({ stage, state }: { stage: PipelineStage; state: StageState }) {
  if (state === "passed") return <Check className="size-[18px]" strokeWidth={2.6} />;
  if (state === "flagged") return <AlertTriangle className="size-[18px]" strokeWidth={2.2} />;
  if (state === "failed") return <X className="size-[18px]" strokeWidth={2.6} />;
  const Icon = STAGE_ICON[stage];
  return <Icon className="size-[18px]" strokeWidth={1.9} />;
}

/**
 * A stage node: a diamond, not a rounded square. The square is rotated 45° and
 * the glyph counter-rotated so it stays upright — CSS `transform` beats a
 * clip-path here because the border, the glow ring and the pulse all rotate
 * with it for free.
 */
function StageNode({
  stage,
  state,
  index,
}: {
  stage: PipelineStage;
  state: StageState;
  index: number;
}) {
  return (
    <motion.div
      initial={{ scale: 0.8, opacity: 0 }}
      animate={{ scale: 1, opacity: 1 }}
      transition={{ delay: index * 0.05, duration: 0.3, ease: "easeOut" }}
      className="relative flex size-[52px] shrink-0 items-center justify-center"
    >
      <div
        className={cn(
          "absolute inset-[7px] rotate-45 border transition-all duration-300",
          NODE_STYLE[state],
        )}
      />
      {state === "active" ? (
        <motion.span
          className="absolute inset-[7px] rotate-45 border border-accent/50"
          animate={{ opacity: [0.75, 0, 0.75], scale: [1, 1.3, 1] }}
          transition={{ duration: 1.8, repeat: Infinity, ease: "easeInOut" }}
        />
      ) : null}
      <span className={cn("relative transition-colors duration-300", NODE_GLYPH[state])}>
        <StageIcon stage={stage} state={state} />
      </span>
    </motion.div>
  );
}

function Connector({ activeAhead, complete }: { activeAhead: boolean; complete: boolean }) {
  return (
    <div className="relative mx-0.5 hidden h-[3px] flex-1 self-start md:block" style={{ marginTop: 25 }}>
      <div className="absolute inset-x-0 top-[1px] h-px bg-hairline" />
      <motion.div
        className="absolute left-0 top-[1px] h-px bg-pass/70"
        initial={{ width: 0 }}
        animate={{ width: complete ? "100%" : 0 }}
        transition={{ duration: 0.45, ease: "easeOut" }}
      />
      {activeAhead ? <div className="connector-active absolute inset-0" /> : null}
    </div>
  );
}

const TERMINAL_STATES: ReadonlySet<StageState> = new Set(["passed", "flagged", "failed"]);

/**
 * Paces how fast finished stages are *revealed*, without inventing any of them.
 *
 * The states themselves always come from the backend. But a digital PDF can
 * clear all six stages in under 50 ms, and rendering that in one frame means a
 * viewer sees the end state and never the pipeline. This holds the reveal to one
 * stage per `stepMs` until it catches up with reality, so what actually happened
 * is legible.
 *
 * It only ever lags the truth — it can never run ahead of it, and it never shows
 * a stage as passed that the backend has not reported as passed.
 */
function usePacedReveal(stages: StageProgress[] | null, stepMs: number): StageProgress[] | null {
  const settled = React.useMemo(
    () => (stages ?? []).filter((entry) => TERMINAL_STATES.has(entry.state)).length,
    [stages],
  );
  const [revealed, setRevealed] = React.useState(0);

  // A new document restarts the reveal.
  React.useEffect(() => {
    if (stages === null) setRevealed(0);
  }, [stages]);

  React.useEffect(() => {
    if (revealed >= settled) return;
    const timer = window.setTimeout(() => setRevealed((count) => count + 1), stepMs);
    return () => window.clearTimeout(timer);
  }, [revealed, settled, stepMs]);

  return React.useMemo(() => {
    if (!stages) return null;
    let seen = 0;
    return stages.map((entry) => {
      if (!TERMINAL_STATES.has(entry.state)) return entry;
      seen += 1;
      if (seen <= revealed) return entry;
      // Not yet revealed: show the first pending one as the live node.
      return { ...entry, state: seen === revealed + 1 ? "active" : "pending", detail: null };
    });
  }, [stages, revealed]);
}

export interface PipelineRailProps {
  stages: StageProgress[] | null;
  /** Shown under the rail while a document is in flight. */
  caption?: string | null;
  /** Minimum time each finished stage stays visible before the next reveals. */
  stepMs?: number;
  className?: string;
}

/**
 * The six-stage pipeline visual (spec §6).
 *
 * Every node's state comes from `GET /v1/documents/{id}/status`, which projects
 * it from the append-only audit log. Nothing here is on a timer: if a stage is
 * green it is because the backend recorded that it finished.
 */
export function PipelineRail({
  stages,
  caption,
  stepMs = 340,
  className,
}: PipelineRailProps) {
  const paced = usePacedReveal(stages, stepMs);
  const byId = React.useMemo(() => {
    const map = new Map<PipelineStage, StageProgress>();
    for (const entry of paced ?? []) map.set(entry.stage, entry);
    return map;
  }, [paced]);

  return (
    <div className={cn("w-full", className)}>
      <div className="flex flex-col gap-5 md:flex-row md:items-start md:gap-0">
        {PIPELINE_STAGES.map((definition, index) => {
          const progress = byId.get(definition.id);
          const state: StageState = progress?.state ?? "pending";
          const next = PIPELINE_STAGES[index + 1];
          const nextState = next ? (byId.get(next.id)?.state ?? "pending") : undefined;
          const done = state === "passed" || state === "flagged";

          return (
            <React.Fragment key={definition.id}>
              <div className="flex min-w-0 flex-1 flex-row items-start gap-3 md:flex-col md:items-center md:gap-0 md:text-center">
                <StageNode stage={definition.id} state={state} index={index} />

                <div className="min-w-0 md:mt-2.5">
                  <p
                    className={cn(
                      "font-mono text-[11px] font-semibold uppercase tracking-[0.14em] transition-colors duration-300",
                      LABEL_STYLE[state],
                    )}
                  >
                    <span className="mr-1.5 text-ink-faint">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    {definition.label}
                  </p>
                  <p className="mt-0.5 text-[11px] leading-snug text-ink-faint">
                    {definition.blurb}
                  </p>
                  <div className="h-4">
                    <AnimatePresence mode="wait">
                      {progress?.detail ? (
                        <motion.p
                          key={progress.detail}
                          initial={{ opacity: 0, y: -3 }}
                          animate={{ opacity: 1, y: 0 }}
                          exit={{ opacity: 0 }}
                          transition={{ duration: 0.22 }}
                          className={cn(
                            "mt-1 truncate text-[11px] font-medium",
                            state === "flagged"
                              ? "text-warn"
                              : state === "failed"
                                ? "text-flag"
                                : "text-ink-muted",
                          )}
                        >
                          {progress.detail}
                        </motion.p>
                      ) : null}
                    </AnimatePresence>
                  </div>
                </div>
              </div>

              {next ? <Connector activeAhead={nextState === "active"} complete={done} /> : null}
            </React.Fragment>
          );
        })}
      </div>

      <AnimatePresence>
        {caption ? (
          <motion.p
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="mt-5 text-center text-xs text-ink-faint"
          >
            {caption}
          </motion.p>
        ) : null}
      </AnimatePresence>
    </div>
  );
}
