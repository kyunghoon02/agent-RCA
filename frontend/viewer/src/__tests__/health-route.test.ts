import { describe, expect, it } from "vitest";
import { GET } from "@/app/api/healthz/route";

describe("Viewer frontend health route", () => {
  it("returns a non-cacheable readiness response", async () => {
    const response = GET();

    expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    await expect(response.json()).resolves.toEqual({ status: "ok" });
  });
});
