import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePollingResource } from "@/lib/hooks/use-polling-resource";

/**
 * Advances fake timers and drains the microtask queue.
 *
 * Testing Library's `waitFor` only knows how to drive jest's fake timers, so
 * these tests step the clock explicitly instead.
 */
async function flush(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("usePollingResource", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("does not start a second request while one is in flight", async () => {
    // A request that never settles: every subsequent poll tick must be dropped.
    const fetcher = vi.fn(() => new Promise<string>(() => {}));

    renderHook(() =>
      usePollingResource({ fetcher, fetchKey: "incidents", intervalMs: 100 }),
    );

    await flush(1000);

    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("polls again once the previous request settles", async () => {
    const fetcher = vi.fn().mockResolvedValue("payload");

    const { result } = renderHook(() =>
      usePollingResource({ fetcher, fetchKey: "incidents", intervalMs: 100 }),
    );

    await flush();
    expect(result.current.data).toBe("payload");

    await flush(250);
    expect(fetcher.mock.calls.length).toBeGreaterThan(1);
  });

  it("keeps the last successful payload when a later request fails", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce("first")
      .mockRejectedValue(new Error("Viewer API is unreachable"));

    const { result } = renderHook(() =>
      usePollingResource({ fetcher, fetchKey: "incidents", intervalMs: 100 }),
    );

    await flush();
    expect(result.current.data).toBe("first");
    const firstSuccessAt = result.current.lastUpdatedAt;

    await flush(150);

    // The failure is reported, but the operator does not lose what they were reading.
    expect(result.current.error?.message).toBe("Viewer API is unreachable");
    expect(result.current.data).toBe("first");
    expect(result.current.isStale).toBe(true);
    expect(result.current.lastUpdatedAt).toBe(firstSuccessAt);
  });

  it("clears the error once a later request succeeds", async () => {
    const fetcher = vi
      .fn()
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValue("recovered");

    const { result } = renderHook(() =>
      usePollingResource({ fetcher, fetchKey: "incidents", intervalMs: 100 }),
    );

    await flush();
    expect(result.current.error).not.toBeNull();

    await flush(150);
    expect(result.current.data).toBe("recovered");
    expect(result.current.error).toBeNull();
    expect(result.current.isStale).toBe(false);
  });

  it("aborts the in-flight request when the fetch key changes", async () => {
    const signals: AbortSignal[] = [];
    const fetcher = vi.fn((signal: AbortSignal) => {
      signals.push(signal);
      return new Promise<string>(() => {});
    });

    const { rerender } = renderHook(
      ({ fetchKey }: { fetchKey: string }) =>
        usePollingResource({ fetcher, fetchKey, intervalMs: 0 }),
      { initialProps: { fetchKey: "page-1" } },
    );

    await flush();
    expect(signals[0].aborted).toBe(false);

    rerender({ fetchKey: "page-2" });
    await flush();

    // The superseded request is cancelled so a late response cannot overwrite
    // the newer page.
    expect(signals[0].aborted).toBe(true);
    expect(signals).toHaveLength(2);
  });

  it("stops polling when the interval is zero but keeps the loaded data", async () => {
    const fetcher = vi.fn().mockResolvedValue("payload");

    const { result } = renderHook(() =>
      usePollingResource({ fetcher, fetchKey: "incidents", intervalMs: 0 }),
    );

    await flush();
    expect(result.current.data).toBe("payload");

    await flush(5000);

    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(result.current.data).toBe("payload");
  });
});
