import { afterEach, describe, expect, it, vi } from "vitest";
import { GET } from "@/app/api/viewer/[...path]/route";

describe("Viewer BFF route", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  function context(path: string[]) {
    return { params: Promise.resolve({ path }) };
  }

  it("attaches the server token and forwards only an authenticated GET", async () => {
    vi.stubEnv("VIEWER_API_ORIGIN", "http://incident-viewer:8080");
    vi.stubEnv("VIEWER_API_TOKEN", "viewer-server-token-123456");
    const upstream = vi.fn().mockResolvedValue(
      Response.json({ schema_version: "1.0.0", items: [], next_cursor: null }),
    );
    vi.stubGlobal("fetch", upstream);

    const response = await GET(
      new Request("http://localhost/api/viewer/api/v1/incidents?limit=1"),
      context(["api", "v1", "incidents"]),
    );

    expect(response.status).toBe(200);
    expect(upstream).toHaveBeenCalledOnce();
    const [url, init] = upstream.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://incident-viewer:8080/api/v1/incidents?limit=1");
    expect(init.method).toBe("GET");
    expect(init.cache).toBe("no-store");
    expect((init.headers as Record<string, string>).Authorization).toBe(
      "Bearer viewer-server-token-123456",
    );
  });

  it("rejects lookalike paths and unsafe upstream origins", async () => {
    vi.stubEnv("VIEWER_API_ORIGIN", "http://incident-viewer:8080");
    vi.stubEnv("VIEWER_API_TOKEN", "viewer-server-token-123456");

    const lookalike = await GET(
      new Request("http://localhost/api/viewer/api/v1/incidents-export"),
      context(["api", "v1", "incidents-export"]),
    );
    expect(lookalike.status).toBe(404);

    vi.stubEnv("VIEWER_API_ORIGIN", "http://user:password@incident-viewer:8080/path");
    const unsafe = await GET(
      new Request("http://localhost/api/viewer/api/v1/incidents"),
      context(["api", "v1", "incidents"]),
    );
    expect(unsafe.status).toBe(503);
  });

  it("fails closed when the server token is missing or too short", async () => {
    vi.stubEnv("VIEWER_API_ORIGIN", "http://incident-viewer:8080");
    vi.stubEnv("VIEWER_API_TOKEN", "short");

    const response = await GET(
      new Request("http://localhost/api/viewer/api/v1/incidents"),
      context(["api", "v1", "incidents"]),
    );
    expect(response.status).toBe(503);
  });
});
