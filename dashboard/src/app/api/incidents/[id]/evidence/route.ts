import { proxyGet } from "@/lib/proxy";

/** The content-addressed bundle and its replay -- read-only, so it exists in the public build too. */
export async function GET(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxyGet(`/api/incidents/${encodeURIComponent(id)}/evidence`);
}
