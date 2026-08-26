import { afterEach, describe, expect, it, vi } from "vitest";
import { allowlistExternalUrl, apiBaseUrl, deepLinks } from "@/lib/config";
import { HttpViewerAdapter } from "@/lib/adapter/http-adapter";
import { ViewerApiError } from "@/lib/adapter/types";

describe("external link allowlist", () => {
  it("accepts http and https URLs", () => {
    expect(allowlistExternalUrl("https://grafana.example.com/d/abc")).toBe(
      "https://grafana.example.com/d/abc",
    );
    expect(allowlistExternalUrl("http://localhost:3000")).toBe("http://localhost:3000/");
  });

  it("rejects script and data schemes", () => {
    expect(allowlistExternalUrl("javascript:alert(1)")).toBeNull();
    expect(allowlistExternalUrl("data:text/html,<script>alert(1)</script>")).toBeNull();
    expect(allowlistExternalUrl("file:///etc/passwd")).toBeNull();
  });

  it("rejects values that are not absolute URLs", () => {
    expect(allowlistExternalUrl("/incidents")).toBeNull();
    expect(allowlistExternalUrl("grafana.example.com")).toBeNull();
    expect(allowlistExternalUrl("")).toBeNull();
    expect(allowlistExternalUrl(undefined)).toBeNull();
  });

  it("renders no deep links when no URL is configured", () => {
    // Nothing is hardcoded: with the variables unset the header shows no links.
    expect(deepLinks()).toEqual([]);
  });
});

describe("Viewer API base URL", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("accepts only the same-origin BFF path", () => {
    vi.stubEnv("NEXT_PUBLIC_VIEWER_API_BASE_URL", "/api/viewer/");
    expect(apiBaseUrl()).toBe("/api/viewer");

    vi.stubEnv("NEXT_PUBLIC_VIEWER_API_BASE_URL", "https://viewer.example.com");
    expect(apiBaseUrl()).toBeNull();

    vi.stubEnv("NEXT_PUBLIC_VIEWER_API_BASE_URL", "/api/other");
    expect(apiBaseUrl()).toBeNull();
  });
});

describe("HttpViewerAdapter is read-only", () => {
  afterEach(() => vi.unstubAllGlobals());

  function stubFetch(payload: unknown) {
    // A Response body can only be read once, so each call gets a fresh one.
    const fetchMock = vi.fn(
      async (_input: string | URL | Request, _init?: RequestInit) =>
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("issues GET for every operation", async () => {
    const fetchMock = stubFetch({ schema_version: "1.0.0", items: [], next_cursor: null });
    const adapter = new HttpViewerAdapter("https://viewer.example.com/");

    await adapter.listIncidents({
      schema_version: "1.0.0",
      statuses: [],
      severities: [],
      namespace: null,
      search: null,
      limit: 25,
      cursor: null,
    });
    await adapter.getIncidentDetail("inc-checkout-0001");
    await adapter.getIncidentWorkState("inc-checkout-0001");

    expect(fetchMock).toHaveBeenCalledTimes(3);
    for (const [, init] of fetchMock.mock.calls) {
      expect(init?.method).toBe("GET");
      expect(init?.cache).toBe("no-store");
      expect(init?.body).toBeUndefined();
    }
  });

  it("builds only the query parameters the contract allows", async () => {
    const fetchMock = stubFetch({ schema_version: "1.0.0", items: [], next_cursor: null });
    const adapter = new HttpViewerAdapter("https://viewer.example.com");

    await adapter.listIncidents({
      schema_version: "1.0.0",
      statuses: ["ANALYZING", "REPORTED"],
      severities: ["critical"],
      namespace: "online-boutique",
      search: "checkout",
      limit: 50,
      cursor: "abc",
    });

    const url = new URL(String(fetchMock.mock.calls[0][0]));
    expect(url.pathname).toBe("/api/v1/incidents");
    expect(url.searchParams.getAll("status")).toEqual(["ANALYZING", "REPORTED"]);
    expect(url.searchParams.getAll("severity")).toEqual(["critical"]);
    expect(url.searchParams.get("namespace")).toBe("online-boutique");
    expect(url.searchParams.get("limit")).toBe("50");
    expect([...url.searchParams.keys()].sort()).toEqual([
      "cursor",
      "limit",
      "namespace",
      "search",
      "severity",
      "status",
      "status",
    ]);
  });

  it("carries no bearer token: the token stays in the server-side proxy", async () => {
    const fetchMock = stubFetch({ schema_version: "1.0.0", items: [], next_cursor: null });
    const adapter = new HttpViewerAdapter("/api/viewer");

    await adapter.getIncidentWorkState("inc-checkout-0001");

    const headers = fetchMock.mock.calls[0][1]?.headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
  });

  it("surfaces a 404 as a not-found error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ error: { code: "INCIDENT_NOT_FOUND", message: "Incident was not found" } }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    const adapter = new HttpViewerAdapter("https://viewer.example.com");

    await expect(adapter.getIncidentDetail("inc-missing-0000")).rejects.toMatchObject({
      kind: "not-found",
      statusCode: 404,
    });
  });

  it("reports an unreachable API as a network error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const adapter = new HttpViewerAdapter("https://viewer.example.com");

    await expect(adapter.listIncidents({
      schema_version: "1.0.0",
      statuses: [],
      severities: [],
      namespace: null,
      search: null,
      limit: 25,
      cursor: null,
    })).rejects.toBeInstanceOf(ViewerApiError);
  });
});
