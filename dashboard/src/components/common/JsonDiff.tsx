"use client";

import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { changedLines, type DiffNode, diff, type Json } from "@/lib/diff";
import { cn } from "@/lib/utils";

/**
 * `state_before` against `state_after`. This is how a reviewer confirms a compensation restored the
 * cluster exactly, so only changed paths are highlighted, unchanged subtrees collapse by default,
 * and the changed lines copy as plain text.
 */
export function JsonDiff({ before, after, label }: { before: unknown; after: unknown; label?: string }) {
  const root = useMemo(() => diff(before as Json, after as Json), [before, after]);
  const lines = useMemo(() => changedLines(root), [root]);
  const [copied, setCopied] = useState(false);

  async function copy() {
    await navigator.clipboard.writeText(lines.join("\n"));
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className="rounded-lg border border-border bg-background/60">
      <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
        <p className="text-xs text-muted">
          {label ?? "State diff"} ·{" "}
          {lines.length === 0 ? (
            <span className="text-tone-success">identical</span>
          ) : (
            <span className="text-tone-warning">{lines.length} changed</span>
          )}
        </p>
        {lines.length > 0 && (
          <Button size="sm" variant="ghost" onClick={copy} aria-live="polite">
            {copied ? "Copied" : "Copy changes"}
          </Button>
        )}
      </div>
      <div className="overflow-x-auto p-3 font-mono text-xs leading-relaxed">
        <Node node={root} depth={0} root />
      </div>
    </div>
  );
}

function Node({ node, depth, root = false }: { node: DiffNode; depth: number; root?: boolean }) {
  const name = node.path.split(/[.[]/).pop()?.replace("]", "") || "(root)";
  const pad = { paddingLeft: `${depth * 14}px` };

  switch (node.kind) {
    case "same":
      return <Unchanged name={name} value={node.value} style={pad} />;
    case "changed":
      return (
        <div style={pad} className="rounded bg-tone-warning/8">
          <span className="text-muted">{name}: </span>
          <del className="text-tone-critical/80 no-underline">{fmt(node.before)}</del>
          <span className="text-subtle"> → </span>
          <ins className="text-tone-success no-underline">{fmt(node.after)}</ins>
        </div>
      );
    case "added":
      return (
        <div style={pad} className="rounded bg-tone-success/8 text-tone-success">
          + {name}: {fmt(node.after)}
        </div>
      );
    case "removed":
      return (
        <div style={pad} className="rounded bg-tone-critical/8 text-tone-critical/80">
          − {name}: {fmt(node.before)}
        </div>
      );
    case "nested":
      return (
        <div>
          {!root && (
            <div style={pad} className="text-muted">
              {name} <span className="text-subtle">({node.changes} changed)</span>
            </div>
          )}
          {node.children.map((c) => (
            <Node key={c.path} node={c} depth={root ? 0 : depth + 1} />
          ))}
        </div>
      );
  }
}

function Unchanged({ name, value, style }: { name: string; value: Json; style: React.CSSProperties }) {
  const [open, setOpen] = useState(false);
  const isTree = typeof value === "object" && value !== null;
  if (!isTree) {
    return (
      <div style={style} className="text-subtle">
        {name}: {fmt(value)}
      </div>
    );
  }
  return (
    <div style={style}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className={cn("text-subtle hover:text-muted")}
      >
        {open ? "▾" : "▸"} {name} <span className="italic">unchanged</span>
      </button>
      {open && <pre className="whitespace-pre-wrap pl-4 text-subtle">{JSON.stringify(value, null, 2)}</pre>}
    </div>
  );
}

function fmt(value: Json): string {
  return JSON.stringify(value);
}
