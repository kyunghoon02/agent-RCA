import { entityKind } from "./lifecycle";
import type { ContextPackage, EntityRef, StatePath } from "./types";
import { isInvestigationScope } from "./types";
import { subjectKey } from "./evidence-grouping";

/**
 * One distinct entity-type chain, e.g. Service → Service → Pod → Node → Pod,
 * with how many concrete paths share that shape.
 */
export interface ChainShape {
  types: string[];
  relations: string[];
  count: number;
  /**
   * Distinct Evidence IDs across every path with this shape.
   *
   * Paths overlap heavily — the same Pod Evidence appears on the Service→Pod
   * path and again on Service→Pod→Node — so summing per-path references would
   * count one observation many times.
   */
  uniqueEvidenceCount: number;
  pathIds: string[];
}

export interface TopologySummary {
  seedEntity: EntityRef | null;
  namespaces: string[];
  entityCount: number;
  pathCount: number;
  evidenceCount: number;
  recentChangeCount: number;
  missingEvidenceCount: number;
  collectorFailureCount: number;
  strategy: ContextPackage["localization"]["strategy"];
  completeness: number;
  /** Distinct chain shapes, most frequent first. */
  shapes: ChainShape[];
  /** Entity types shared by the front of every path, collapsed once. */
  commonPrefix: string[];
}

/**
 * Shape identity: entity types *and* the ordered relations between them.
 *
 * Two paths over the same entity types can describe entirely different
 * topology — `Service --SELECTS--> Pod` is not `Service --DEPENDS_ON--> Pod` —
 * so keying on types alone would merge them and then display one path's
 * relations as if they applied to both.
 */
function chainKey(path: StatePath): string {
  const parts: string[] = [];
  path.entities.forEach((entity, index) => {
    if (index > 0) parts.push(path.relations[index - 1] ?? RELATION_FALLBACK);
    parts.push(entityKind(entity));
  });
  return parts.join("|");
}

/** Rendered when a path omits a relation for an edge. */
export const RELATION_FALLBACK = "RELATED";

function longestCommonPrefix(chains: string[][]): string[] {
  if (chains.length === 0) return [];
  const prefix: string[] = [];
  for (let index = 0; index < chains[0].length; index += 1) {
    const candidate = chains[0][index];
    if (chains.every((chain) => chain[index] === candidate)) prefix.push(candidate);
    else break;
  }
  return prefix;
}

/**
 * Condenses a Frozen Context's StateGraph paths into a readable topology.
 *
 * A live Context routinely carries 20+ paths that differ only in their tail, so
 * the raw list reads as repetition. Grouping by entity-type shape shows the
 * actual topology once and reports how many concrete paths share it.
 */
export function summariseTopology(context: ContextPackage): TopologySummary {
  const shapes = new Map<string, ChainShape>();
  // Per-shape Evidence identity, so an ID shared by several paths counts once.
  const shapeEvidence = new Map<string, Set<string>>();
  const entityKeys = new Set<string>();
  const namespaces = new Set<string>();

  for (const path of context.state_paths) {
    const key = chainKey(path);
    let shape = shapes.get(key);
    if (!shape) {
      shape = {
        types: path.entities.map((entity) => entityKind(entity)),
        // One relation per edge, matching what the key encodes, so the rendered
        // relations are true for every path in this shape.
        relations: path.entities
          .slice(1)
          .map((_, index) => path.relations[index] ?? RELATION_FALLBACK),
        count: 0,
        uniqueEvidenceCount: 0,
        pathIds: [],
      };
      shapes.set(key, shape);
      shapeEvidence.set(key, new Set<string>());
    }
    shape.count += 1;
    shape.pathIds.push(path.path_id);

    const seen = shapeEvidence.get(key)!;
    for (const evidenceId of path.evidence_ids) seen.add(evidenceId);
    shape.uniqueEvidenceCount = seen.size;

    for (const entity of path.entities) {
      entityKeys.add(subjectKey(entity));
      const scope = (entity as { scope?: Record<string, unknown> }).scope;
      const namespace =
        (entity as { namespace?: string | null }).namespace ??
        (typeof scope?.["namespace"] === "string" ? (scope["namespace"] as string) : null);
      if (namespace) namespaces.add(namespace);
    }
  }

  const scope = context.scope;
  if (isInvestigationScope(scope)) {
    const scoped = scope.correlation_keys?.["namespace"];
    if (scoped) namespaces.add(scoped);
  } else {
    for (const namespace of scope.namespaces) namespaces.add(namespace);
  }

  const ordered = [...shapes.values()].sort((left, right) =>
    right.count === left.count
      ? right.types.length - left.types.length
      : right.count - left.count,
  );

  return {
    seedEntity: context.source_entity ?? null,
    namespaces: [...namespaces].sort(),
    entityCount: entityKeys.size,
    pathCount: context.state_paths.length,
    evidenceCount: context.evidence_ids.length,
    recentChangeCount: context.recent_change_evidence_ids.length,
    missingEvidenceCount: context.missing_evidence.length,
    collectorFailureCount: context.collector_failures.length,
    strategy: context.localization.strategy,
    completeness: context.localization.context_completeness,
    shapes: ordered,
    commonPrefix: longestCommonPrefix(ordered.map((shape) => shape.types)),
  };
}
