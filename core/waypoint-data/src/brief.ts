// Worker brief and copy-paste prompt packet builders for Waypoint 1.2.
// Pure functions: no I/O, no clock, no randomness. A Python port replays fixtures/brief.json.
import type { WaypointNode, Todo, Check } from "./types.ts";

export interface BriefGraph {
  nodes: { id: string; title?: string; deps: { id: string; crosses: string }[] }[];
}

const RULES = [
  "Do not change the goal.",
  "Do only this task. Write only the owned files (and the test files the check names).",
  "Write the check first if it does not exist, see it fail, then make it pass. Never weaken or skip it.",
  "No paid routes, no new credentials, never push public.",
  "Report evidence: the exact command output or files that prove the check passes.",
];

// Redaction: tokens, keys and absolute personal paths never appear in a brief or packet.
const REDACTIONS: [RegExp, string][] = [
  [/\b(X-CC-(?:Owner-)?Token\s*[:=]\s*)\S+/gi, "$1[redacted]"],
  [/\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,})/g, "[redacted-key]"],
  [/\b[0-9a-fA-F]{24,}\b/g, "[redacted-token]"],
  [/[A-Za-z]:[\\/]+Users[\\/]+[^\s"'`)\]]*/gi, "<path>"],
  [/\/(?:home|Users)\/[^\s"'`)\]]*/g, "<path>"],
];

export function scrub(text: string): string {
  let out = text;
  for (const [re, rep] of REDACTIONS) out = out.replace(re, rep);
  return out;
}

export function receiptPath(node: { project: string; id: string }): string {
  return `.waypoint/receipts/${node.project}.${node.id}.json`;
}

function describeCheck(check: Check): string {
  switch (check.type) {
    case "command": {
      let s = `Run \`${check.run ?? ""}\`; it must exit 0`;
      if (check.expect_stdout) s += ` and print "${check.expect_stdout}"`;
      return s + ".";
    }
    case "files_exist":
      return `These paths must exist: ${(check.paths ?? []).join(", ")}.`;
    case "text_present":
      return `File ${check.path ?? ""} must contain "${check.text ?? ""}".`;
    case "human":
      return `Owner confirms: ${check.prompt ?? ""}`;
    default:
      return "(unknown check type)";
  }
}

function quote(text: string): string {
  return text.split("\n").map((l) => (l === "" ? ">" : "> " + l)).join("\n");
}

function downstream(node: WaypointNode, graph?: BriefGraph) {
  const res: { id: string; title: string; crosses: string }[] = [];
  for (const n of graph?.nodes ?? []) {
    const d = (n.deps ?? []).find((x) => x.id === node.id);
    if (d) res.push({ id: n.id, title: n.title ?? n.id, crosses: d.crosses });
  }
  return res;
}

function filesBlock(node: WaypointNode): string[] {
  return node.files.length ? node.files.map((f) => `- ${f}`) : ["- (none named)"];
}

function depsBlock(node: WaypointNode): string[] {
  return node.deps.length ? node.deps.map((d) => `- ${d.id}: ${d.crosses}`) : ["- (none)"];
}

/** Context text the mod injects into a subagent prompt and the web page shows. */
export function buildWorkerBrief(
  node: WaypointNode,
  graph?: BriefGraph,
  todos: Pick<Todo, "text" | "status" | "node">[] = [],
): string {
  const out = downstream(node, graph);
  const open = todos.filter(
    (t) => t.status === "open" && t.node && t.node.project === node.project && t.node.id === node.id,
  );
  const lines = [
    `# Your node: ${node.id} (${node.title})`,
    `Project: ${node.project}`,
    "",
    "## Goal (fixed)",
    quote(node.goal),
    "",
    "## Done-check",
    describeCheck(node.check),
    "",
    "## Owned files (write only these)",
    ...filesBlock(node),
    "",
    "## Comes in",
    ...depsBlock(node),
    "",
    "## Goes out",
    ...(out.length ? out.map((o) => `- ${o.id}: ${o.crosses}`) : ["- (nothing depends on this node)"]),
  ];
  if (open.length) lines.push("", "## Open to-dos on this node", ...open.map((t) => `- ${t.text}`));
  lines.push("", "## Receipt", `Write ${receiptPath(node)} when finished.`, "", "## Rules", ...RULES.map((r) => `- ${r}`));
  return scrub(lines.join("\n")) + "\n";
}

/** Self-contained copy-paste packet for one node. */
export function buildPromptPacket(node: WaypointNode, graph?: BriefGraph): string {
  const out = downstream(node, graph);
  const lines = [
    `# Task ${node.id}: ${node.title}`,
    `Project: ${node.project}`,
    "",
    "## Goal (locked, quoted exactly)",
    quote(node.goal),
    "",
    "## Done-check (must pass)",
    describeCheck(node.check),
    "```json",
    JSON.stringify(node.check, null, 1),
    "```",
    "",
    "## Inputs (what crosses in)",
    ...depsBlock(node),
    "",
    "## Owned files (write only these)",
    ...filesBlock(node),
    "",
    "## What crosses out",
    ...(out.length ? out.map((o) => `- ${o.id} (${o.title}): ${o.crosses}`) : ["- (nothing depends on this node)"]),
  ];
  if (node.prompt && node.prompt.trim()) lines.push("", "## Plan prompt", node.prompt);
  lines.push(
    "",
    "## Exit criteria",
    "- The done-check passes on your branch.",
    "- Only the owned files changed.",
    "- The deliverable is complete, not empty.",
    "",
    "## Receipt format",
    `Write one file, ${receiptPath(node)}:`,
    "```json",
    JSON.stringify(
      {
        schema: "waypoint.receipt.v1",
        project: node.project,
        node: node.id,
        status: "done",
        summary: "One plain sentence.",
        evidence: ["command: result"],
        outputs: ["path/you/wrote"],
        todo: null,
      },
      null,
      1,
    ),
    "```",
    "",
    "## Rules",
    ...RULES.map((r) => `- ${r}`),
  );
  return scrub(lines.join("\n")) + "\n";
}
