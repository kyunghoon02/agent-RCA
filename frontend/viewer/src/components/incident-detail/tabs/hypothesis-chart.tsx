"use client";

import {
  Bar,
  BarChart,
  Cell,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { RcaHypothesis } from "@/lib/types";

const STATUS_FILL: Record<RcaHypothesis["status"], string> = {
  supported: "var(--status-success)",
  competing: "var(--status-warning)",
  unresolved: "var(--status-info)",
  rejected: "var(--status-neutral)",
};

function percentageLabel(value: unknown): string {
  const numeric = typeof value === "number" ? value : Number(value);
  return Number.isFinite(numeric) ? `${numeric}%` : "—";
}

/**
 * Ranked hypothesis confidence.
 *
 * The chart repeats what the list below already states in text and in badges;
 * it is a reading aid, never the only place a value appears.
 */
export function HypothesisChart({ hypotheses }: { hypotheses: RcaHypothesis[] }) {
  const data = hypotheses.map((hypothesis) => ({
    name: `#${hypothesis.rank}`,
    confidence: Number((hypothesis.confidence * 100).toFixed(1)),
    status: hypothesis.status,
  }));

  return (
    <div className="h-[--chart-height]" style={{ ["--chart-height" as string]: `${data.length * 34 + 24}px` }}>
      {/* Minimums keep the chart measurable in narrow containers and in jsdom. */}
      <ResponsiveContainer width="100%" height="100%" minWidth={240} minHeight={48}>
        <BarChart data={data} layout="vertical" margin={{ top: 4, right: 44, bottom: 4, left: 4 }}>
          <XAxis type="number" domain={[0, 100]} hide />
          <YAxis
            type="category"
            dataKey="name"
            width={32}
            tickLine={false}
            axisLine={false}
            tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
          />
          <Tooltip
            cursor={{ fill: "var(--accent)" }}
            contentStyle={{
              background: "var(--popover)",
              border: "1px solid var(--border)",
              borderRadius: 6,
              fontSize: 11,
              color: "var(--popover-foreground)",
            }}
            formatter={(value, _name, entry) => [
              `${percentageLabel(value)} · ${(entry?.payload as { status?: string })?.status ?? ""}`,
              "confidence",
            ]}
          />
          <Bar dataKey="confidence" radius={2} barSize={16} isAnimationActive={false}>
            {data.map((entry) => (
              <Cell key={entry.name} fill={STATUS_FILL[entry.status]} />
            ))}
            <LabelList
              dataKey="confidence"
              position="right"
              formatter={percentageLabel}
              style={{ fontSize: 11, fill: "var(--muted-foreground)" }}
            />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
