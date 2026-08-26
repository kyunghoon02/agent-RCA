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
  ScanSearch,
  TimerOff,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type {
  AgentRunStatus,
  CollectorStatusValue,
  IncidentStatus,
  ReportStatus,
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
 * Conclusion label shown for an RCA Report.
 *
 * An inconclusive report with no named root cause is an ABSTAIN: the Agent
 * declined to conclude because the Evidence did not support one. That is a
 * correct safety outcome, so it is styled as a neutral state and never as a
 * failure.
 */
export function conclusionLabel(
  status: ReportStatus,
  hasRootCause: boolean,
): { label: string; tone: Tone; icon: LucideIcon; description: string } {
  if (status === "inconclusive" && !hasRootCause) {
    return {
      label: "ABSTAIN",
      tone: "info",
      icon: MinusCircle,
      description:
        "The Agent declined to name a root cause because the available Evidence did not support one.",
    };
  }
  if (status === "conclusive") {
    return {
      label: "CONCLUSIVE",
      tone: "success",
      icon: CheckCircle2,
      description: "A root cause is named and supported by cited Evidence.",
    };
  }
  if (status === "partial") {
    return {
      label: "PARTIAL",
      tone: "warning",
      icon: CircleDashed,
      description:
        "A root cause is named, but at least one competing hypothesis could not be resolved.",
    };
  }
  return {
    label: "INCONCLUSIVE",
    tone: "info",
    icon: MinusCircle,
    description: "No conclusion was reached from the frozen Context.",
  };
}

export function ConclusionBadge({
  status,
  hasRootCause,
  className,
}: {
  status: ReportStatus;
  hasRootCause: boolean;
  className?: string;
}) {
  const { label, tone, icon: Icon } = conclusionLabel(status, hasRootCause);
  return (
    <Badge tone={tone} className={cn("text-xs", className)}>
      <Icon aria-hidden="true" />
      {label}
    </Badge>
  );
}
