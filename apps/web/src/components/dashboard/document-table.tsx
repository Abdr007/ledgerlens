"use client";

import { motion } from "framer-motion";
import { FileStack, ScrollText } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Badge,
  EmptyState,
  Panel,
  PanelHeader,
  Skeleton,
  StatusBadge,
} from "@/components/ui/primitives";
import { formatLatency, formatMoney, relativeTime } from "@/lib/utils";
import type { DocumentSummary } from "@/types/api";

export function DocumentTable({
  documents,
  loading,
  onInspect,
}: {
  documents: DocumentSummary[];
  loading: boolean;
  onInspect: (documentId: string) => void;
}) {
  return (
    <Panel edgeLight className="overflow-hidden">
      <PanelHeader
        eyebrow="Ledger"
        title="Recent documents"
        action={<Badge tone="neutral">{documents.length} shown</Badge>}
      />

      {loading ? (
        <div className="space-y-2 px-5 py-4">
          {[0, 1, 2, 3, 4].map((index) => (
            <Skeleton key={index} className="h-10 w-full" />
          ))}
        </div>
      ) : documents.length === 0 ? (
        <EmptyState
          icon={<FileStack className="size-6" />}
          title="The ledger is empty"
          hint="Drop a document above, or run `make seed` to load vendor history."
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] border-collapse text-left">
            <thead>
              <tr className="border-b border-hairline">
                {["Document", "Vendor", "Total", "Status", "Latency", "Received", ""].map(
                  (heading) => (
                    <th
                      key={heading}
                      className="px-5 py-2.5 text-[11px] font-medium uppercase tracking-wider text-ink-faint"
                    >
                      {heading}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {documents.map((document, index) => (
                <motion.tr
                  key={document.id}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  transition={{ delay: Math.min(index * 0.03, 0.3) }}
                  className="border-b border-hairline/60 transition-colors last:border-b-0 hover:bg-white/[0.03]"
                >
                  <td className="max-w-[220px] px-5 py-3">
                    <p className="truncate text-[13px] font-medium text-ink">
                      {document.filename}
                    </p>
                    <p className="mt-0.5 text-[11px] text-ink-faint">
                      {document.doc_kind ?? "unknown"} · {document.lane ?? "—"} lane
                    </p>
                  </td>
                  <td className="max-w-[180px] px-5 py-3">
                    <span className="block truncate text-[13px] text-ink-muted">
                      {document.vendor ?? "—"}
                    </span>
                  </td>
                  <td className="whitespace-nowrap px-5 py-3 font-mono tabular text-[13px] text-ink">
                    {formatMoney(document.total, document.currency)}
                  </td>
                  <td className="px-5 py-3">
                    <div className="flex items-center gap-2">
                      <StatusBadge status={document.status} />
                      {document.anomaly_count > 0 ? (
                        <Badge tone={document.highest_severity === "HIGH" ? "flag" : "warn"}>
                          {document.anomaly_count}
                        </Badge>
                      ) : null}
                    </div>
                  </td>
                  <td className="whitespace-nowrap px-5 py-3 font-mono tabular text-[12px] text-ink-muted">
                    {formatLatency(document.latency_ms)}
                  </td>
                  <td className="whitespace-nowrap px-5 py-3 text-[12px] text-ink-faint">
                    {relativeTime(document.created_at)}
                  </td>
                  <td className="px-5 py-3 text-right">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => onInspect(document.id)}
                      aria-label={`Open audit trail for ${document.filename}`}
                    >
                      <ScrollText className="size-3.5" />
                      Trail
                    </Button>
                  </td>
                </motion.tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
