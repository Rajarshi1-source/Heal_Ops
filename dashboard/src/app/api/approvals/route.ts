import { proxyGet } from "@/lib/proxy";

export async function GET() {
  return proxyGet("/api/approvals?status_filter=pending");
}
