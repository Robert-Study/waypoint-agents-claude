import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { computeFloat } from "../src/float.ts";

const fixture = JSON.parse(
  readFileSync(new URL("../fixtures/float.json", import.meta.url), "utf8"),
);

for (const c of fixture.cases) {
  test(`fixture: ${c.name}`, () => {
    if (c.expect_error) {
      assert.throws(() => computeFloat(c.nodes), (e: Error) => new RegExp(c.expect_error, "i").test(e.message));
      return;
    }
    const out = computeFloat(c.nodes);
    assert.equal(out.length, c.nodes.length);
    for (const [id, want] of Object.entries(c.expect) as [string, any][]) {
      const got = out.find((r) => r.id === id);
      assert.ok(got, `missing ${id}`);
      assert.deepEqual(
        { early: got.early, late: got.late, float: got.float, critical: got.critical },
        want,
        id,
      );
    }
  });
}

test("cycle error names the nodes involved", () => {
  assert.throws(
    () => computeFloat([{ id: "a", deps: ["b"] }, { id: "b", deps: ["a"] }]),
    /cycle.*a.*b|cycle.*b.*a/i,
  );
});

test("trap: done node or deleted dep does not change unrelated branch float", () => {
  const base = [
    { id: "x", estimate: 3 },
    { id: "y", deps: ["x"], estimate: 2 },
    { id: "z", estimate: 1 },
  ];
  const before = computeFloat(base);
  const withDone = computeFloat([...base, { id: "d", status: "done", estimate: 50 }, { id: "e", deps: ["d", "deleted"], status: "done" }]);
  for (const r of before) {
    assert.deepEqual(withDone.find((w) => w.id === r.id), r);
  }
});

test("pure: does not mutate input and keeps input order", () => {
  const nodes = [{ id: "b", deps: ["a"] }, { id: "a" }];
  const snap = JSON.stringify(nodes);
  const out = computeFloat(nodes);
  assert.equal(JSON.stringify(nodes), snap);
  assert.deepEqual(out.map((r) => r.id), ["b", "a"]);
});
