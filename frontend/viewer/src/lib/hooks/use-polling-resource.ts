"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { isAbortError } from "@/lib/adapter";

export interface PollingResource<T> {
  /** Last successful payload. Survives later failures. */
  data: T | null;
  /** Error from the most recent attempt, or null once an attempt succeeds. */
  error: Error | null;
  /** True only before the first payload arrives. */
  isLoading: boolean;
  /** True while a request is in flight, including background polls. */
  isFetching: boolean;
  /** True when `data` is held over from before the current error. */
  isStale: boolean;
  /** Epoch millis of the last success, for the header's refresh clock. */
  lastUpdatedAt: number | null;
  refresh: () => void;
}

export interface PollingOptions<T> {
  /** Receives an AbortSignal; must forward it so stale requests are cancelled. */
  fetcher: (signal: AbortSignal) => Promise<T>;
  /** Re-runs the fetch whenever this changes. Serialise inputs into it. */
  fetchKey: string;
  intervalMs: number;
  /** When false, no request is issued and no timer runs. */
  enabled?: boolean;
}

type Trigger = "key" | "poll" | "manual";

/**
 * Polls one read-only resource.
 *
 * Three behaviours matter operationally and are enforced here rather than left
 * to callers:
 *
 * - Polls never overlap. A tick that lands while a request is in flight is
 *   dropped, so a slow API cannot pile up requests.
 * - Superseded requests are aborted, so a late response can never overwrite a
 *   newer one.
 * - A failure keeps the last good payload and reports it as stale. Operators
 *   lose the connection, not the incident they were reading.
 */
export function usePollingResource<T>({
  fetcher,
  fetchKey,
  intervalMs,
  enabled = true,
}: PollingOptions<T>): PollingResource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [isFetching, setIsFetching] = useState(false);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null);

  const controllerRef = useRef<AbortController | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const execute = useCallback(async (trigger: Trigger) => {
    if (controllerRef.current) {
      // A poll tick must never queue behind an in-flight request; an explicit
      // refresh or a key change supersedes it instead.
      if (trigger === "poll") return;
      controllerRef.current.abort();
    }

    const controller = new AbortController();
    controllerRef.current = controller;
    setIsFetching(true);

    try {
      const result = await fetcherRef.current(controller.signal);
      if (controller.signal.aborted) return;
      setData(result);
      setError(null);
      setLastUpdatedAt(Date.now());
    } catch (caught) {
      if (controller.signal.aborted || isAbortError(caught)) return;
      setError(caught instanceof Error ? caught : new Error(String(caught)));
    } finally {
      // Only the request that is still current may clear the in-flight flag;
      // an aborted predecessor settling later must not unblock the new one.
      if (controllerRef.current === controller) {
        controllerRef.current = null;
        setIsFetching(false);
      }
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    void execute("key");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetchKey, enabled, execute]);

  useEffect(() => {
    if (!enabled || intervalMs <= 0) return;
    const timer = setInterval(() => void execute("poll"), intervalMs);
    return () => clearInterval(timer);
  }, [enabled, intervalMs, execute, fetchKey]);

  useEffect(() => {
    return () => {
      controllerRef.current?.abort();
      controllerRef.current = null;
    };
  }, []);

  const refresh = useCallback(() => void execute("manual"), [execute]);

  return {
    data,
    error,
    isLoading: data === null && error === null,
    isFetching,
    isStale: data !== null && error !== null,
    lastUpdatedAt,
    refresh,
  };
}
