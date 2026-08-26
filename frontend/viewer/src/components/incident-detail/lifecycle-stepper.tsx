"use client";

import { Check, Circle, CircleDashed, Minus, X } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { completedStepCount, type LifecycleStep, type StepState } from "@/lib/lifecycle";
import { cn } from "@/lib/utils";

const STATE_PRESENTATION: Record<
  StepState,
  { icon: LucideIcon; srLabel: string; dot: string; text: string; connector: string }
> = {
  complete: {
    icon: Check,
    srLabel: "completed",
    dot: "border-status-success bg-status-success-surface text-status-success",
    text: "text-foreground",
    connector: "bg-status-success",
  },
  current: {
    icon: Circle,
    srLabel: "in progress",
    dot: "border-status-running bg-status-running-surface text-status-running",
    text: "text-foreground font-semibold",
    connector: "bg-border",
  },
  failed: {
    icon: X,
    srLabel: "failed at this step",
    dot: "border-status-critical bg-status-critical-surface text-status-critical",
    text: "text-status-critical font-semibold",
    connector: "bg-border",
  },
  pending: {
    icon: CircleDashed,
    srLabel: "not started",
    dot: "border-border bg-surface-sunken text-muted-foreground",
    text: "text-muted-foreground",
    connector: "bg-border",
  },
  "not-reached": {
    icon: Minus,
    srLabel: "never reached",
    dot: "border-dashed border-border bg-transparent text-muted-foreground/60",
    text: "text-muted-foreground/60 line-through decoration-1",
    connector: "bg-border",
  },
};

/**
 * The RECEIVED → REPORTED path.
 *
 * A step is only drawn as complete when the Incident is known to have passed
 * through it. After a terminal failure the remaining steps read "never reached"
 * rather than "pending", so the run is never shown as having skipped ahead.
 */
export function LifecycleStepper({ steps }: { steps: LifecycleStep[] }) {
  const completed = completedStepCount(steps);
  return (
    <div>
      <div className="mb-1.5 flex items-baseline gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Lifecycle
        </h2>
        <span className="tabular text-[11px] text-muted-foreground">
          {completed} of {steps.length} steps completed
        </span>
      </div>
      <ol
        className="scroll-x flex w-full min-w-0 max-w-full items-start gap-0"
        aria-label="Incident lifecycle"
      >
        {steps.map((step, index) => {
          const presentation = STATE_PRESENTATION[step.state];
          const Icon = presentation.icon;
          const isLast = index === steps.length - 1;
          return (
            <li
              key={step.status}
              className="flex min-w-28 flex-1 shrink-0 items-start gap-0"
              data-state={step.state}
              data-status={step.status}
            >
              <div className="flex w-24 shrink-0 flex-col items-center gap-1 px-1">
                <span
                  className={cn(
                    "flex size-6 items-center justify-center rounded-full border-2",
                    presentation.dot,
                  )}
                >
                  <Icon className="size-3" strokeWidth={3} aria-hidden="true" />
                </span>
                <span className={cn("text-center text-[11px] leading-tight", presentation.text)}>
                  {step.status}
                </span>
                <span className="sr-only">{presentation.srLabel}</span>
              </div>
              {!isLast && (
                <span
                  aria-hidden="true"
                  className={cn("mt-3 h-0.5 min-w-4 flex-1", presentation.connector)}
                />
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
