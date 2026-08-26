import "@testing-library/jest-dom/vitest";

// next-themes and Radix primitives probe APIs that jsdom does not implement.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

// jsdom has no layout, so charts measure 0x0 and warn. This stub reports a
// fixed box, which is what a real viewport would give them.
if (!window.ResizeObserver) {
  window.ResizeObserver = class {
    constructor(private readonly callback: ResizeObserverCallback) {}
    observe(target: Element) {
      const contentRect = {
        width: 800,
        height: 300,
        top: 0,
        left: 0,
        bottom: 300,
        right: 800,
        x: 0,
        y: 0,
        toJSON: () => ({}),
      } as DOMRectReadOnly;
      this.callback(
        [{ target, contentRect } as ResizeObserverEntry],
        this as unknown as ResizeObserver,
      );
    }
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}
