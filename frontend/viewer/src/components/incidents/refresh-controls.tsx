"use client";

import { Pause, Play, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { POLL_INTERVAL_OPTIONS } from "@/lib/config";
import { cn } from "@/lib/utils";

/** Polling is operator-controlled: it can be paused, retimed, or run manually. */
export function RefreshControls({
  polling,
  onPollingChange,
  intervalMs,
  onIntervalChange,
  onRefresh,
  isFetching,
}: {
  polling: boolean;
  onPollingChange: (next: boolean) => void;
  intervalMs: number;
  onIntervalChange: (next: number) => void;
  onRefresh: () => void;
  isFetching: boolean;
}) {
  return (
    <div className="flex items-center gap-1.5">
      <Button
        variant="outline"
        size="sm"
        onClick={onRefresh}
        aria-label="Refresh now"
        title="Refresh now"
      >
        <RefreshCw
          className={cn("size-3.5", isFetching && "animate-[spin_1s_linear_infinite]")}
          aria-hidden="true"
        />
        Refresh
      </Button>

      <Button
        variant="outline"
        size="sm"
        onClick={() => onPollingChange(!polling)}
        aria-pressed={polling}
      >
        {polling ? (
          <Pause className="size-3.5" aria-hidden="true" />
        ) : (
          <Play className="size-3.5" aria-hidden="true" />
        )}
        {polling ? "Pause" : "Resume"}
      </Button>

      <Select
        value={String(intervalMs)}
        onValueChange={(value) => onIntervalChange(Number(value))}
        disabled={!polling}
      >
        <SelectTrigger className="h-8 w-[5.5rem]" aria-label="Auto-refresh interval">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {POLL_INTERVAL_OPTIONS.map((option) => (
            <SelectItem key={option.value} value={String(option.value)}>
              every {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
