"use client";

import * as React from "react";
import { ChevronRight, Hourglass, PauseCircle } from "lucide-react";
import { Notice } from "@/components/notices";
import { WorkStateBadge } from "@/components/status";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatTimestamp } from "@/lib/format";
import type { Incident, IncidentWorkState, WorkQueueState, WorkStage } from "@/lib/types";
import {
  isWaitingForAgentRuntime,
  WORK_STAGES,
  WORK_STAGE_LABELS,
  workForStage,
} from "@/lib/work";
import { cn } from "@/lib/utils";

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <dt className="shrink-0 text-[11px] text-muted-foreground">{label}</dt>
      <dd className="truncate font-mono text-[11px]">{value}</dd>
    </div>
  );
}

/**
 * Worker identity, lease fencing and claim timestamps are forensic detail.
 * They stay collapsed so the card answers "what state is this stage in"
 * without a wall of operational metadata.
 */
function ExecutionDetails({ item }: { item: WorkQueueState }) {
  const [open, setOpen] = React.useState(false);
  const id = React.useId();
  return (
    <div className="mt-2 border-t border-border pt-1.5">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((current) => !current)}
        className="inline-flex items-center gap-1 rounded text-[11px] text-muted-foreground hover:text-foreground"
      >
        <ChevronRight
          aria-hidden="true"
          className={cn("size-3 transition-transform", open && "rotate-90")}
        />
        Execution details
      </button>
      {open && (
        <dl id={id} className="mt-1.5 flex flex-col gap-1">
          <Field label="Worker" value={item.worker_id ?? "—"} />
          <Field
            label="Lease expires"
            value={item.lease_expires_at ? formatTimestamp(item.lease_expires_at) : "—"}
          />
          <Field label="Available" value={formatTimestamp(item.available_at)} />
          <Field
            label="Claimed"
            value={item.claimed_at ? formatTimestamp(item.claimed_at) : "—"}
          />
          <Field
            label="Completed"
            value={item.completed_at ? formatTimestamp(item.completed_at) : "—"}
          />
        </dl>
      )}
    </div>
  );
}

function WorkCard({
  stage,
  item,
  waitingForAgent,
}: {
  stage: WorkStage;
  item: WorkQueueState | null;
  waitingForAgent: boolean;
}) {
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-2 pb-2">
        <CardTitle>{WORK_STAGE_LABELS[stage]}</CardTitle>
        {item ? (
          <WorkStateBadge state={item.state} />
        ) : (
          <span className="text-[11px] text-muted-foreground">not enqueued</span>
        )}
      </CardHeader>
      <CardContent className="pt-0">
        {!item ? (
          <p className="text-xs text-muted-foreground">
            This stage has not been enqueued for this Incident.
          </p>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge tone="outline">
                {item.attempt_count} attempt{item.attempt_count === 1 ? "" : "s"}
              </Badge>
              {item.context_id && (
                <Badge tone="info" title="Immutable Context pinned to this work item">
                  {item.context_id}
                </Badge>
              )}
              {item.last_error_code && (
                <Badge tone="critical">{item.last_error_code}</Badge>
              )}
            </div>

            {waitingForAgent && (
              // Deliberately neutral: an unclaimed queue is a waiting state, not
              // an error, and must not read as a red failure.
              <Notice
                tone="info"
                icon={PauseCircle}
                title="Waiting for Agent runtime"
                className="mt-2"
              >
                The Frozen Context is ready and pinned, but no Agent runtime has claimed
                this analysis work.
              </Notice>
            )}

            {item.state === "READY" && !waitingForAgent && (
              <p className="mt-2 flex items-center gap-1.5 text-[11px] text-muted-foreground">
                <Hourglass className="size-3" aria-hidden="true" />
                Queued since {formatTimestamp(item.available_at)}
              </p>
            )}

            <ExecutionDetails item={item} />
          </>
        )}
      </CardContent>
    </Card>
  );
}

export function WorkQueueCards({
  incident,
  work,
  isLoading,
}: {
  incident: Incident;
  work: IncidentWorkState | null;
  isLoading: boolean;
}) {
  const waiting = isWaitingForAgentRuntime(incident, work);

  if (isLoading && !work) {
    return (
      <div className="grid gap-2 md:grid-cols-3">
        {WORK_STAGES.map((stage) => (
          <Skeleton key={stage} className="h-32" />
        ))}
      </div>
    );
  }

  return (
    <div className="grid gap-2 md:grid-cols-3">
      {WORK_STAGES.map((stage) => (
        <WorkCard
          key={stage}
          stage={stage}
          item={workForStage(work, stage)}
          waitingForAgent={waiting && stage === "ANALYSIS"}
        />
      ))}
    </div>
  );
}
