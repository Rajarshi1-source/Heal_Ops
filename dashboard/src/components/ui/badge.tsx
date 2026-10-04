// Copied from shadcn/ui; variants replaced by the outcome tones so colour always carries meaning.
import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

import { TONE_CLASSES, type Tone } from "./tone";

export function Badge({ tone, className, ...props }: ComponentProps<"span"> & { tone: Tone }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset whitespace-nowrap",
        TONE_CLASSES[tone],
        className,
      )}
      {...props}
    />
  );
}
