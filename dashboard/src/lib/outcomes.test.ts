import assert from "node:assert/strict";
import { test } from "node:test";

import { duration, percent } from "./format.ts";
import { OUTCOMES } from "./outcomes.ts";

test("every outcome has a label, a tone and an explanation", () => {
  for (const [outcome, meta] of Object.entries(OUTCOMES)) {
    assert.ok(meta.label, outcome);
    assert.ok(meta.description.length > 10, outcome);
  }
});

test("escalation, refusal and denial are never styled as failures", () => {
  // The system admitting "not mine" or "not allowed" is a good outcome.
  for (const outcome of ["escalated", "refused", "denied"] as const) {
    assert.notEqual(OUTCOMES[outcome].tone, "critical");
  }
});

test("a revert reads as a correction, and only revert_failed is critical", () => {
  assert.equal(OUTCOMES.reverted.tone, "correction");
  const critical = Object.entries(OUTCOMES).filter(([, m]) => m.tone === "critical").map(([o]) => o);
  assert.deepEqual(critical, ["revert_failed"]);
});

test("rates show a dash when there is no denominator, not 0%", () => {
  assert.equal(percent({ value: null, numerator: 0, denominator: 0 }), "—");
  assert.equal(percent({ value: 0.6, numerator: 3, denominator: 5 }), "60%");
});

test("durations", () => {
  assert.equal(duration(null), "—");
  assert.equal(duration(42), "42s");
  assert.equal(duration(90), "1m 30s");
  assert.equal(duration(7300), "2h 1m");
});
