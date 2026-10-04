"use client";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

export default function ErrorBoundary({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <Card role="alert" className="flex flex-col items-start gap-3 p-6">
      <p className="text-sm font-medium text-tone-warning">This view failed to render</p>
      <p className="text-sm text-muted">{error.message}</p>
      <Button size="sm" onClick={reset}>
        Try again
      </Button>
    </Card>
  );
}
