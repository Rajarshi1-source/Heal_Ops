import type { Metadata } from "next";

import { ApprovalPending } from "@/components/dashboard/ApprovalPending";
import { EmptyState, ErrorState, Freshness } from "@/components/common/States";
import { READ_ONLY } from "@/lib/config";
import { engine } from "@/lib/engine";
import type { PendingApproval } from "@/lib/types";

export const metadata: Metadata = { title: "Approvals" };
export const dynamic = "force-dynamic";

export default async function ApprovalsPage() {
  const { data, error, fetchedAt } = await engine<{ items: PendingApproval[] }>(
    "/api/approvals?status_filter=pending",
    { revalidate: 5 },
  );

  return (
    <div className="flex flex-col gap-6">
      <header className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Pending approvals</h1>
          <p className="mt-1 text-sm text-muted">
            The same facts as the Slack card. On expiry the request escalates — silence is never consent.
          </p>
        </div>
        <Freshness fetchedAt={fetchedAt} />
      </header>
      {!data && <ErrorState error={error ?? "unknown"} />}
      {data && data.items.length === 0 && (
        <EmptyState title="Nothing is waiting for a human">
          Plans whose policy decision requires approval appear here until someone decides or the window
          closes.
        </EmptyState>
      )}
      {data?.items.map((approval) => (
        <ApprovalPending key={approval.execution_id} approval={approval} writable={!READ_ONLY} />
      ))}
    </div>
  );
}
