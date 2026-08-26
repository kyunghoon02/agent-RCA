import { NextResponse } from "next/server";

/**
 * Same-origin read-only proxy for the Viewer query API.
 *
 * contracts/viewer.md requires a bearer token on every Viewer route and states
 * that the token must not live in a browser-visible variable. This route keeps
 * it server-side: the browser calls the same origin, and only this handler ever
 * sees `VIEWER_API_TOKEN`.
 *
 * Only GET is exported. Every other method returns 405 from the framework, so
 * no mutation can be proxied even by accident.
 */

export const dynamic = "force-dynamic";

const INCIDENTS_PATH = "api/v1/incidents";

/** Accept the collection itself or a child Incident path, never a prefix lookalike. */
function isAllowedViewerPath(path: string): boolean {
  return path === INCIDENTS_PATH || path.startsWith(`${INCIDENTS_PATH}/`);
}

function viewerOrigin(raw: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    return null;
  }
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash
  ) {
    return null;
  }
  return parsed.origin;
}

function jsonError(status: number, code: string, message: string) {
  return NextResponse.json(
    { error: { code, message } },
    { status, headers: { "Cache-Control": "no-store" } },
  );
}

export async function GET(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
) {
  const origin = process.env.VIEWER_API_ORIGIN;
  const token = process.env.VIEWER_API_TOKEN;

  if (!origin || !token || token.length < 16) {
    return jsonError(
      503,
      "VIEWER_API_NOT_CONFIGURED",
      "Set VIEWER_API_ORIGIN and VIEWER_API_TOKEN to proxy the Viewer query API.",
    );
  }

  const upstreamOrigin = viewerOrigin(origin);
  if (!upstreamOrigin) {
    return jsonError(
      503,
      "VIEWER_API_NOT_CONFIGURED",
      "VIEWER_API_ORIGIN must be an http(s) origin without credentials or a path.",
    );
  }

  const { path } = await context.params;
  const upstreamPath = path.map(encodeURIComponent).join("/");
  if (!isAllowedViewerPath(upstreamPath)) {
    return jsonError(404, "ROUTE_NOT_FOUND", "route not found");
  }

  const search = new URL(request.url).search;
  let upstream: Response;
  try {
    upstream = await fetch(`${upstreamOrigin}/${upstreamPath}${search}`, {
      method: "GET",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${token}`,
      },
    });
  } catch {
    return jsonError(502, "VIEWER_API_UNREACHABLE", "Viewer query API is unreachable.");
  }

  const body = await upstream.text();
  return new NextResponse(body, {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("Content-Type") ?? "application/json",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "no-referrer",
    },
  });
}
