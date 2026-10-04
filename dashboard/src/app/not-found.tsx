import Link from "next/link";

import { EmptyState } from "@/components/common/States";

export default function NotFound() {
  return (
    <EmptyState title="Not found">
      There is no incident with that id.{" "}
      <Link href="/" className="underline underline-offset-2">
        Back to incidents
      </Link>
    </EmptyState>
  );
}
