import { ListSkeleton } from "@/components/common/LoadingSkeleton";
import { Skeleton } from "@/components/ui/skeleton";

export default function Loading() {
  return (
    <div className="flex flex-col gap-6" aria-busy="true">
      <Skeleton className="h-8 w-2/3" />
      <Skeleton className="h-16" />
      <ListSkeleton rows={3} />
    </div>
  );
}
