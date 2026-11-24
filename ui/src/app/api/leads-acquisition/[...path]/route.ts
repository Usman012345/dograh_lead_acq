import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const HOP_BY_HOP_HEADERS = [
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
];

function getBackendBaseUrl(): string {
  const envUrl = process.env.LEADS_CALLER_API_URL || process.env.NEXT_PUBLIC_LEADS_CALLER_API_URL;
  if (envUrl && envUrl.trim()) {
    return envUrl.trim().replace(/\/$/, "");
  }
  return "http://127.0.0.1:8090";
}

function buildTargetUrl(request: NextRequest, subpath: string[]): string {
  const base = getBackendBaseUrl();
  const path = subpath.join("/");
  const search = request.nextUrl.search;
  return `${base}/api/${path}${search}`;
}

function createRequestHeaders(request: NextRequest): Headers {
  const headers = new Headers(request.headers);
  for (const h of HOP_BY_HOP_HEADERS) {
    headers.delete(h);
  }
  headers.delete("host");
  headers.delete("content-length");
  return headers;
}

function createResponseHeaders(response: Response): Headers {
  const headers = new Headers(response.headers);
  for (const h of HOP_BY_HOP_HEADERS) {
    headers.delete(h);
  }
  headers.delete("content-encoding");
  headers.delete("content-length");
  return headers;
}

async function handleProxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const { path } = await context.params;
  const targetUrl = buildTargetUrl(request, path);

  try {
    const isBodyAllowed = request.method !== "GET" && request.method !== "HEAD";
    const body = isBodyAllowed ? await request.arrayBuffer() : undefined;

    const response = await fetch(targetUrl, {
      method: request.method,
      headers: createRequestHeaders(request),
      body,
      cache: "no-store",
    });

    return new Response(request.method === "HEAD" ? null : response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: createResponseHeaders(response),
    });
  } catch (error) {
    const msg = error instanceof Error ? error.message : "Target server unreachable";
    return NextResponse.json(
      {
        error: `Failed to connect to Leads Acquisition backend at ${targetUrl}: ${msg}`,
        hint: "Make sure python dashboard_server.py is running on http://127.0.0.1:8090",
      },
      { status: 502 },
    );
  }
}

export const GET = handleProxy;
export const POST = handleProxy;
export const PUT = handleProxy;
export const DELETE = handleProxy;
