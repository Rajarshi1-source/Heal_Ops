import { KPISkeleton, ListSkeleton } from "@/components/common/LoadingSkeleton";

export default function Loading() {
  return (
    <div className="flex flex-col gap-8" aria-busy="true">
      <KPISkeleton />
      <ListSkeleton />
    </div>
  );
}
