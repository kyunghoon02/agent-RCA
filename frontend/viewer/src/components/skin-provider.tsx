"use client";

import * as React from "react";

export const SKINS = ["console", "blueprint"] as const;
export type Skin = (typeof SKINS)[number];

/** Blueprint is the default look; the Console palette stays one click away. */
export const DEFAULT_SKIN: Skin = "blueprint";

export const SKIN_LABELS: Record<Skin, string> = {
  console: "Console",
  blueprint: "Blueprint",
};

export const SKIN_STORAGE_KEY = "agent-rca-viewer-skin";

function isSkin(value: unknown): value is Skin {
  return typeof value === "string" && (SKINS as readonly string[]).includes(value);
}

const SkinContext = React.createContext<{
  skin: Skin;
  setSkin: (skin: Skin) => void;
}>({ skin: DEFAULT_SKIN, setSkin: () => {} });

/**
 * Applies the chosen skin as `data-skin` on the document element.
 *
 * A skin only swaps CSS custom properties, so no component re-renders
 * differently and no layout or behaviour depends on which skin is active.
 */
export function SkinProvider({ children }: { children: React.ReactNode }) {
  const [skin, setSkinState] = React.useState<Skin>(DEFAULT_SKIN);

  // The inline script in the layout already applied the stored skin before
  // paint; this reads it back so React state matches the DOM.
  React.useEffect(() => {
    const stored = window.localStorage.getItem(SKIN_STORAGE_KEY);
    setSkinState(isSkin(stored) ? stored : DEFAULT_SKIN);
  }, []);

  const setSkin = React.useCallback((next: Skin) => {
    setSkinState(next);
    document.documentElement.setAttribute("data-skin", next);
    try {
      window.localStorage.setItem(SKIN_STORAGE_KEY, next);
    } catch {
      // A blocked storage write must not stop the skin from applying.
    }
  }, []);

  const value = React.useMemo(() => ({ skin, setSkin }), [skin, setSkin]);
  return <SkinContext.Provider value={value}>{children}</SkinContext.Provider>;
}

export function useSkin() {
  return React.useContext(SkinContext);
}

/**
 * Runs before first paint so a stored skin does not flash the default one.
 * Kept tiny and dependency-free because it is inlined into the document head.
 */
export const SKIN_BOOTSTRAP_SCRIPT = `(function(){var d=${JSON.stringify(DEFAULT_SKIN)};try{var s=localStorage.getItem(${JSON.stringify(
  SKIN_STORAGE_KEY,
)});if(s==="console"||s==="blueprint"){d=s;}}catch(e){}document.documentElement.setAttribute("data-skin",d);})();`;
