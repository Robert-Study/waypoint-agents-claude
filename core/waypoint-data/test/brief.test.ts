import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { buildWorkerBrief, buildPromptPacket } from "../src/brief.ts";

const fx = JSON.parse(readFileSync(new URL("../fixtures/brief.json", import.meta.url), "utf8"));

for (const c of fx.cases) {
  test(`fixture ${c.name}: brief`, () => {
    assert.equal(buildWorkerBrief(c.node, c.graph, c.todos), c.expected_brief);
  });
  test(`fixture ${c.name}: packet`, () => {
    assert.equal(buildPromptPacket(c.node, c.graph), c.expected_packet);
  });
}

test("packet quotes the goal verbatim", () => {
  const c = fx.cases[0];
  const p = buildPromptPacket(c.node, c.graph);
  const quoted = c.node.goal.split("\n").map((l: string) => "> " + l).join("\n");
  assert.ok(p.includes(quoted));
  assert.ok(p.includes(c.node.check.run));
  for (const f of c.node.files) assert.ok(p.includes(f));
  assert.ok(p.includes(`.waypoint/receipts/${c.node.project}.${c.node.id}.json`));
  assert.ok(p.includes("data-schema-core") && p.includes("node, check and todo shapes"));
  assert.ok(p.includes("run-dispatch-core"));
});

test("node without prompt still yields a packet from its fields", () => {
  const c = fx.cases[1];
  assert.equal(c.node.prompt, undefined);
  const p = buildPromptPacket(c.node, c.graph);
  assert.ok(p.includes(c.node.goal));
  assert.ok(p.includes("## Exit criteria"));
  assert.ok(!p.includes("## Plan prompt"));
});

test("packet never leaks tokens, keys or absolute personal paths", () => {
  const node = {
    ...fx.cases[0].node,
    goal: "Read C:" + String.fromCharCode(92) + "Users" + String.fromCharCode(92) + "home" + String.fromCharCode(92) + "secret and /home/rob/x with owner token abcdef0123456789abcdef0123456789 and sk-" + "ant-api03-AAAAAAAAAAAAAAAAAAAA",
    prompt: "Use X-CC-Owner-Token: deadbeefdeadbeefdeadbeef and /Users/rob/proj and gh" + "p_abcdefghijklmnopqrstuvwxyz0123456789",
  };
  for (const out of [buildPromptPacket(node, fx.cases[0].graph), buildWorkerBrief(node, fx.cases[0].graph, [])]) {
    assert.ok(!/C:[^ ]{1,2}Users/i.test(out));
    assert.ok(!out.includes("/home/rob") && !out.includes("/Users/rob"));
    assert.ok(!/abcdef0123456789abcdef0123456789|sk-ant|deadbeefdeadbeef|ghp_/.test(out));
  }
});

test("pure: does not mutate inputs", () => {
  const c = structuredClone(fx.cases[0]);
  const before = JSON.stringify(c);
  buildWorkerBrief(c.node, c.graph, c.todos);
  buildPromptPacket(c.node, c.graph);
  assert.equal(JSON.stringify(c), before);
});
