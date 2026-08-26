import { cn } from "@/lib/utils";
import { entityKind, entityNamespace } from "@/lib/lifecycle";
import { isGraphEntityRef, type EntityRef } from "@/lib/types";

/** Compact `Kind/name` identity with an optional namespace and existence flag. */
export function EntityRefLabel({
  entity,
  showNamespace = true,
  className,
}: {
  entity: EntityRef;
  showNamespace?: boolean;
  className?: string;
}) {
  const namespace = entityNamespace(entity);
  return (
    <span className={cn("inline-flex items-baseline gap-1.5", className)}>
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
        {entityKind(entity)}
      </span>
      <span className="font-mono text-xs">{entity.name}</span>
      {showNamespace && namespace && (
        <span className="text-[11px] text-muted-foreground">in {namespace}</span>
      )}
      {!entity.exists && (
        <span className="text-[11px] font-medium text-status-critical">not found</span>
      )}
    </span>
  );
}

/** Full identity block for detail panes, including the graph entity id. */
export function EntityRefDetail({ entity }: { entity: EntityRef }) {
  const namespace = entityNamespace(entity);
  const rows: [string, string][] = [
    ["Kind", entityKind(entity)],
    ["Name", entity.name],
    ["Namespace", namespace ?? "—"],
  ];
  if (isGraphEntityRef(entity)) {
    rows.push(["Entity ID", entity.entity_id], ["Domain", entity.domain]);
    if (entity.external_ref) rows.push(["External ref", entity.external_ref]);
  } else {
    if (entity.cluster_id) rows.push(["Cluster", entity.cluster_id]);
    rows.push(["UID", entity.uid ?? "—"]);
  }
  rows.push(["Exists", entity.exists ? "yes" : "no"]);

  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
      {rows.map(([term, value]) => (
        <div key={term} className="contents">
          <dt className="text-muted-foreground">{term}</dt>
          <dd className="font-mono break-all">{value}</dd>
        </div>
      ))}
    </dl>
  );
}
