import {
  AlertOctagon,
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  Clock,
  Download,
  FileCheck2,
  Info,
  Inbox,
  Loader,
  Locate,
  MinusCircle,
  PauseCircle,
  ScanSearch,
  ShieldQuestion,
  Split,
  TimerOff,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { OUTCOME_LABELS, type RcaOutcome } from "@/lib/diagnosis";
import type {
  AgentRunStatus,
  CollectorStatusValue,
  IncidentStatus,
  Severity,
  WorkState,
} from "@/lib/types";

type Tone = NonNullable<BadgeProps["tone"]>;

interface Presentation {
  label: string;
  tone: Tone;
  icon: LucideIcon;
}

/**
 * Status presentation lives in one place so the same state never renders two
 * ways. Every entry carries an icon and a written label: colour is redundant
 * reinforcement, never the only signal.
 */
const INCIDENT_STATUS: Record<IncidentStatus, Presentation> = {
  RECEIVED: { label: "RECEIVED", tone: "neutral", icon: Inbox },
  COLLECTING: { label: "COLLECTING", tone: "running", icon: Download },
  LOCALIZING: { label: "LOCALIZING", tone: "running", icon: Locate },
  ANALYZING: { label: "ANALYZING", tone: "running", icon: ScanSearch },
  REPORTED: { label: "REPORTED", tone: "success", icon: FileCheck2 },
  PARTIAL: { label: "PARTIAL", tone: "warning", icon: CircleDashed },
  FAILED: { label: "FAILED", tone: "critical", icon: XCircle },
};

const SEVERITY: Record<Severity, Presentation> = {
  critical: { label: "critical", tone: "critical", icon: AlertOctagon },
  warning: { label: "warning", tone: "warning", icon: AlertTriangle },
  info: { label: "info", tone: "info", icon: Info },
};

const WORK_STATE: Record<WorkState, Presentation> = {
  READY: { label: "READY", tone: "neutral", icon: Clock },
  RUNNING: { label: "RUNNING", tone: "running", icon: Loader },
  SUCCEEDED: { label: "SUCCEEDED", tone: "success", icon: CheckCircle2 },
  FAILED: { label: "FAILED", tone: "critical", icon: XCircle },
};

const COLLECTOR_STATUS: Record<CollectorStatusValue, Presentation> = {
  PENDING: { label: "PENDING", tone: "neutral", icon: Clock },
  RUNNING: { label: "RUNNING", tone: "running", icon: Loader },
  SUCCEEDED: { label: "SUCCEEDED", tone: "success", icon: CheckCircle2 },
  PARTIAL: { label: "PARTIAL", tone: "warning", icon: CircleDashed },
  FAILED: { label: "FAILED", tone: "critical", icon: XCircle },
  TIMED_OUT: { label: "TIMED_OUT", tone: "critical", icon: TimerOff },
  SKIPPED: { label: "SKIPPED", tone: "outline", icon: MinusCircle },
};

const AGENT_RUN_STATUS: Record<AgentRunStatus, Presentation> = {
  SUCCEEDED: { label: "SUCCEEDED", tone: "success", icon: CheckCircle2 },
  GATE_REJECTED: { label: "GATE_REJECTED", tone: "warning", icon: CircleDashed },
  MODEL_FAILED: { label: "MODEL_FAILED", tone: "critical", icon: XCircle },
  BUDGET_EXHAUSTED: { label: "BUDGET_EXHAUSTED", tone: "warning", icon: TimerOff },
};

function PresentedBadge({
  presentation,
  className,
  spin = false,
}: {
  presentation: Presentation;
  className?: string;
  spin?: boolean;
}) {
  const Icon = presentation.icon;
  return (
    <Badge tone={presentation.tone} className={className}>
      <Icon aria-hidden="true" className={cn(spin && "animate-[spin_2s_linear_infinite]")} />
      {presentation.label}
    </Badge>
  );
}

export function IncidentStatusBadge({
  status,
  className,
}: {
  status: IncidentStatus;
  className?: string;
}) {
  const presentation = INCIDENT_STATUS[status];
  return (
    <PresentedBadge
      presentation={presentation}
      className={className}
      spin={status === "COLLECTING" || status === "LOCALIZING" || status === "ANALYZING"}
    />
  );
}

export function SeverityBadge({
  severity,
  className,
}: {
  severity: Severity;
  className?: string;
}) {
  return <PresentedBadge presentation={SEVERITY[severity]} className={className} />;
}

export function WorkStateBadge({
  state,
  className,
}: {
  state: WorkState;
  className?: string;
}) {
  return (
    <PresentedBadge
      presentation={WORK_STATE[state]}
      className={className}
      spin={state === "RUNNING"}
    />
  );
}

export function CollectorStatusBadge({
  status,
  className,
}: {
  status: CollectorStatusValue;
  className?: string;
}) {
  return (
    <PresentedBadge
      presentation={COLLECTOR_STATUS[status]}
      className={className}
      spin={status === "RUNNING"}
    />
  );
}

export function AgentRunStatusBadge({
  status,
  className,
}: {
  status: AgentRunStatus;
  className?: string;
}) {
  return <PresentedBadge presentation={AGENT_RUN_STATUS[status]} className={className} />;
}

/**
 * RCA outcome badge.
 *
 * Outcome is not pipeline status. `NOT_AVAILABLE` is the honest default and
 * renders as a plain, non-alarming state: no Report exists yet, which is not a
 * failure.
 */
const OUTCOME_PRESENTATION: Record<
  RcaOutcome,
  { tone: Tone; icon: LucideIcon; hint: string }
> = {
  NOT_AVAILABLE: {
    tone: "outline",
    icon: MinusCircle,
    hint: "No RCA Report has been stored for this Incident.",
  },
  PROVEN: {
    tone: "success",
    icon: CheckCircle2,
    hint: "A root cause was recorded and supported by cited Evidence.",
  },
  PARTIAL: {
    tone: "warning",
    icon: CircleDashed,
    // A PARTIAL Report is legal with or without an accepted root cause, so this
    // must not assert that one was recorded. The Diagnosis panel carries the
    // precise wording once the Report is known.
    hint: "The investigation produced partial findings; an accepted root cause may still be unresolved.",
  },
  ABSTAIN: {
    tone: "info",
    icon: ShieldQuestion,
    hint: "The Agent declined to name a root cause on the available Evidence.",
  },
  AMBIGUOUS: {
    tone: "warning",
    icon: Split,
    hint: "Candidates were recorded but none was settled on.",
  },
};

export function RcaOutcomeBadge({
  outcome,
  className,
}: {
  outcome: RcaOutcome;
  className?: string;
}) {
  const presentation = OUTCOME_PRESENTATION[outcome];
  const Icon = presentation.icon;
  return (
    <Badge tone={presentation.tone} className={className} title={presentation.hint}>
      <Icon aria-hidden="true" />
      {OUTCOME_LABELS[outcome]}
    </Badge>
  );
}

/**
 * Whether anything has claimed the analysis queue.
 *
 * Deliberately worded as an observation about the work item, not a claim about
 * cluster state: the Viewer cannot see Agent runtimes, only whether the queue
 * was drained.
 */
export function AgentRuntimeBadge({
  awaiting,
  className,
}: {
  awaiting: boolean;
  className?: string;
}) {
  if (!awaiting) return null;
  return (
    <Badge
      tone="info"
      className={className}
      title="The analysis work item is READY and has never been claimed."
    >
      <PauseCircle aria-hidden="true" />
      No Agent runtime has claimed this
    </Badge>
  );
}
