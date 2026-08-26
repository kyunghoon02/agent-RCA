"use client";

import { Hourglass, PauseCircle } from "lucide-react";
import { Notice } from "@/components/notices";
import { WorkStateBadge } from "@/components/status";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatTimestamp } from "@/lib/format";
import type { Incident, IncidentWorkState, WorkQueueState, WorkStage } from "@/lib/types";
import { isWaitingForAgentRuntime, WORK_STAGES, WORK_STAGE_LABELS, workForStage } from "@/lib/work";

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <dt className="shrink-0 text-[11px] text-muted-foreground">{label}</dt>
      <dd className="truncate font-mono text-[11px]">{value}</dd>
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
            <dl className="flex flex-col gap-1">
              <Field label="Attempts" value={item.attempt_count} />
              <Field label="Worker" value={item.worker_id ?? "—"} />
              <Field
                label="Lease expires"
                value={item.lease_expires_at ? formatTimestamp(item.lease_expires_at) : "—"}
              />
              <Field
                label="Claimed"
                value={item.claimed_at ? formatTimestamp(item.claimed_at) : "—"}
              />
              <Field
                label="Completed"
                value={item.completed_at ? formatTimestamp(item.completed_at) : "—"}
              />
              <Field
                label="Last error"
                value={
                  item.last_error_code ? (
                    <span className="text-status-critical">{item.last_error_code}</span>
                  ) : (
                    "none"
                  )
                }
              />
              <Field label="Pinned context" value={item.context_id ?? "—"} />
            </dl>

            {waitingForAgent && (
              // Deliberately neutral: an undeployed Agent runtime is a waiting
              // state, not an error, and must not read as a red failure.
              <Notice
                tone="info"
                icon={PauseCircle}
                title="Waiting for Agent runtime"
                className="mt-2"
              >
                The Frozen Context is pinned and this work item is READY, but nothing has
                claimed it. Analysis begins when an Agent runtime is available.
              </Notice>
            )}

            {item.state === "READY" && !waitingForAgent && (
              <p className="mt-2 flex items-center gap-1.5 text-[11px] text-muted-foreground">
                <Hourglass className="size-3" aria-hidden="true" />
                Queued since {formatTimestamp(item.available_at)}
              </p>
            )}
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
          <Skeleton key={stage} className="h-48" />
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
