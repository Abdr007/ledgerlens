"use client";

import { BarChart3 } from "lucide-react";
import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { EmptyState, Panel, PanelHeader, Skeleton } from "@/components/ui/primitives";
import { formatMoney } from "@/lib/utils";
import type { VendorSpend } from "@/types/api";

/**
 * Vendor spend (spec §6).
 *
 * One hue, varied by opacity down the ranking. A categorical palette here would
 * imply the vendors are different *kinds* of thing rather than the same measure
 * at different magnitudes, and would break the single-accent rule.
 */
const BAR_FILL = "#c8ff2f";

/**
 * Recharts clones the element passed to `content` and injects the tooltip props,
 * so the component is typed against just the fields it reads. Declaring the
 * library's generic signature here fights variance for no benefit.
 */
function ChartTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: ReadonlyArray<{ payload?: VendorSpend }>;
}) {
  if (!active || !payload?.length) return null;
  const datum = payload[0]?.payload;
  if (!datum) return null;
  return (
    <div className="panel bevel-sm px-3 py-2 text-xs shadow-xl">
      <p className="font-medium text-ink">{datum.vendor}</p>
      <p className="mt-1 font-mono tabular text-accent">
        {formatMoney(datum.total, datum.currency)}
      </p>
      <p className="mt-0.5 text-ink-faint">
        {datum.invoice_count} invoice{datum.invoice_count === 1 ? "" : "s"}
      </p>
    </div>
  );
}

function shorten(vendor: string): string {
  return vendor.length > 22 ? `${vendor.slice(0, 21)}…` : vendor;
}

export function VendorChart({
  data,
  loading,
}: {
  data: VendorSpend[];
  loading: boolean;
}) {
  return (
    <Panel edgeLight className="flex h-full flex-col">
      <PanelHeader eyebrow="Ledger" title="Vendor spend" />
      <div className="flex-1 px-2 pb-4 pt-4">
        {loading ? (
          <div className="space-y-3 px-3">
            {[0, 1, 2, 3, 4].map((index) => (
              <Skeleton key={index} className="h-7 w-full" />
            ))}
          </div>
        ) : data.length === 0 ? (
          <EmptyState
            icon={<BarChart3 className="size-6" />}
            title="No committed invoices yet"
            hint="Spend appears once documents finish the pipeline."
          />
        ) : (
          <ResponsiveContainer width="100%" height={Math.max(200, data.length * 42)}>
            <BarChart data={data} layout="vertical" margin={{ left: 8, right: 24, top: 4 }}>
              <XAxis type="number" hide />
              <YAxis
                type="category"
                dataKey="vendor"
                width={150}
                tickLine={false}
                axisLine={false}
                tickFormatter={shorten}
                tick={{ fill: "#9a94b4", fontSize: 10 }}
              />
              <Tooltip content={<ChartTooltip />} cursor={{ fill: "rgba(200,255,47,0.06)" }} />
              <Bar dataKey="total" radius={[0, 0, 0, 0]} maxBarSize={14}>
                {data.map((entry, index) => (
                  <Cell
                    key={entry.vendor}
                    fill={BAR_FILL}
                    fillOpacity={Math.max(0.28, 1 - index * 0.11)}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>
    </Panel>
  );
}
