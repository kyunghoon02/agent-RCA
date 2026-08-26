"use client";

import * as React from "react";
import { Search, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  INCIDENT_STATUSES,
  SEVERITIES,
  type IncidentStatus,
  type Severity,
} from "@/lib/types";

export interface FilterState {
  statuses: IncidentStatus[];
  severities: Severity[];
  namespace: string;
  search: string;
}

export const EMPTY_FILTERS: FilterState = {
  statuses: [],
  severities: [],
  namespace: "",
  search: "",
};

function toggle<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
}

/**
 * Exactly the filters the query contract accepts: status, severity, namespace
 * and a bounded search over alert name and resource identity.
 */
export function IncidentFilters({
  value,
  onChange,
}: {
  value: FilterState;
  onChange: (next: FilterState) => void;
}) {
  const [searchDraft, setSearchDraft] = React.useState(value.search);
  const [namespaceDraft, setNamespaceDraft] = React.useState(value.namespace);

  // Keep drafts aligned when filters are reset or applied from elsewhere.
  React.useEffect(() => setSearchDraft(value.search), [value.search]);
  React.useEffect(() => setNamespaceDraft(value.namespace), [value.namespace]);

  const activeCount =
    value.statuses.length +
    value.severities.length +
    (value.namespace ? 1 : 0) +
    (value.search ? 1 : 0);

  return (
    <form
      className="flex flex-wrap items-end gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        onChange({
          ...value,
          search: searchDraft.trim().slice(0, 100),
          namespace: namespaceDraft.trim(),
        });
      }}
    >
      <div className="flex flex-col gap-1">
        <Label htmlFor="incident-search">Alert or resource</Label>
        <div className="relative">
          <Search
            className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            id="incident-search"
            value={searchDraft}
            maxLength={100}
            placeholder="checkoutservice"
            onChange={(event) => setSearchDraft(event.target.value)}
            className="w-56 pl-7"
          />
        </div>
      </div>

      <div className="flex flex-col gap-1">
        <Label htmlFor="incident-namespace">Namespace</Label>
        <Input
          id="incident-namespace"
          value={namespaceDraft}
          maxLength={253}
          placeholder="online-boutique"
          onChange={(event) => setNamespaceDraft(event.target.value)}
          className="w-48"
        />
      </div>

      <FilterPopover
        label="Status"
        selected={value.statuses}
        options={INCIDENT_STATUSES}
        onToggle={(status) => onChange({ ...value, statuses: toggle(value.statuses, status) })}
      />

      <FilterPopover
        label="Severity"
        selected={value.severities}
        options={SEVERITIES}
        onToggle={(severity) =>
          onChange({ ...value, severities: toggle(value.severities, severity) })
        }
      />

      <Button type="submit" size="sm" variant="secondary">
        Apply
      </Button>

      {activeCount > 0 && (
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={() => onChange(EMPTY_FILTERS)}
        >
          <X className="size-3.5" aria-hidden="true" />
          Clear {activeCount}
        </Button>
      )}
    </form>
  );
}

function FilterPopover<T extends string>({
  label,
  options,
  selected,
  onToggle,
}: {
  label: string;
  options: readonly T[];
  selected: T[];
  onToggle: (value: T) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <Label>{label}</Label>
      <Popover>
        <PopoverTrigger asChild>
          <Button type="button" variant="outline" size="sm" className="h-8 min-w-28 justify-between">
            {selected.length === 0 ? `All ${label.toLowerCase()}` : `${selected.length} selected`}
            {selected.length > 0 && <Badge tone="neutral">{selected.length}</Badge>}
          </Button>
        </PopoverTrigger>
        <PopoverContent>
          <fieldset className="flex flex-col gap-0.5">
            <legend className="sr-only">Filter by {label.toLowerCase()}</legend>
            {options.map((option) => {
              const id = `${label}-${option}`;
              return (
                <label
                  key={option}
                  htmlFor={id}
                  className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-xs hover:bg-accent"
                >
                  <Checkbox
                    id={id}
                    checked={selected.includes(option)}
                    onCheckedChange={() => onToggle(option)}
                  />
                  {option}
                </label>
              );
            })}
          </fieldset>
        </PopoverContent>
      </Popover>
    </div>
  );
}
