// Run with `npm test` (node --test). Node 24 strips the types natively; no test framework needed.
import assert from "node:assert/strict";
import { test } from "node:test";

import { changedLines, countChanges, deepEqual, diff } from "./diff.ts";

test("identical state is a single unchanged node", () => {
  const state = { replicas: 3, containers: [{ name: "api", image: "v1" }] };
  assert.equal(diff(state, structuredClone(state)).kind, "same");
});

test("a compensation that restored state exactly reports no changes", () => {
  // The chaos test asserts this with ==; the dashboard must agree with it.
  assert.deepEqual(changedLines(diff({ a: { b: [1, 2] } }, { a: { b: [1, 2] } })), []);
});

test("only changed paths are reported, with copyable lines", () => {
  const before = { replicas: 3, revision: "4", containers: [{ name: "api", image: "v2" }] };
  const after = { replicas: 3, revision: "3", containers: [{ name: "api", image: "v1" }] };
  assert.deepEqual(changedLines(diff(before, after)), [
    'containers[0].image: "v2" -> "v1"',
    'revision: "4" -> "3"',
  ]);
});

test("added and removed keys are distinguished from changed ones", () => {
  const node = diff({ a: 1, gone: true }, { a: 1, fresh: "x" });
  assert.deepEqual(changedLines(node), ['fresh: (absent) -> "x"', "gone: true -> (absent)"]);
  assert.equal(node.kind === "nested" ? countChanges(node.children) : -1, 2);
});

test("arrays compare by index, including length changes", () => {
  assert.deepEqual(changedLines(diff([1, 2], [1, 2, 3])), ["[2]: (absent) -> 3"]);
});

test("a type change at a leaf is a change, not a crash", () => {
  assert.deepEqual(changedLines(diff({ limit: "512Mi" }, { limit: null })), ['limit: "512Mi" -> null']);
});

test("deepEqual treats key order as irrelevant", () => {
  assert.equal(deepEqual({ a: 1, b: 2 }, { b: 2, a: 1 }), true);
  assert.equal(deepEqual({ a: 1 }, { a: 1, b: undefined }), false);
});
