import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LifecycleStepper } from "@/components/incident-detail/lifecycle-stepper";
import { deriveLifecycle } from "@/lib/lifecycle";

function renderFor(...args: Parameters<typeof deriveLifecycle>) {
  return render(<LifecycleStepper steps={deriveLifecycle(...args)} />);
}

describe("LifecycleStepper", () => {
  it("renders all five lifecycle steps in order", () => {
    renderFor("COLLECTING");
    const list = screen.getByRole("list", { name: /incident lifecycle/i });
    const labels = within(list)
      .getAllByRole("listitem")
      .map((item) => item.getAttribute("data-status"));
    expect(labels).toEqual([
      "RECEIVED",
      "COLLECTING",
      "LOCALIZING",
      "ANALYZING",
      "REPORTED",
    ]);
  });

  it("marks the current step and counts completed steps", () => {
    renderFor("ANALYZING");
    expect(screen.getByText("3 of 5 steps completed")).toBeInTheDocument();

    const current = screen.getByRole("list").querySelector('[data-status="ANALYZING"]');
    expect(current).toHaveAttribute("data-state", "current");
  });

  it("distinguishes each state with text, not colour alone", () => {
    renderFor("ANALYZING");
    // Every step contributes a screen-reader state word alongside its colour.
    expect(screen.getAllByText("completed")).toHaveLength(3);
    expect(screen.getByText("in progress")).toBeInTheDocument();
    expect(screen.getAllByText("not started")).toHaveLength(1);
  });

  it("shows a failed Incident stopping at the step it reached", () => {
    renderFor("FAILED", ["RECEIVED", "COLLECTING"]);
    const list = screen.getByRole("list");
    expect(list.querySelector('[data-status="COLLECTING"]')).toHaveAttribute(
      "data-state",
      "failed",
    );
    expect(list.querySelector('[data-status="LOCALIZING"]')).toHaveAttribute(
      "data-state",
      "not-reached",
    );
    expect(screen.getByText("failed at this step")).toBeInTheDocument();
    expect(screen.getAllByText("never reached")).toHaveLength(3);
  });

  it("never renders a pending step before a completed one", () => {
    renderFor("REPORTED");
    const states = within(screen.getByRole("list"))
      .getAllByRole("listitem")
      .map((item) => item.getAttribute("data-state"));
    expect(states).toEqual(Array(5).fill("complete"));
  });
});
