// Types for the ops-sessions data files. They mirror ../schema/*.schema.json; check_ops_sessions.mjs
// fails when a schema title or property is missing here.

export type Nullable<T> = T | null;

// ---- sessions/<id>.json (session_event.schema.json) ----
export interface SessionCallback {
  from_session: string;
  to_session: string;
  outcome: "done" | "error" | "needs_you" | "question";
  summary?: string | null;
  receipt?: string | null;
}

export interface SessionEvent {
  seq: number;
  at: string;
  kind: "started" | "message" | "tool" | "fork" | "callback" | "status" | "ended";
  actor?: string | null;
  summary?: string | null;
  parent_id?: string | null;
  fork_id?: string | null;
  callback?: SessionCallback | null;
  data?: Record<string, unknown> | null;
}

export interface SessionFile {
  schema: "waypoint.session.v1";
  id: string;
  agent: string;
  parent?: string | null;
  fork_of?: string | null;
  orchestrator?: string | null;
  project?: string | null;
  node?: string | null;
  cwd?: string | null;
  title?: string | null;
  status: "running" | "idle" | "done" | "error" | "archived";
  started_at?: string;
  updated_at?: string;
  events: SessionEvent[];
}

// ---- orchestrators/<id>.json (orchestrator.schema.json) ----
export interface OrchestratorLocation {
  kind: "local" | "worktree" | "cloud";
  host?: string | null;
  cwd?: string | null;
  worktree?: string | null;
  branch?: string | null;
}

export interface OrchestratorFile {
  schema: "waypoint.orchestrator.v1";
  id: string;
  kind: "lead" | "project";
  title?: string | null;
  provider?: "claude" | "codex" | "other" | null;
  session?: string | null;
  projects?: string[];
  children?: string[];
  location: OrchestratorLocation;
  status: "active" | "paused" | "retired";
  created_at?: string;
  updated_at?: string;
}

// ---- brain/index.json (brain_index.schema.json) ----
export interface BrainNote {
  id: string;
  path: string;
  title: string;
  kind: "mental_model" | "doctrine" | "learning" | "source" | "robert_words" | "other";
  provenance?: "owner" | "vetted" | "agent" | "unknown";
  tags?: string[];
  summary?: string | null;
  updated_at?: string;
  links?: string[];
}

export interface BrainIndexFile {
  schema: "waypoint.brain_index.v1";
  generated_at: string;
  notes: BrainNote[];
}

// ---- chats/<id>.json and chats/<id>/messages.ndjson (chat.schema.json) ----
export type ChatProvider = "claude" | "codex";

export interface ChatMessage {
  seq: number;
  provider: ChatProvider;
  role: "user" | "assistant" | "system" | "tool";
  text: string;
  at: string;
  turn_id?: string | null;
}

export interface ChatFile {
  schema: "waypoint.chat.v1";
  id: string;
  provider: ChatProvider;
  kind?: "direct" | "subagent";
  parent_session?: string | null;
  title?: string | null;
  status: "open" | "running" | "done" | "error" | "archived";
  created_at?: string;
  updated_at?: string;
  messages?: ChatMessage[];
}

// ---- snapshots/<project>/<ts>.json (snapshot.schema.json) ----
export interface SnapshotProject {
  id: string;
  title: string;
  brief?: string | null;
  cwd?: string | null;
  created?: string | null;
  status?: string | null;
  family?: string | null;
  family_title?: string | null;
  version?: string | null;
  archived?: boolean;
}

export interface SnapshotClaim {
  by?: string | null;
  worktree?: string | null;
  cwd?: string | null;
  environment?: string | null;
  since?: string | null;
}

export interface SnapshotNode {
  id: string;
  title: string;
  goal?: string | null;
  check?: unknown;
  deps?: unknown[];
  owner?: string | null;
  lane?: string | null;
  status: string;
  attempts?: number;
  actor?: string | null;
  evidence?: string | null;
  claim?: SnapshotClaim | null;
  [optionalEngineField: string]: unknown;
}

export interface BoardSnapshot {
  schema: string;
  project: SnapshotProject;
  revision: number;
  nodes: SnapshotNode[];
}

// ---- briefs/<day>.json (brief.schema.json) ----
export interface BriefItem {
  project?: string | null;
  node?: string | null;
  title: string;
  summary?: string | null;
  [extra: string]: unknown;
}

export interface BriefUsage {
  pool: string;
  used_pct?: number | null;
  floor_pct?: number | null;
  text?: string | null;
  [extra: string]: unknown;
}

export interface MorningBrief {
  since: string;
  until: string;
  hours: number;
  paragraph: string;
  done: number;
  failed: number;
  needs_you: number;
  done_items?: BriefItem[];
  failed_items?: BriefItem[];
  usage?: BriefUsage[];
  triage?: unknown[] | Record<string, unknown> | null;
}

export interface BriefConstraint {
  project: string;
  node?: string | null;
  line: string;
  accepted?: boolean;
  derived?: boolean;
  lands_in?: string | null;
  source?: string | null;
  created_at?: string | null;
}

export interface BriefFile {
  schema: "waypoint.brief.v1";
  day: string;
  brief: MorningBrief;
  constraints: BriefConstraint[];
}

// ---- learn/{mastery,cards,log}.json (learn.schema.json) ----
export interface LearnMastery {
  competency_id: string;
  level: number;
  attempts: number;
  updated_at?: string | null;
}

export interface LearnCard {
  question_id: string;
  reps: number;
  ease: number;
  interval_days: number;
  due_at: string;
  last_rating?: number | null;
  updated_at?: string | null;
}

export interface LearnLogEntry {
  seq: number;
  at: string;
  question_id: string;
  competency_id?: string | null;
  outcome: string;
  before?: number | null;
  after?: number | null;
}

export interface LearnMasteryFile {
  schema: "waypoint.learn.v1";
  kind: "mastery";
  items: LearnMastery[];
}

export interface LearnCardsFile {
  schema: "waypoint.learn.v1";
  kind: "cards";
  items: LearnCard[];
}

export interface LearnLogFile {
  schema: "waypoint.learn.v1";
  kind: "log";
  items: LearnLogEntry[];
}

export type LearnFile = LearnMasteryFile | LearnCardsFile | LearnLogFile;

// ---- plan_stages/<id>/<stage>.json (plan_stage.schema.json) ----
export interface PlanDraftNode {
  id: string;
  title: string;
  goal: string;
  check?: unknown;
  deps?: unknown[];
  lane?: string | null;
  owner?: string | null;
  files?: string[];
  [extra: string]: unknown;
}

export interface PlanFinding {
  node?: string | null;
  severity?: "info" | "warn" | "block" | null;
  text: string;
}

export interface PlanQuestion {
  id: string;
  prompt: string;
  kind?: "scale" | "tick" | "text" | null;
  options?: string[];
  answer?: unknown;
  purpose?: string | null;
}

export interface PlanResearchTopic {
  topic: string;
  finding: string;
  sources?: string[];
}

export interface PlanDraftFile {
  schema: "waypoint.plan_stage.v1";
  stage: "draft";
  project: string;
  brief?: string | null;
  created_at?: string;
  nodes: PlanDraftNode[];
}

export interface PlanCheckDraftFile {
  schema: "waypoint.plan_stage.v1";
  stage: "check_draft";
  ok: boolean;
  checked_at?: string;
  findings: PlanFinding[];
}

export interface PlanDoctorFile {
  schema: "waypoint.plan_stage.v1";
  stage: "doctor";
  project?: string | null;
  node: string;
  diagnosis: string;
  cause?: "check" | "brief" | "environment" | "model" | "unknown" | null;
  suggestions?: string[];
  created_at?: string;
}

export interface PlanResearchFile {
  schema: "waypoint.plan_stage.v1";
  stage: "research";
  created_at?: string;
  topics: PlanResearchTopic[];
}

export interface PlanQuestionsFile {
  schema: "waypoint.plan_stage.v1";
  stage: "questions";
  created_at?: string;
  questions: PlanQuestion[];
}

export interface PlanCritiqueFile {
  schema: "waypoint.plan_stage.v1";
  stage: "critique";
  verdict: "accept" | "revise" | "reject";
  created_at?: string;
  findings: PlanFinding[];
}

export type PlanStageFile =
  | PlanDraftFile
  | PlanCheckDraftFile
  | PlanDoctorFile
  | PlanResearchFile
  | PlanQuestionsFile
  | PlanCritiqueFile;
