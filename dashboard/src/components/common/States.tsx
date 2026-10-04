import { Card } from "@/components/ui/card";
import { relative } from "@/lib/format";

/** The four states every data view has: loading (see LoadingSkeleton), empty, error, stale. */

export function EmptyState({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Card className="flex flex-col items-center gap-2 px-6 py-12 text-center">
      <p className="text-sm font-medium">{title}</p>
      <div className="max-w-md text-sm text-muted">{children}</div>
    </Card>
  );
}

export function ErrorState({ error }: { error: string }) {
  return (
    <Card role="alert" className="border-tone-warning/40 px-5 py-4">
      <p className="text-sm font-medium text-tone-warning">The engine could not be reached</p>
      <p className="mt-1 text-sm text-muted">
        {error}. Automation keeps running inside the cluster; this view will recover on its own.
      </p>
    </Card>
  );
}

/** Shown whenever data came from cache: a dashboard 30 seconds stale beats one that errors. */
export function Freshness({ fetchedAt }: { fetchedAt: string | null }) {
  if (!fetchedAt) return null;
  const age = (Date.now() - new Date(fetchedAt).getTime()) / 1000;
  return (
    <p className="text-[0.7rem] text-subtle" title={fetchedAt}>
      {age > 30 ? (
        <span className="text-tone-warning">Stale — data from {relative(fetchedAt)}</span>
      ) : (
        <>Updated {relative(fetchedAt)}</>
      )}
    </p>
  );
}
