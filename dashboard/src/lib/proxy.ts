import "server-only";

import { NextResponse } from "next/server";

import { ENGINE_URL } from "./engine";

/**
 * Forward a GET to the engine for the browser's polling components. Errors become a 502 with a
 * reason, never a hanging request: the live feed keeps showing what it had and says it is stale.
 */
export async function proxyGet(path: string): Promise<NextResponse> {
  try {
    const response = await fetch(`${ENGINE_URL}${path}`, {
      headers: { accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    const body = await response.text();
    return new NextResponse(body, {
      status: response.status,
      headers: { "content-type": response.headers.get("content-type") ?? "application/json" },
    });
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    return NextResponse.json({ detail: `engine unreachable: ${reason}` }, { status: 502 });
  }
}
