// Waypoint 1.2 file data model: core entities. Mirrors ../schema/*.schema.json; keep the two in step.
// Computed values (such as a node's float) are never stored, so they do not appear here.

export type Id = string;
export type Iso = string; // ISO 8601 timestamp

/** On every mutable file: rev goes up by one on each write; writers compare it before saving. */
export interface Mutable {
  rev: number;
  updated_at: Iso;
  updated_by: string;
}

/** Machine name plus a path relative to that machine's code root. Never an absolute drive path. */
export interface Cwd {
  machine: string;
  rel: string;
}

// ---- check (embedded in node.check)
export type CheckType = "human" | "command" | "files_exist" | "text_present";
export interface Check {
  type: CheckType;
  prompt?: string; // human
  run?: string; // command (required for command)
  expect_stdout?: string; // command
  paths?: string[]; // files_exist (required)
  path?: string; // text_present (required)
  text?: string; // text_present (required)
  [extra: string]: unknown;
}

// ---- project: projects/<id>/project.json
export interface Project extends Mutable {
  schema: "waypoint.project.v1";
  id: Id;
  title: string;
  brief?: string;
  goal_hash?: string | null;
  cwd: Cwd;
  created_at: Iso;
  archived: boolean;
  status: "draft" | "active";
  family: Id | null;
  version: number | null;
  /** The product's own version name (V10, 1.2); shown instead of the ordinal `version`. */
  version_label?: string | null;
}

// ---- family: families/<family>.json
export interface Family extends Mutable {
  schema: "waypoint.family.v1";
  id: Id;
  title: string;
  created_at: Iso;
  projects: Id[];
}

// ---- version: versions/<project>/<n>.json (append-only)
export interface VersionDecision {
  node: Id;
  decision: "carry" | "skip";
  reason: string;
  decided_by?: string;
  decided_at?: Iso;
}
export interface Version extends Mutable {
  schema: "waypoint.version.v1";
  project: Id;
  n: number;
  family?: Id | null;
  created_at: Iso;
  from_project?: Id | null;
  decisions: VersionDecision[];
}

// ---- node: projects/<id>/nodes/<node>.json
export type NodeStatus =
  | "not_started" | "ready" | "running" | "submitted" | "done" | "error" | "needs_you" | "skipped" | "carried";
export interface Dep {
  id: Id;
  crosses: string; // what passes across the edge; never empty
}
export interface NodeLocation {
  where: "pc" | "cloud" | "unset";
  changed_at: Iso | null;
}
export interface WaypointNode extends Mutable {
  schema: "waypoint.node.v1";
  project: Id;
  id: Id;
  title: string;
  goal: string;
  goal_hash?: string | null;
  check_hash?: string | null;
  check: Check;
  deps: Dep[];
  parent?: Id | null;
  estimate: number; // default 1
  slot: number | null; // start offset within the float; null = earliest
  location: NodeLocation;
  files: string[];
  kind: string;
  risk?: string;
  prompt?: string;
  recommended?: string | null;
  effort?: string | null;
  owner?: string;
  lane?: string | null;
  pool?: string | null;
  model?: string | null;
  status: NodeStatus;
  attempts?: number;
  actor?: string | null;
  evidence?: string | null;
  tags?: string[]; // tool labels, e.g. "stale" (running, no update for 30 min)
}

// ---- run: runs/<run_id>.json (+ runs/<run_id>/events.ndjson)
export interface Sprint {
  id: Id;
  pool: string;
  start_at?: Iso | null;
  end_at?: Iso | null;
  status: string;
}
export interface Run extends Mutable {
  schema: "waypoint.run.v1";
  id: Id;
  project: Id;
  settings?: Record<string, unknown>;
  status: string;
  created_at: Iso;
  started_at?: Iso | null;
  ended_at?: Iso | null;
  sprints?: Sprint[];
}

/** One line of runs/<run_id>/events.ndjson. Append-only: no rev. */
export interface WaypointEvent {
  seq: number;
  at: Iso;
  project?: Id | null;
  node?: Id | null;
  kind: string;
  actor: string;
  summary: string;
  data?: Record<string, unknown> | null;
}

// ---- todo: todos/<id>.json. The writer sets category and subtype; no reader classifies.
export type TodoCategory = "review" | "decisions" | "hands_on";
export type TodoSubtype = "failure" | "approval" | "own";
interface TodoBase extends Mutable {
  schema: "waypoint.todo.v1";
  id: Id;
  text: string;
  status: "open" | "handed_off" | "done" | "dropped";
  link: string | null;
  node: { project: Id; id: Id } | null;
  project: Id | null;
  order: number;
  merged_into: Id | null;
  click_count: number;
  next_click: string | null;
  created_at: Iso;
  created_by?: string;
  first_clicked_at?: Iso | null;
  archived_at?: Iso | null;
  done_by?: string | null;
  handoffs?: Record<string, unknown>[];
  deliverables?: string[];
}
export type Todo =
  | (TodoBase & { category: "review"; subtype: "failure" }) // shown red
  | (TodoBase & { category: "decisions"; subtype: "approval" })
  | (TodoBase & { category: "hands_on"; subtype: "own" }); // Owner's own

// ---- question: questions/<id>.json
export interface Question extends Mutable {
  schema: "waypoint.question.v1";
  id: Id;
  project: Id | null;
  node: Id | null;
  kind: "select" | "likert" | "text";
  prompt: string;
  options?: string[]; // required for select; never carries a preselected flag
  scale?: number | null; // likert, default 9
  answer: unknown; // null until answered
  answered_at: Iso | null;
  captured: boolean;
  allowed?: string[] | null;
  proposal?: unknown;
  purpose?: string | null;
  session?: string | null; // the session that asked, when known
  resume?: QuestionResume | null; // written once by the runner after the answer (resume.py)
  created_at: Iso;
}

/** How an answer goes back to the work: direct to a warm session, or fresh (resume from Waypoint) when it is cold. */
export interface QuestionResume {
  mode: "direct" | "fresh";
  session?: string | null;
  line: string; // "resume <project> from Waypoint"
  idle_minutes?: number | null;
  cold_after_minutes: number;
  reason: string;
  action: "deliver" | "restart_node" | "clear_session";
  node_status_before?: string | null;
  decided_at: Iso;
  delivered_at?: Iso | null;
}

// ---- receipt: receipts/<name>.json (waypoint.receipt.v1 unchanged; write-once, no rev)
export interface Receipt {
  schema: "waypoint.receipt.v1";
  status: "done" | "error" | "needs_you";
  summary: string; // at most 300 characters
  project?: string;
  node?: string;
  evidence?: string[];
  outputs?: string[];
  todo?: { text: string; section?: "human" | "computer"; link?: string | null } | null;
  questions?: Record<string, unknown>[];
  metrics?: Record<string, unknown>;
  session?: string;
  agent?: string;
  [extra: string]: unknown;
}
