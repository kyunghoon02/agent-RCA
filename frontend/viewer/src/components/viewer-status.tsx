"use client";

import * as React from "react";
import type { AdapterMode } from "@/lib/adapter";

export type ConnectionState = "demo" | "live" | "disconnected" | "connecting";

export interface ViewerStatus {
  connection: ConnectionState;
  /** True when the screen is showing data from before the current error. */
  stale: boolean;
  lastUpdatedAt: number | null;
}

const DEFAULT_STATUS: ViewerStatus = {
  connection: "connecting",
  stale: false,
  lastUpdatedAt: null,
};

const ViewerStatusContext = React.createContext<{
  status: ViewerStatus;
  publish: (status: ViewerStatus) => void;
}>({ status: DEFAULT_STATUS, publish: () => {} });

export function ViewerStatusProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = React.useState<ViewerStatus>(DEFAULT_STATUS);
  const publish = React.useCallback((next: ViewerStatus) => {
    setStatus((current) =>
      current.connection === next.connection &&
      current.stale === next.stale &&
      current.lastUpdatedAt === next.lastUpdatedAt
        ? current
        : next,
    );
  }, []);
  const value = React.useMemo(() => ({ status, publish }), [status, publish]);
  return (
    <ViewerStatusContext.Provider value={value}>{children}</ViewerStatusContext.Provider>
  );
}

export function useViewerStatus(): ViewerStatus {
  return React.useContext(ViewerStatusContext).status;
}

/**
 * Publishes the active screen's fetch state to the global header.
 *
 * The header reports what the screen actually observed — a fixture adapter is
 * always "Demo Data", and a live adapter that just failed is "Disconnected"
 * even while stale rows remain on screen.
 */
export function usePublishViewerStatus(input: {
  mode: AdapterMode;
  error: Error | null;
  isStale: boolean;
  lastUpdatedAt: number | null;
}): void {
  const { publish } = React.useContext(ViewerStatusContext);
  const { mode, error, isStale, lastUpdatedAt } = input;

  React.useEffect(() => {
    const connection: ConnectionState =
      mode === "fixture"
        ? "demo"
        : error
          ? "disconnected"
          : lastUpdatedAt === null
            ? "connecting"
            : "live";
    publish({ connection, stale: isStale, lastUpdatedAt });
  }, [publish, mode, error, isStale, lastUpdatedAt]);
}
