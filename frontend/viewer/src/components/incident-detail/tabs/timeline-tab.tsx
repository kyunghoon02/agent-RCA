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
import {
  collectedWindow,
  countGroupedEvents,
  entryTimestamp,
  groupTimeline,
  type TimelineEntry,
} from "@/lib/timeline-grouping";
import type { EvidenceItem } from "@/lib/types";
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
  evidence = [],
  onFocusEvidence,
}: {
  timeline: TimelineEvent[];
  /** Used only to report when a batch was actually collected. */
  evidence?: EvidenceItem[];
  onFocusEvidence: (evidenceId: string) => void;
}) {
  const [stages, setStages] = React.useState<TimelineStage[]>([]);
  const [allExpanded, setAllExpanded] = React.useState(false);
  const [grouped, setGrouped] = React.useState(true);

  const evidenceById = React.useMemo(
    () => new Map(evidence.map((item) => [item.evidence_id, item])),
    [evidence],
  );

  const visible = React.useMemo(
    () => (stages.length === 0 ? timeline : timeline.filter((e) => stages.includes(e.stage))),
    [timeline, stages],
  );

  // Grouping folds per-Evidence rows into one row per Provider so the lifecycle
  // milestones stay visible; every member event remains reachable.
  const entries = React.useMemo<TimelineEntry[]>(
    () =>
      grouped
        ? groupTimeline(visible)
        : visible.map((event, index) => ({
            kind: "event" as const,
            id: `${event.occurred_at}-${index}`,
            occurredAt: event.occurred_at,
            event,
          })),
    [visible, grouped],
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
        <div className="ml-auto flex items-center gap-1.5">
          <Button
            variant={grouped ? "secondary" : "outline"}
            size="xs"
            aria-pressed={grouped}
            onClick={() => setGrouped((current) => !current)}
          >
            {grouped ? "Grouped" : "All events"}
          </Button>
          <Button variant="ghost" size="xs" onClick={() => setAllExpanded((c) => !c)}>
            {allExpanded ? "Collapse all" : "Expand all"}
          </Button>
        </div>
        <p className="w-full text-[11px] text-muted-foreground">
          {entries.length} row{entries.length === 1 ? "" : "s"} covering{" "}
          {countGroupedEvents(entries)} event
          {countGroupedEvents(entries) === 1 ? "" : "s"}. Timestamps are when each
          signal was <span className="font-medium">observed</span>, which can precede
          the Incident; Provider batches also show when collection stored them.
        </p>
      </Card>

      <Card className="px-3 py-2">
        <ol className="flex flex-col">
          {entries.map((entry, index) =>
            entry.kind === "group" ? (
              <TimelineGroupRow
                key={entry.id}
                entry={entry}
                evidenceById={evidenceById}
                forceOpen={allExpanded}
                isLast={index === entries.length - 1}
                onFocusEvidence={onFocusEvidence}
              />
            ) : (
              <TimelineRow
                key={entry.id}
                event={entry.event}
                forceOpen={allExpanded}
                isLast={index === entries.length - 1}
                onFocusEvidence={onFocusEvidence}
              />
            ),
          )}
        </ol>
      </Card>
    </div>
  );
}

/** A folded batch: one row per Provider, expandable to its member events. */
function TimelineGroupRow({
  entry,
  evidenceById,
  forceOpen,
  isLast,
  onFocusEvidence,
}: {
  entry: Extract<TimelineEntry, { kind: "group" }>;
  evidenceById: ReadonlyMap<string, EvidenceItem>;
  forceOpen: boolean;
  isLast: boolean;
  onFocusEvidence: (evidenceId: string) => void;
}) {
  const [open, setOpen] = React.useState(false);
  const expanded = forceOpen || open;
  const contentId = React.useId();
  const style = STAGE_STYLE[entry.stage];
  const spans = entry.startedAt !== entry.endedAt;
  const collected = collectedWindow(entry.evidenceIds, evidenceById);

  return (
    <li className="flex gap-3">
      <div className="flex flex-col items-center pt-2">
        <span
          aria-hidden="true"
          className={cn("size-2.5 shrink-0 rounded-full ring-2 ring-card", style.dot)}
        />
        {!isLast && <span aria-hidden="true" className="w-px flex-1 bg-border" />}
      </div>

      <div className="min-w-0 flex-1 pb-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="tabular shrink-0 font-mono text-[11px] text-muted-foreground">
            <span className="text-muted-foreground/70">observed </span>
            {formatTimestamp(entry.startedAt)}
            {spans && ` → ${formatTimestamp(entry.endedAt).slice(11)}`}
          </span>
          <Badge tone="outline" className={style.text}>
            {STAGE_LABELS[entry.stage]}
          </Badge>
          <span className="text-xs font-medium">{entry.label}</span>
          {!entry.passKnown && (
            <Badge
              tone="outline"
              title="This payload records no collection-pass identity, so separate retries cannot be distinguished."
            >
              pass unknown
            </Badge>
          )}
          {collected && (
            <span
              className="tabular font-mono text-[10px] text-muted-foreground"
              title="When the Provider stored this Evidence (provenance.collected_at)"
            >
              collected {formatTimestamp(collected.start)}
              {collected.start !== collected.end &&
                ` → ${formatTimestamp(collected.end).slice(11)}`}
            </span>
          )}
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
            {expanded ? "Hide" : `${entry.events.length} events`}
          </Button>
        </div>

        {expanded && (
          <ul
            id={contentId}
            className="mt-1.5 flex flex-col gap-0.5 rounded border border-border bg-surface-sunken p-2"
          >
            {entry.events.map((event, index) => (
              <li
                key={`${event.occurred_at}-${index}`}
                className="flex flex-wrap items-center gap-2 text-[11px]"
              >
                <span className="tabular font-mono text-muted-foreground">
                  {formatTimestamp(event.occurred_at)}
                </span>
                <span className="font-mono">{event.event_type}</span>
                {event.evidence_ids.map((id) => (
                  <button
                    key={id}
                    type="button"
                    onClick={() => onFocusEvidence(id)}
                    className="rounded border border-border px-1.5 py-0.5 font-mono hover:bg-accent"
                  >
                    {id}
                  </button>
                ))}
              </li>
            ))}
          </ul>
        )}
      </div>
    </li>
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
