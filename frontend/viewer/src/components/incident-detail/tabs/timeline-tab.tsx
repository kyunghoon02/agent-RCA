"use client";

import * as React from "react";
import { ChevronRight, Clock } from "lucide-react";
import { EmptyState } from "@/components/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { formatTimestamp } from "@/lib/format";
import { STAGE_LABELS } from "@/lib/lifecycle";
import { TIMELINE_STAGES, type TimelineEvent, type TimelineStage } from "@/lib/types";
import { cn } from "@/lib/utils";

/** One colour per stage, used identically in the filter chips and the rail. */
const STAGE_STYLE: Record<TimelineStage, { dot: string; text: string; border: string }> = {
  DETECTION: {
    dot: "bg-stage-detection",
    text: "text-stage-detection",
    border: "border-stage-detection/40",
  },
  COLLECTION: {
    dot: "bg-stage-collection",
    text: "text-stage-collection",
    border: "border-stage-collection/40",
  },
  LOCALIZATION: {
    dot: "bg-stage-localization",
    text: "text-stage-localization",
    border: "border-stage-localization/40",
  },
  ANALYSIS: {
    dot: "bg-stage-analysis",
    text: "text-stage-analysis",
    border: "border-stage-analysis/40",
  },
  REPORT: {
    dot: "bg-stage-report",
    text: "text-stage-report",
    border: "border-stage-report/40",
  },
};

export function TimelineTab({
  timeline,
  onFocusEvidence,
}: {
  timeline: TimelineEvent[];
  onFocusEvidence: (evidenceId: string) => void;
}) {
  const [stages, setStages] = React.useState<TimelineStage[]>([]);
  const [allExpanded, setAllExpanded] = React.useState(false);

  const visible = React.useMemo(
    () => (stages.length === 0 ? timeline : timeline.filter((e) => stages.includes(e.stage))),
    [timeline, stages],
  );

  if (timeline.length === 0) {
    return (
      <EmptyState
        icon={Clock}
        title="No timeline events"
        description="No lifecycle audit rows, Evidence observations, Context freezes or Agent runs are stored for this Incident."
      />
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <Card className="flex flex-wrap items-center gap-1.5 p-2.5">
        <span className="mr-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          Stage
        </span>
        {TIMELINE_STAGES.map((stage) => {
          const active = stages.includes(stage);
          const count = timeline.filter((event) => event.stage === stage).length;
          return (
            <button
              key={stage}
              type="button"
              aria-pressed={active}
              disabled={count === 0}
              onClick={() =>
                setStages((current) =>
                  current.includes(stage)
                    ? current.filter((item) => item !== stage)
                    : [...current, stage],
                )
              }
              className={cn(
                "inline-flex items-center gap-1.5 rounded border px-2 py-1 text-[11px] transition-colors",
                "disabled:opacity-40",
                active ? cn(STAGE_STYLE[stage].border, "bg-accent") : "border-border",
              )}
            >
              <span
                aria-hidden="true"
                className={cn("size-2 rounded-full", STAGE_STYLE[stage].dot)}
              />
              {STAGE_LABELS[stage]}
              <span className="tabular text-muted-foreground">{count}</span>
            </button>
          );
        })}
        <Button
          variant="ghost"
          size="xs"
          className="ml-auto"
          onClick={() => setAllExpanded((current) => !current)}
        >
          {allExpanded ? "Collapse all details" : "Expand all details"}
        </Button>
      </Card>

      <Card className="px-3 py-2">
        <ol className="flex flex-col">
          {visible.map((event, index) => (
            <TimelineRow
              key={`${event.occurred_at}-${event.event_type}-${index}`}
              event={event}
              forceOpen={allExpanded}
              isLast={index === visible.length - 1}
              onFocusEvidence={onFocusEvidence}
            />
          ))}
        </ol>
      </Card>
    </div>
  );
}

function TimelineRow({
  event,
  forceOpen,
  isLast,
  onFocusEvidence,
}: {
  event: TimelineEvent;
  forceOpen: boolean;
  isLast: boolean;
  onFocusEvidence: (evidenceId: string) => void;
}) {
  const [open, setOpen] = React.useState(false);
  const expanded = forceOpen || open;
  const contentId = React.useId();
  const style = STAGE_STYLE[event.stage];
  const detailKeys = Object.keys(event.details ?? {});
  const hasDetail = detailKeys.length > 0 || event.evidence_ids.length > 0;

  return (
    <li className="flex gap-3">
      <div className="flex flex-col items-center pt-2">
        <span aria-hidden="true" className={cn("size-2.5 shrink-0 rounded-full", style.dot)} />
        {!isLast && <span aria-hidden="true" className="w-px flex-1 bg-border" />}
      </div>

      <div className="min-w-0 flex-1 pb-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="tabular shrink-0 font-mono text-[11px] text-muted-foreground">
            {formatTimestamp(event.occurred_at)}
          </span>
          <Badge tone="outline" className={style.text}>
            {STAGE_LABELS[event.stage]}
          </Badge>
          <span className="font-mono text-xs font-medium">{event.event_type}</span>
          {event.evidence_ids.length > 0 && (
            <span className="tabular text-[11px] text-muted-foreground">
              {event.evidence_ids.length} Evidence
            </span>
          )}
          {hasDetail && (
            <Button
              variant="ghost"
              size="xs"
              className="ml-auto"
              aria-expanded={expanded}
              aria-controls={contentId}
              onClick={() => setOpen((current) => !current)}
            >
              <ChevronRight
                aria-hidden="true"
                className={cn("size-3 transition-transform", expanded && "rotate-90")}
              />
              {expanded ? "Hide" : "Details"}
            </Button>
          )}
        </div>

        {expanded && hasDetail && (
          <div id={contentId} className="mt-1.5 rounded border border-border bg-surface-sunken p-2">
            {detailKeys.length > 0 && (
              <dl className="grid gap-x-4 gap-y-1 text-[11px] sm:grid-cols-2">
                {detailKeys.sort().map((key) => (
                  <div key={key} className="flex items-baseline gap-2">
                    <dt className="shrink-0 font-mono text-muted-foreground">{key}</dt>
                    <dd className="min-w-0 break-all font-mono">
                      {formatDetailValue(event.details[key])}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
            {event.evidence_ids.length > 0 && (
              <div className="mt-1.5 flex flex-wrap items-center gap-1 border-t border-border pt-1.5">
                <span className="text-[11px] text-muted-foreground">Evidence:</span>
                {event.evidence_ids.map((id) => (
                  <button
                    key={id}
                    type="button"
                    onClick={() => onFocusEvidence(id)}
                    className="rounded border border-border px-1.5 py-0.5 font-mono text-[11px] hover:bg-accent"
                  >
                    {id}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </li>
  );
}

function formatDetailValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
