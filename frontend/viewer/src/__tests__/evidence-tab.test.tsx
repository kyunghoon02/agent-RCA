import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { EvidenceTab } from "@/components/incident-detail/tabs/evidence-tab";
import { FixtureViewerAdapter } from "@/lib/adapter/fixture-adapter";
import { evidenceCompleteness } from "@/lib/evidence";
import type { EvidenceItem } from "@/lib/types";

const adapter = new FixtureViewerAdapter();

async function evidenceFor(incidentId: string): Promise<EvidenceItem[]> {
  return (await adapter.getIncidentDetail(incidentId)).evidence;
}

/**
 * Renders the tab in Raw cards view.
 *
 * The tab now defaults to the grouped Investigation view, so per-item detail
 * lives behind a group. These assertions are about a single item's rendering,
 * so they open the flat view explicitly.
 */
async function renderCards(incidentId: string) {
  const detail = await adapter.getIncidentDetail(incidentId);
  const result = render(
    <EvidenceTab
      evidence={detail.evidence}
      contexts={detail.contexts}
      reports={detail.reports}
      focusedEvidenceId={null}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /Raw cards/ }));
  return result;
}

describe("EvidenceTab insufficient data", () => {
  it("labels an INSUFFICIENT_DATA item and states the cause", async () => {
    await renderCards("inc-rediscart-0004");

    expect(screen.getByText("INSUFFICIENT_DATA")).toBeInTheDocument();
    expect(
      screen.getByText("Insufficient data — this Evidence supports no conclusion"),
    ).toBeInTheDocument();
    expect(screen.getByText("PROMETHEUS_QUERY_TIMEOUT")).toBeInTheDocument();
    expect(
      screen.getByText(/The range query exceeded its 10s deadline after 2 attempts/),
    ).toBeInTheDocument();
  });

  it("names the series that were expected but never returned", async () => {
    await renderCards("inc-rediscart-0004");

    expect(screen.getByText("Missing series")).toBeInTheDocument();
    expect(
      screen.getByText(
        'redis_memory_used_bytes{namespace="online-boutique",pod="redis-cart-7f8d9c6b5-lk3jd"}',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/Expected samples:/)).toBeInTheDocument();
    expect(screen.getByText(/Returned samples:/)).toBeInTheDocument();
  });

  it("marks a partial item as a lower bound rather than a full observation", async () => {
    await renderCards("inc-rediscart-0004");

    expect(
      screen.getByText("Partial data — treat these values as a lower bound"),
    ).toBeInTheDocument();
    expect(screen.getByText("LOG_BACKEND_PARTIAL_RESPONSE")).toBeInTheDocument();
  });

  it("lists redacted field paths without rendering any value", async () => {
    await renderCards("inc-rediscart-0004");
    // Provenance and redaction paths are progressive disclosure.
    for (const toggle of screen.getAllByRole("button", { name: /Facts and provenance/ })) {
      fireEvent.click(toggle);
    }

    expect(
      screen.getByText(
        /Redacted before storage — values are not retained and cannot be shown/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("line.auth_token")).toBeInTheDocument();
  });

  it("keeps facts collapsed until the operator expands them", async () => {
    await renderCards("inc-rediscart-0004");

    const toggles = screen.getAllByRole("button", { name: /facts/i });
    expect(toggles[0]).toHaveAttribute("aria-expanded", "false");
  });

  it("explains why there is no Evidence rather than showing a blank pane", () => {
    render(
      <EvidenceTab evidence={[]} contexts={[]} reports={[]} focusedEvidenceId={null} />,
    );
    expect(
      screen.getByText("No Evidence was stored for this Incident"),
    ).toBeInTheDocument();
  });
});

describe("evidenceCompleteness", () => {
  it("classifies fixture Evidence by its recorded status and completeness", async () => {
    const evidence = await evidenceFor("inc-rediscart-0004");
    const byId = Object.fromEntries(
      evidence.map((item) => [item.evidence_id, evidenceCompleteness(item)]),
    );
    expect(byId["ev-redis-mem-01"]).toBe("insufficient");
    expect(byId["ev-redis-log-01"]).toBe("partial");
    expect(byId["ev-redis-evt-01"]).toBe("complete");
  });
});


describe("Evidence deep-link scrolling", () => {
  /**
   * Deep links arrive while another view is active, so the target card does not
   * exist when the focus effect runs. These assert the card is actually
   * scrolled to once it mounts.
   */
  async function renderFocused(incidentId: string, evidenceId: string) {
    const detail = await adapter.getIncidentDetail(incidentId);
    const scrollIntoView = vi.fn();
    // jsdom does not implement scrollIntoView.
    Element.prototype.scrollIntoView = scrollIntoView;
    // requestAnimationFrame is used to let layout settle before scrolling.
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb) => {
      cb(0);
      return 0;
    });
    const view = render(
      <EvidenceTab
        evidence={detail.evidence}
        contexts={detail.contexts}
        reports={detail.reports}
        focusedEvidenceId={evidenceId}
      />,
    );
    return { ...view, scrollIntoView, detail };
  }

  it("switches to Raw cards and scrolls the referenced Evidence into view", async () => {
    const { scrollIntoView } = await renderFocused(
      "inc-checkout-0001",
      "ev-checkout-dep-01",
    );

    expect(scrollIntoView).toHaveBeenCalled();
    const target = document.getElementById("evidence-ev-checkout-dep-01");
    expect(target).not.toBeNull();
    // The scrolled element must be the deep-linked card itself.
    expect(scrollIntoView.mock.instances[0]).toBe(target);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "center" });
  });

  it("renders the target in Raw cards view, not behind a collapsed group", async () => {
    await renderFocused("inc-checkout-0001", "ev-checkout-dep-01");
    // Raw cards is the active view, so the full record is on screen.
    expect(
      screen.getByRole("button", { name: /Raw cards/ }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  it("keeps the visual highlight on the focused card", async () => {
    await renderFocused("inc-checkout-0001", "ev-checkout-dep-01");
    const target = document.getElementById("evidence-ev-checkout-dep-01");
    expect(target?.className).toContain("ring-2");
  });

  it("clears filters that could otherwise hide the target", async () => {
    const { detail } = await renderFocused("inc-checkout-0001", "ev-checkout-dep-01");
    // Every Evidence item is listed, so no filter is suppressing the target.
    expect(
      screen.getByText(new RegExp(`Showing ${detail.evidence.length} of ${detail.evidence.length}`)),
    ).toBeInTheDocument();
  });

  it("does not scroll when no Evidence is deep-linked", async () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    const detail = await adapter.getIncidentDetail("inc-checkout-0001");
    render(
      <EvidenceTab
        evidence={detail.evidence}
        contexts={detail.contexts}
        reports={detail.reports}
        focusedEvidenceId={null}
      />,
    );
    expect(scrollIntoView).not.toHaveBeenCalled();
  });
});
