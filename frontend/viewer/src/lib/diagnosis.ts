import type {
  Incident,
  IncidentWorkState,
  ReportBundle,
  WorkQueueState,
} from "./types";

/**
 * What the Viewer can say about an Incident's RCA outcome.
 *
 * This is deliberately separate from `Incident.status`: the pipeline being
 * ANALYZING says where the work is, not what was concluded. An outcome exists
 * only when a stored RCA Report exists.
 */
export type RcaOutcome =
  | "NOT_AVAILABLE"
  | "PROVEN"
  | "PARTIAL"
  | "ABSTAIN"
  | "AMBIGUOUS";

export type DiagnosisState =
  | "WAITING_COLLECTION"
  | "WAITING_LOCALIZATION"
  | "WAITING_AGENT"
  | "AGENT_ANALYZING"
  | "PIPELINE_PARTIAL"
  | "FAILED"
  | "PROVEN"
  | "PARTIAL"
  | "ABSTAIN"
  | "AMBIGUOUS";

export type DiagnosisTone = "waiting" | "active" | "resolved" | "caution" | "failed";

export interface Diagnosis {
  state: DiagnosisState;
  title: string;
  description: string;
  tone: DiagnosisTone;
  /** Outcome of stored analysis. NOT_AVAILABLE whenever no Report exists. */
  outcome: RcaOutcome;
  /** True when the analysis queue is pinned but nothing has claimed it. */
  awaitingAgentRuntime: boolean;
}

/**
 * Maps a stored Report to an outcome.
 *
 * This is a field mapping, not a re-derivation: the Agent already decided, and
 * the Viewer only renames its recorded verdict. `inconclusive` splits on
 * whether a root cause was recorded — none means the Agent abstained, one means
 * it could not settle between candidates.
 */
export type ReportedOutcome = Exclude<RcaOutcome, "NOT_AVAILABLE">;

export function outcomeFromReport(bundle: ReportBundle): ReportedOutcome {
  const report = bundle.report;
  if (report.status === "conclusive") return "PROVEN";
  if (report.status === "partial") return "PARTIAL";
  return report.root_cause === null ? "ABSTAIN" : "AMBIGUOUS";
}

/** An analysis work item that exists, is READY, and was never claimed. */
export function isUnclaimedAnalysis(analysis: WorkQueueState | null): boolean {
  return (
    analysis !== null &&
    analysis.state === "READY" &&
    analysis.attempt_count === 0 &&
    analysis.worker_id === null
  );
}

/**
 * A PARTIAL Report is legal both with and without an accepted root cause, so
 * one sentence cannot describe both. Neither is a pipeline failure.
 */
export const PARTIAL_WITH_ROOT_CAUSE =
  "A root cause was recorded, but the investigation completed with unresolved Evidence or collection gaps.";
export const PARTIAL_WITHOUT_ROOT_CAUSE =
  "The investigation produced partial findings, but the available Evidence did not support an accepted root cause.";

const OUTCOME_COPY: Record<
  ReportedOutcome,
  { title: string; description: string; tone: DiagnosisTone; state: DiagnosisState }
> = {
  PROVEN: {
    state: "PROVEN",
    title: "Root cause identified",
    description:
      "The Agent recorded a conclusive root cause supported by cited Evidence.",
    tone: "resolved",
  },
  PARTIAL: {
    state: "PARTIAL",
    title: "Partial diagnosis",
    // Replaced at derivation time: a PARTIAL Report may or may not carry a
    // root cause, and the two mean different things.
    description: PARTIAL_WITH_ROOT_CAUSE,
    tone: "caution",
  },
  ABSTAIN: {
    state: "ABSTAIN",
    title: "Agent abstained",
    description:
      "The Agent declined to name a root cause because the available Evidence did not support one. This is the intended safe outcome, not a failure.",
    tone: "caution",
  },
  AMBIGUOUS: {
    state: "AMBIGUOUS",
    title: "Ambiguous diagnosis",
    description:
      "The Agent recorded candidates but could not settle on a single root cause.",
    tone: "caution",
  },
};

/**
 * Resolves the single question the detail page must answer first: what, if
 * anything, is known about this Incident's root cause, and if nothing, why.
 *
 * Every branch is grounded in stored state — Incident status, the durable work
 * queue, and stored Reports. Nothing is inferred from Evidence values.
 */
export function deriveDiagnosis(
  incident: Incident,
  work: IncidentWorkState | null,
  reports: readonly ReportBundle[],
): Diagnosis {
  const analysis = work?.analysis ?? null;
  const awaitingAgentRuntime =
    incident.status === "ANALYZING" && isUnclaimedAnalysis(analysis);

  const bundle = reports.at(0);
  if (bundle) {
    const outcome = outcomeFromReport(bundle);
    const copy = OUTCOME_COPY[outcome];
    const description =
      outcome === "PARTIAL"
        ? bundle.report.root_cause !== null
          ? PARTIAL_WITH_ROOT_CAUSE
          : PARTIAL_WITHOUT_ROOT_CAUSE
        : copy.description;
    return { ...copy, description, outcome, awaitingAgentRuntime: false };
  }

  if (incident.status === "FAILED") {
    return {
      state: "FAILED",
      title: "Pipeline failed",
      description:
        "The Incident ended as FAILED before an RCA Report was produced. The lifecycle and Timeline show the stage that stopped it.",
      tone: "failed",
      outcome: "NOT_AVAILABLE",
      awaitingAgentRuntime: false,
    };
  }

  if (incident.status === "PARTIAL") {
    return {
      state: "PIPELINE_PARTIAL",
      title: "Pipeline ended incomplete",
      description:
        "Collection or localization finished with gaps and analysis was not attempted, so no Report exists.",
      tone: "caution",
      outcome: "NOT_AVAILABLE",
      awaitingAgentRuntime: false,
    };
  }

  if (awaitingAgentRuntime) {
    return {
      state: "WAITING_AGENT",
      title: "Waiting for Agent runtime",
      description:
        "The Frozen Context is ready and pinned, but no Agent runtime has claimed this analysis work.",
      tone: "waiting",
      outcome: "NOT_AVAILABLE",
      awaitingAgentRuntime: true,
    };
  }

  if (incident.status === "ANALYZING" && analysis?.state === "RUNNING") {
    return {
      state: "AGENT_ANALYZING",
      title: "Agent analyzing",
      description: analysis.worker_id
        ? `Analysis work is claimed by ${analysis.worker_id} and running against the pinned Frozen Context.`
        : "Analysis work is claimed and running against the pinned Frozen Context.",
      tone: "active",
      outcome: "NOT_AVAILABLE",
      awaitingAgentRuntime: false,
    };
  }

  if (incident.status === "LOCALIZING") {
    return {
      state: "WAITING_LOCALIZATION",
      title: "Waiting for localization",
      description:
        "Evidence has been collected. Localization must freeze a Context before any analysis can start.",
      tone: "waiting",
      outcome: "NOT_AVAILABLE",
      awaitingAgentRuntime: false,
    };
  }

  // RECEIVED, COLLECTING, or an ANALYZING Incident whose analysis row is absent.
  return {
    state: "WAITING_COLLECTION",
    title: "Waiting for collection",
    description:
      "Evidence collection has not finished, so there is nothing to localize or analyse yet.",
    tone: "waiting",
    outcome: "NOT_AVAILABLE",
    awaitingAgentRuntime: false,
  };
}

export const OUTCOME_LABELS: Record<RcaOutcome, string> = {
  NOT_AVAILABLE: "Not available",
  PROVEN: "PROVEN",
  PARTIAL: "PARTIAL",
  ABSTAIN: "ABSTAIN",
  AMBIGUOUS: "AMBIGUOUS",
};

/**
 * A controlled/evaluation Incident, identified only by an explicit label.
 *
 * Returns null when no label proves it either way — the Viewer never guesses
 * from an alert name.
 */
export function controlledVerificationId(incident: Incident): string | null {
  const labels = incident.alert.labels ?? {};
  const id = labels["verification_id"];
  return typeof id === "string" && id.trim() ? id.trim() : null;
}
