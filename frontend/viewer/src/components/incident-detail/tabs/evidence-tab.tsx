"use client";

import * as React from "react";
import {
  ChevronRight,
  CircleDashed,
  Database,
  EyeOff,
  LayoutGrid,
  Rows3,
  ShieldAlert,
  Boxes,
  TriangleAlert,
} from "lucide-react";
import { EmptyState } from "@/components/empty-state";
import { JsonViewer } from "@/components/json-viewer";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  buildRelevanceIndex,
  groupEvidenceBySubject,
  isDegraded,
  relevanceTags,
  resultStatus,
  summariseEvidence,
  RELEVANCE_LABELS,
  type RelevanceIndex,
  type RelevanceTag,
  type SubjectGroup,
} from "@/lib/evidence-grouping";
import { insufficiencyDetail } from "@/lib/evidence";
import { formatRatio, formatTimestamp, shortHash } from "@/lib/format";
import { entityKind } from "@/lib/lifecycle";
import type { ContextPackage, EvidenceItem, ReportBundle } from "@/lib/types";
import { cn } from "@/lib/utils";

const ALL = "__all__";
/** Upper bound on frames spent waiting for the focused card to gain layout. */
const MAX_SCROLL_FRAMES = 10;
type ViewMode = "investigation" | "table" | "cards";

const RELEVANCE_TONE: Record<RelevanceTag, "success" | "info" | "warning" | "outline"> = {
  CITED_BY_REPORT: "success",
  IN_CONTEXT: "info",
  RECENT_CHANGE: "warning",
  NOT_USED_BY_REPORT: "outline",
  OUTSIDE_CONTEXT: "outline",
};

function distinct(values: string[]): string[] {
  return [...new Set(values)].sort();
}

export function EvidenceTab({
  evidence,
  contexts,
  reports,
  focusedEvidenceId,
}: {
  evidence: EvidenceItem[];
  contexts: ContextPackage[];
  reports: ReportBundle[];
  focusedEvidenceId: string | null;
}) {
  const [view, setView] = React.useState<ViewMode>("investigation");
  const [source, setSource] = React.useState(ALL);
  const [kind, setKind] = React.useState(ALL);
  const [relevance, setRelevance] = React.useState<string>(ALL);
  const [quality, setQuality] = React.useState(ALL);
  const [subject, setSubject] = React.useState("");

  const index = React.useMemo(
    () => buildRelevanceIndex(evidence, contexts, reports),
    [evidence, contexts, reports],
  );

  const filtered = React.useMemo(
    () =>
      evidence.filter((item) => {
        if (source !== ALL && item.source !== source) return false;
        if (kind !== ALL && item.kind !== kind) return false;
        if (quality === "degraded" && !isDegraded(item)) return false;
        if (quality === "complete" && isDegraded(item)) return false;
        if (relevance !== ALL) {
          if (!relevanceTags(item.evidence_id, index).includes(relevance as RelevanceTag)) {
            return false;
          }
        }
        if (subject.trim()) {
          const needle = subject.trim().toLowerCase();
          const haystack = [
            item.subject.name,
            item.evidence_id,
            item.summary,
            item.provenance.provider,
          ]
            .join(" ")
            .toLowerCase();
          if (!haystack.includes(needle)) return false;
        }
        return true;
      }),
    [evidence, source, kind, quality, relevance, subject, index],
  );

  const groups = React.useMemo(
    () => groupEvidenceBySubject(filtered, index),
    [filtered, index],
  );
  const allGroups = React.useMemo(
    () => groupEvidenceBySubject(evidence, index),
    [evidence, index],
  );
  const summary = React.useMemo(
    () => summariseEvidence(evidence, index, contexts, allGroups),
    [evidence, index, contexts, allGroups],
  );

  const sources = React.useMemo(() => distinct(evidence.map((e) => e.source)), [evidence]);
  const kinds = React.useMemo(() => distinct(evidence.map((e) => e.kind)), [evidence]);

  // A deep link from the Report, Context or Timeline must never land on a
  // filtered-out item, and the raw card is where the full record lives.
  React.useEffect(() => {
    if (!focusedEvidenceId) return;
    setSource(ALL);
    setKind(ALL);
    setQuality(ALL);
    setRelevance(ALL);
    setSubject("");
    setView("cards");
  }, [focusedEvidenceId]);

  /*
   * Scrolling is driven by a callback ref rather than a post-render DOM query.
   * Clearing the filters and switching to Raw cards is a state change, so the
   * target card does not exist yet when the effect above runs. The ref fires
   * exactly when the focused card mounts, and its identity is keyed on the
   * focused ID so following a second link re-attaches and scrolls again.
   *
   * Mounting is still not enough to scroll: a deep link also switches tabs, and
   * the tab panel is display:none for the first frames, which gives the card a
   * zero-sized box and makes scrollIntoView a no-op. So the scroll waits for
   * the element to actually have layout, checked once per animation frame and
   * bounded so it can never spin.
   */
  const focusedCardRef = React.useCallback(
    (node: HTMLDivElement | null) => {
      if (!node) return;

      const hasLayout = () =>
        node.offsetParent !== null && node.getBoundingClientRect().height > 0;

      // Usually the card already has layout by the time its ref runs, so scroll
      // straight away. Doing this synchronously also means the deep link still
      // works when the page is not producing animation frames.
      if (hasLayout()) {
        node.scrollIntoView({ block: "center" });
        return;
      }

      // Otherwise the tab panel is still being shown and the card has no box
      // yet. Re-check once per frame, bounded so this can never spin.
      let frames = 0;
      const scrollWhenLaidOut = () => {
        if (hasLayout() || frames >= MAX_SCROLL_FRAMES) {
          node.scrollIntoView({ block: "center" });
          return;
        }
        frames += 1;
        requestAnimationFrame(scrollWhenLaidOut);
      };
      requestAnimationFrame(scrollWhenLaidOut);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [focusedEvidenceId],
  );

  if (evidence.length === 0) {
    return (
      <EmptyState
        icon={Database}
        title="No Evidence was stored for this Incident"
        description="Collection either has not run yet or produced nothing. The Overview tab shows each Provider's result and error code."
      />
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <EvidenceSummaryBar summary={summary} />

      <Card className="sticky top-10 z-20 p-2.5">
        <div className="flex flex-wrap items-end gap-2">
          <FilterSelect id="ev-source" label="Source" value={source} onChange={setSource}
            options={sources} allLabel="All sources" />
          <FilterSelect id="ev-kind" label="Kind" value={kind} onChange={setKind}
            options={kinds} allLabel="All kinds" />
          <FilterSelect id="ev-relevance" label="Relevance" value={relevance}
            onChange={setRelevance}
            options={(Object.keys(RELEVANCE_LABELS) as RelevanceTag[]).filter(
              (tag) => tag !== "CITED_BY_REPORT" || index.hasReport,
            )}
            optionLabel={(value) => RELEVANCE_LABELS[value as RelevanceTag]}
            allLabel="Any relevance" />
          <FilterSelect id="ev-quality" label="Result" value={quality} onChange={setQuality}
            options={["complete", "degraded"]} allLabel="Any result" />
          <div className="flex flex-col gap-1">
            <Label htmlFor="ev-subject">Search</Label>
            <Input id="ev-subject" value={subject} placeholder="pod, metric, id"
              onChange={(event) => setSubject(event.target.value)} className="w-48" />
          </div>

          <div className="ml-auto flex items-center gap-1" role="group" aria-label="Evidence view">
            <ViewButton active={view === "investigation"} onClick={() => setView("investigation")}
              icon={Boxes} label="Investigation" />
            <ViewButton active={view === "table"} onClick={() => setView("table")}
              icon={Rows3} label="Table" />
            <ViewButton active={view === "cards"} onClick={() => setView("cards")}
              icon={LayoutGrid} label="Raw cards" />
          </div>
        </div>
        <p className="mt-1.5 text-[11px] text-muted-foreground">
          {view === "investigation"
            ? `${groups.length} subject${groups.length === 1 ? "" : "s"} · ${filtered.length} of ${evidence.length} Evidence items`
            : `Showing ${filtered.length} of ${evidence.length} Evidence items`}
        </p>
      </Card>

      {filtered.length === 0 ? (
        <EmptyState icon={CircleDashed} title="No Evidence matches these filters"
          description="Clear the source, kind, relevance, result or search filter to see the full set." />
      ) : view === "investigation" ? (
        <div className="flex flex-col gap-2">
          {groups.map((group) => (
            <SubjectGroupRow key={group.identity.key} group={group} index={index}
              focusedEvidenceId={focusedEvidenceId} />
          ))}
        </div>
      ) : view === "table" ? (
        <Card>
          <EvidenceTable items={filtered} index={index} focusedEvidenceId={focusedEvidenceId} />
        </Card>
      ) : (
        <div className="flex flex-col gap-2">
          {filtered.map((item) => {
            const focused = item.evidence_id === focusedEvidenceId;
            return (
              <EvidenceCard
                key={item.evidence_id}
                item={item}
                index={index}
                focused={focused}
                focusRef={focused ? focusedCardRef : undefined}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

function FilterSelect({
  id,
  label,
  value,
  onChange,
  options,
  allLabel,
  optionLabel,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (next: string) => void;
  options: string[];
  allLabel: string;
  optionLabel?: (value: string) => string;
}) {
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor={id}>{label}</Label>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger id={id} className="w-40">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>{allLabel}</SelectItem>
          {options.map((option) => (
            <SelectItem key={option} value={option}>
              {optionLabel ? optionLabel(option) : option}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function ViewButton({
  active, onClick, icon: Icon, label,
}: {
  active: boolean; onClick: () => void; icon: typeof Boxes; label: string;
}) {
  return (
    <Button variant={active ? "secondary" : "outline"} size="sm" aria-pressed={active}
      onClick={onClick}>
      <Icon className="size-3.5" aria-hidden="true" />
      <span className="hidden md:inline">{label}</span>
    </Button>
  );
}

function EvidenceSummaryBar({
  summary,
}: {
  summary: ReturnType<typeof summariseEvidence>;
}) {
  const cells: { label: string; value: number; tone?: string; hint?: string }[] = [
    { label: "Total Evidence", value: summary.total },
    { label: "Subjects", value: summary.subjects, hint: "Distinct resources observed" },
    { label: "In Frozen Context", value: summary.inContext },
    { label: "Recent change", value: summary.recentChange },
    {
      label: "Insufficient / partial",
      value: summary.degraded,
      tone: summary.degraded > 0 ? "text-status-warning" : undefined,
    },
    {
      label: "Provider failures",
      value: summary.providerFailures,
      tone: summary.providerFailures > 0 ? "text-status-critical" : undefined,
    },
  ];
  return (
    <Card>
      <dl className="grid grid-cols-2 divide-border sm:grid-cols-3 lg:grid-cols-6 lg:divide-x">
        {cells.map((cell) => (
          <div key={cell.label} className="px-3 py-2">
            <dt className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
              {cell.label}
            </dt>
            <dd className={cn("tabular text-lg font-semibold leading-tight", cell.tone)}>
              {cell.value}
            </dd>
            {cell.hint && (
              <p className="text-[10px] text-muted-foreground">{cell.hint}</p>
            )}
          </div>
        ))}
      </dl>
    </Card>
  );
}

function RelevanceBadges({ id, index }: { id: string; index: RelevanceIndex }) {
  const tags = relevanceTags(id, index);
  if (tags.length === 0) return null;
  return (
    <>
      {tags.map((tag) => (
        <Badge key={tag} tone={RELEVANCE_TONE[tag]}>
          {RELEVANCE_LABELS[tag]}
        </Badge>
      ))}
    </>
  );
}

function ResultBadge({ item }: { item: EvidenceItem }) {
  const status = resultStatus(item);
  const degraded = isDegraded(item);
  if (!status && !degraded) return null;
  return (
    <Badge tone={degraded ? "critical" : "outline"}>
      {degraded && <TriangleAlert aria-hidden="true" />}
      {status ?? `completeness ${formatRatio(item.quality.completeness)}`}
    </Badge>
  );
}

/**
 * One observed subject and everything seen about it.
 *
 * Called "Observed signals" rather than "Supporting Evidence": nothing here has
 * been judged to support anything until an RCA Report cites it, and that is
 * shown separately as a "Cited by Report" tag.
 */
function SubjectGroupRow({
  group, index, focusedEvidenceId,
}: {
  group: SubjectGroup; index: RelevanceIndex; focusedEvidenceId: string | null;
}) {
  const containsFocus = group.items.some((i) => i.evidence_id === focusedEvidenceId);
  const [open, setOpen] = React.useState(containsFocus);
  React.useEffect(() => {
    if (containsFocus) setOpen(true);
  }, [containsFocus]);

  const identity = group.identity;
  const bodyId = React.useId();

  return (
    <Card className={cn(containsFocus && "ring-1 ring-ring")}>
      <button type="button" aria-expanded={open} aria-controls={bodyId}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-start gap-2 px-3 py-2 text-left hover:bg-accent/50">
        <ChevronRight aria-hidden="true"
          className={cn("mt-1 size-3.5 shrink-0 transition-transform", open && "rotate-90")} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
              {identity.kind}
            </span>
            <span className="font-mono text-xs font-medium">{identity.name}</span>
            {identity.namespace && (
              <span className="text-[11px] text-muted-foreground">in {identity.namespace}</span>
            )}
            {!identity.exists && (
              <Badge tone="critical">not found</Badge>
            )}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <Badge tone="neutral">
              {group.items.length} observed signal{group.items.length === 1 ? "" : "s"}
            </Badge>
            {group.citedCount > 0 && (
              <Badge tone="success">{group.citedCount} cited by Report</Badge>
            )}
            {group.inContextCount > 0 && (
              <Badge tone="info">{group.inContextCount} in Context</Badge>
            )}
            {group.recentChangeCount > 0 && (
              <Badge tone="warning">{group.recentChangeCount} recent change</Badge>
            )}
            {group.degradedCount > 0 && (
              <Badge tone="critical">
                <TriangleAlert aria-hidden="true" />
                {group.degradedCount} degraded
              </Badge>
            )}
            {group.sources.map((s) => (
              <Badge key={s} tone="outline">{s}</Badge>
            ))}
          </div>
          {identity.uid && (
            <p className="mt-1 font-mono text-[10px] text-muted-foreground">
              uid {identity.uid}
            </p>
          )}
        </div>
        <span className="tabular shrink-0 text-[11px] text-muted-foreground">
          {formatTimestamp(group.lastObservedAt)}
        </span>
      </button>

      {open && (
        <div id={bodyId} className="border-t border-border">
          <ul className="divide-y divide-border">
            {group.items.map((item) => (
              <li key={item.evidence_id} id={`evidence-${item.evidence_id}`}
                className={cn("scroll-mt-32 px-3 py-2",
                  item.evidence_id === focusedEvidenceId && "bg-accent")}>
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="font-mono text-[11px]">{item.evidence_id}</span>
                  <Badge tone="outline">{item.source}</Badge>
                  <Badge tone="outline">{item.kind}</Badge>
                  <ResultBadge item={item} />
                  <RelevanceBadges id={item.evidence_id} index={index} />
                  <span className="tabular ml-auto text-[10px] text-muted-foreground">
                    {formatTimestamp(item.observed_at)}
                  </span>
                </div>
                <p className="mt-1 text-xs leading-relaxed">{item.summary}</p>
                {isDegraded(item) && <InsufficientDataPanel item={item} />}
                <EvidenceDetails item={item} />
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

/**
 * Why an Evidence item establishes less than it appears to.
 *
 * Stays visible rather than collapsed: an operator scanning for gaps must see
 * the cause and the series that never returned without opening anything.
 */
function InsufficientDataPanel({ item }: { item: EvidenceItem }) {
  const { reason, detail, missingSeries, observed } = insufficiencyDetail(item);
  const status = resultStatus(item);
  const severe = status === "INSUFFICIENT_DATA" || item.quality.completeness <= 0;
  return (
    <div
      className={cn(
        "mt-1.5 rounded border px-2 py-1.5 text-[11px]",
        severe
          ? "border-status-critical/40 bg-status-critical-surface"
          : "border-status-warning/40 bg-status-warning-surface",
      )}
    >
      <p className="flex items-center gap-1.5 font-medium">
        <TriangleAlert className="size-3" aria-hidden="true" />
        {severe
          ? "Insufficient data — this Evidence supports no conclusion"
          : "Partial data — treat these values as a lower bound"}
      </p>
      <dl className="mt-1 flex flex-col gap-0.5">
        <div className="flex gap-2">
          <dt className="min-w-20 shrink-0 text-muted-foreground">Cause</dt>
          <dd className="font-mono">{reason ?? "not reported"}</dd>
        </div>
        {detail && (
          <div className="flex gap-2">
            <dt className="min-w-20 shrink-0 text-muted-foreground">Detail</dt>
            <dd>{detail}</dd>
          </div>
        )}
        {observed.length > 0 && (
          <div className="flex gap-2">
            <dt className="min-w-20 shrink-0 text-muted-foreground">Counts</dt>
            <dd className="tabular flex flex-wrap gap-x-3">
              {observed.map((entry) => (
                <span key={entry.label}>
                  {entry.label}: <strong>{entry.value}</strong>
                </span>
              ))}
            </dd>
          </div>
        )}
        {missingSeries.length > 0 && (
          <div className="flex gap-2">
            <dt className="min-w-20 shrink-0 text-muted-foreground">Missing series</dt>
            <dd className="min-w-0">
              <ul className="flex flex-col gap-0.5">
                {missingSeries.map((series) => (
                  <li key={series} className="scroll-x font-mono text-[10px]">
                    {series}
                  </li>
                ))}
              </ul>
            </dd>
          </div>
        )}
      </dl>
    </div>
  );
}

/** Raw facts and provenance stay collapsed until asked for. */
function EvidenceDetails({ item }: { item: EvidenceItem }) {
  const [open, setOpen] = React.useState(false);
  const id = React.useId();
  return (
    <div className="mt-1.5">
      <button type="button" aria-expanded={open} aria-controls={id}
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1 rounded text-[11px] text-muted-foreground hover:text-foreground">
        <ChevronRight aria-hidden="true"
          className={cn("size-3 transition-transform", open && "rotate-90")} />
        {open ? "Hide" : "Facts and provenance"}
      </button>
      {open && (
        <div id={id} className="mt-1.5 flex flex-col gap-1.5">
          <dl className="grid gap-x-6 gap-y-1 text-[11px] sm:grid-cols-2">
            <Row term="Provider" value={item.provenance.provider} />
            <Row term="Window"
              value={`${formatTimestamp(item.window.start)} → ${formatTimestamp(item.window.end)}`} />
            <Row term="Query" value={item.provenance.query} wrap />
            <Row term="Locator" value={item.provenance.locator} wrap />
            <Row term="Content hash" value={shortHash(item.provenance.content_hash)} />
            <Row term="Quality"
              value={`freshness ${item.quality.freshness} · completeness ${formatRatio(item.quality.completeness)} · confidence ${formatRatio(item.quality.confidence)}`} />
          </dl>
          {item.redactions.length > 0 && (
            <div className="rounded border border-border bg-surface-sunken px-2 py-1.5">
              <p className="flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground">
                <ShieldAlert className="size-3" aria-hidden="true" />
                Redacted before storage — values are not retained and cannot be shown
              </p>
              <ul className="mt-1 flex flex-col gap-0.5">
                {item.redactions.map((path) => (
                  <li key={path} className="scroll-x font-mono text-[10px]">{path}</li>
                ))}
              </ul>
            </div>
          )}
          <JsonViewer value={item.facts} label="facts" />
        </div>
      )}
    </div>
  );
}

function Row({ term, value, wrap = false }: { term: string; value: string; wrap?: boolean }) {
  return (
    <div className="flex items-baseline gap-2">
      <dt className="min-w-24 shrink-0 text-muted-foreground">{term}</dt>
      <dd className={cn("font-mono", wrap ? "break-all" : "truncate")}>{value}</dd>
    </div>
  );
}

function EvidenceCard({
  item, index, focused, focusRef,
}: {
  item: EvidenceItem;
  index: RelevanceIndex;
  focused: boolean;
  /** Set only on the deep-linked card; scrolls it into view once mounted. */
  focusRef?: (node: HTMLDivElement | null) => void;
}) {
  return (
    <Card ref={focusRef} id={`evidence-${item.evidence_id}`}
      className={cn("scroll-mt-32", focused && "ring-2 ring-ring")}>
      <CardContent className="flex flex-col gap-1.5 px-3 py-2.5">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="font-mono text-xs font-medium">{item.evidence_id}</span>
          <Badge tone="outline">{item.source}</Badge>
          <Badge tone="outline">{item.kind}</Badge>
          <ResultBadge item={item} />
          <RelevanceBadges id={item.evidence_id} index={index} />
          {item.redactions.length > 0 && (
            <Badge tone="neutral" title="Fields removed before storage">
              <EyeOff aria-hidden="true" />
              {item.redactions.length} redacted
            </Badge>
          )}
          <span className="tabular ml-auto text-[11px] text-muted-foreground">
            {formatTimestamp(item.observed_at)}
          </span>
        </div>
        <div className="flex flex-wrap items-baseline gap-1.5">
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            {entityKind(item.subject)}
          </span>
          <span className="font-mono text-xs">{item.subject.name}</span>
        </div>
        <p className="text-xs leading-relaxed">{item.summary}</p>
        {isDegraded(item) && <InsufficientDataPanel item={item} />}
        <EvidenceDetails item={item} />
      </CardContent>
    </Card>
  );
}

function EvidenceTable({
  items, index, focusedEvidenceId,
}: {
  items: EvidenceItem[]; index: RelevanceIndex; focusedEvidenceId: string | null;
}) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead scope="col">Evidence ID</TableHead>
          <TableHead scope="col">Source</TableHead>
          <TableHead scope="col">Kind</TableHead>
          <TableHead scope="col">Subject</TableHead>
          <TableHead scope="col">Observed</TableHead>
          <TableHead scope="col">Result</TableHead>
          <TableHead scope="col">Relevance</TableHead>
          <TableHead scope="col">Summary</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((item) => (
          <TableRow key={item.evidence_id} id={`evidence-${item.evidence_id}`}
            className={cn("scroll-mt-32", item.evidence_id === focusedEvidenceId && "bg-accent")}>
            <TableCell className="font-mono text-[11px]">{item.evidence_id}</TableCell>
            <TableCell className="text-[11px]">{item.source}</TableCell>
            <TableCell className="text-[11px]">{item.kind}</TableCell>
            <TableCell className="max-w-52 truncate font-mono text-[11px]">
              {item.subject.name}
            </TableCell>
            <TableCell className="tabular text-[11px] text-muted-foreground">
              {formatTimestamp(item.observed_at)}
            </TableCell>
            <TableCell><ResultBadge item={item} /></TableCell>
            <TableCell className="whitespace-nowrap">
              <RelevanceBadges id={item.evidence_id} index={index} />
            </TableCell>
            <TableCell className="max-w-96 truncate text-[11px]">{item.summary}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
