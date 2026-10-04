import { type NextRequest, NextResponse } from "next/server";

import { READ_ONLY } from "@/lib/config";
import { ENGINE_URL } from "@/lib/engine";

/**
 * The dashboard's one write path: record an approval decision.
 *
 * Fails closed three ways. A read-only build refuses outright. The operator's identity must come
 * from the SSO proxy in front of the dashboard (`x-forwarded-user`), never from the request body --
 * a name a browser sends is a claim, not an identity. And the bearer token the engine requires is
 * held here, server-side, so no browser ever sees it.
 */
export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  if (READ_ONLY) {
    return NextResponse.json({ detail: "this dashboard is read-only" }, { status: 403 });
  }

  const operator = request.headers.get("x-forwarded-user");
  if (!operator) {
    return NextResponse.json(
      { detail: "no authenticated operator: put the dashboard behind your SSO proxy" },
      { status: 401 },
    );
  }

  const token = process.env.HEALOPS_APPROVALS_API_TOKEN;
  if (!token) {
    return NextResponse.json(
      { detail: "approvals from the dashboard are not configured; decide from the Slack card" },
      { status: 503 },
    );
  }

  const body = (await request.json().catch(() => null)) as { decision?: unknown } | null;
  const decision = body?.decision;
  if (decision !== "approve" && decision !== "reject") {
    return NextResponse.json({ detail: "decision must be approve or reject" }, { status: 400 });
  }

  const { id } = await params;
  try {
    const response = await fetch(`${ENGINE_URL}/api/approvals/${encodeURIComponent(id)}`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
      body: JSON.stringify({ decision, decided_by: operator }),
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    return new NextResponse(await response.text(), {
      status: response.status,
      headers: { "content-type": "application/json" },
    });
  } catch {
    return NextResponse.json({ detail: "engine unreachable; the decision was not recorded" }, { status: 502 });
  }
}
