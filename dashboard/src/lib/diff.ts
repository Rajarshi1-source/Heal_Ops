/**
 * A structural diff of two JSON values, for `state_before` versus `state_after`.
 *
 * This is how a reviewer confirms a compensation restored state exactly, so the rules are strict:
 * values compare by deep equality, arrays by index, and an unchanged subtree is reported as one
 * collapsed node rather than expanded line by line. Pure and dependency-free, so it is tested with
 * `node --test` directly.
 */

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

export type DiffNode =
  | { kind: "same"; path: string; value: Json }
  | { kind: "changed"; path: string; before: Json; after: Json }
  | { kind: "added"; path: string; after: Json }
  | { kind: "removed"; path: string; before: Json }
  | { kind: "nested"; path: string; children: DiffNode[]; changes: number };

function isObject(value: unknown): value is { [key: string]: Json } {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((item, i) => deepEqual(item, b[i]));
  }
  if (isObject(a) && isObject(b)) {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    for (const key of keys) {
      if (!(key in a) || !(key in b) || !deepEqual(a[key], b[key])) return false;
    }
    return true;
  }
  return false;
}

function child(path: string, key: string | number): string {
  return typeof key === "number" ? `${path}[${key}]` : path ? `${path}.${key}` : key;
}

export function diff(before: Json, after: Json, path = ""): DiffNode {
  if (deepEqual(before, after)) return { kind: "same", path, value: before };

  const bothObjects = isObject(before) && isObject(after);
  const bothArrays = Array.isArray(before) && Array.isArray(after);
  if (!bothObjects && !bothArrays) return { kind: "changed", path, before, after };

  const children: DiffNode[] = [];
  if (bothArrays) {
    const length = Math.max(before.length, after.length);
    for (let i = 0; i < length; i++) {
      const p = child(path, i);
      if (i >= before.length) children.push({ kind: "added", path: p, after: after[i] as Json });
      else if (i >= after.length) children.push({ kind: "removed", path: p, before: before[i] as Json });
      else children.push(diff(before[i] as Json, after[i] as Json, p));
    }
  } else if (bothObjects) {
    const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])].sort();
    for (const key of keys) {
      const p = child(path, key);
      if (!(key in before)) children.push({ kind: "added", path: p, after: after[key] as Json });
      else if (!(key in after)) children.push({ kind: "removed", path: p, before: before[key] as Json });
      else children.push(diff(before[key] as Json, after[key] as Json, p));
    }
  }
  return { kind: "nested", path, children, changes: countChanges(children) };
}

export function countChanges(nodes: DiffNode[]): number {
  return nodes.reduce((total, node) => {
    switch (node.kind) {
      case "same":
        return total;
      case "nested":
        return total + node.changes;
      case "changed":
      case "added":
      case "removed":
        return total + 1;
    }
  }, 0);
}

/** Every changed leaf as a copyable `path: before -> after` line, in path order. */
export function changedLines(node: DiffNode): string[] {
  switch (node.kind) {
    case "same":
      return [];
    case "changed":
      return [`${node.path}: ${JSON.stringify(node.before)} -> ${JSON.stringify(node.after)}`];
    case "added":
      return [`${node.path}: (absent) -> ${JSON.stringify(node.after)}`];
    case "removed":
      return [`${node.path}: ${JSON.stringify(node.before)} -> (absent)`];
    case "nested":
      return node.children.flatMap(changedLines);
  }
}
