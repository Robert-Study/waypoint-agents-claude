// Types for the ops queue. Keep in step with schema/*.schema.json (check_ops_queue.mjs verifies the names).
// A payload never carries a key, token, secret or password.

export type RunMode = "frugal" | "balanced" | "push" | "max";
export type ActionStatus = "queued" | "done" | "failed" | "refused";
export type DispatchRoute = "session_subagent" | "side_session" | "headless" | "codex" | "free";

export type ClaimOwner = "mod" | "runner" | `cloud:${string}`;
export interface Claim {
  schema: "waypoint.claim.v1";
  project: string;
  node: string;
  owner: ClaimOwner;
  location: string;
  /** Random claim token proving the same claimer refreshes it. Not a credential. */
  token: string;
  heartbeat_at: string;
  since?: string;
  claimed_by?: string;
  environment?: string;
}

/** Action types only Owner may queue (by = "owner"). */
export const OWNER_ONLY_ACTION_TYPES = ["accept", "reject", "owner_status", "project_archive", "project_unarchive", "version_label", "version_decide"] as const;

interface ActionBase {
  schema: "waypoint.action.v1";
  id: string;
  status: ActionStatus;
  created_at: string;
  by: string;
  finished_at?: string;
  result?: Record<string, unknown>;
  error?: string;
}
export interface Action_run extends ActionBase { type: "run"; payload: { mode: RunMode; project?: string; node?: string }; }
export interface Action_retry extends ActionBase { type: "retry"; payload: { node: string; project?: string }; }
export interface Action_stop extends ActionBase { type: "stop"; payload: { node?: string; project?: string }; }
export interface Action_claim extends ActionBase { type: "claim"; payload: { node: string; project?: string; by?: string }; }
export interface Action_release extends ActionBase { type: "release"; payload: { node: string; project?: string }; }
export interface Action_accept extends ActionBase { type: "accept"; payload: { node: string; project?: string }; }
export interface Action_reject extends ActionBase { type: "reject"; payload: { node: string; project?: string; reason?: string }; }
export interface Action_owner_status extends ActionBase { type: "owner_status"; payload: { node: string; status: "not_started" | "ready" | "running" | "done" | "error" | "needs_you" | "blocked"; project?: string }; }
export interface Action_plan_draft extends ActionBase { type: "plan_draft"; payload: { goal: string; project?: string }; }
export interface Action_plan_deep extends ActionBase { type: "plan_deep"; payload: { project: string; stage: "research" | "questions" | "graph" | "critique" }; }
export interface Action_check_draft extends ActionBase { type: "check_draft"; payload: { node: string; project?: string }; }
export interface Action_failure_doctor extends ActionBase { type: "failure_doctor"; payload: { node: string; project?: string }; }
export interface Action_brain_rebuild extends ActionBase { type: "brain_rebuild"; payload: { scope?: "all" | "changed" }; }
export interface Action_todo_merge extends ActionBase { type: "todo_merge"; payload: { todo_ids: string[]; into?: string }; }
export interface Action_todo_click extends ActionBase { type: "todo_click"; payload: { todo_id: string }; }
export interface Action_todo_handoff extends ActionBase { type: "todo_handoff"; payload: { todo_id: string; to: "plan" | "project"; project?: string }; }
export interface Action_todo_add_to_project extends ActionBase { type: "todo_add_to_project"; payload: { todo_id: string; project: string }; }
export interface Action_project_archive extends ActionBase { type: "project_archive"; payload: { project: string }; }
export interface Action_project_unarchive extends ActionBase { type: "project_unarchive"; payload: { project: string }; }
export interface Action_version_new extends ActionBase { type: "version_new"; payload: { project: string; label?: string; title?: string; decisions?: Record<string, { action: "carry" | "skip"; reason?: string }> }; }
export interface Action_version_label extends ActionBase { type: "version_label"; payload: { project: string; label: string | null }; }
export interface Action_version_decide extends ActionBase { type: "version_decide"; payload: { project: string; version: string; decision: "keep" | "discard" }; }
export interface Action_family_group extends ActionBase { type: "family_group"; payload: { family: string; projects: string[] }; }
export interface Action_state_snapshot_write extends ActionBase { type: "state_snapshot_write"; payload: { project?: string; note?: string }; }
export interface Action_state_snapshot_resume extends ActionBase { type: "state_snapshot_resume"; payload: { snapshot_id: string; project?: string }; }
export interface Action_chat_send extends ActionBase { type: "chat_send"; payload: { session_id: string; text: string }; }
export interface Action_chat_stop extends ActionBase { type: "chat_stop"; payload: { session_id: string }; }
export interface Action_chat_resume extends ActionBase { type: "chat_resume"; payload: { session_id: string }; }
export interface Action_probe extends ActionBase { type: "probe"; payload: { target?: string }; }
export interface Action_sync_import extends ActionBase { type: "sync_import"; payload: { source: string; project?: string }; }
export interface Action_brief_build extends ActionBase { type: "brief_build"; payload: { node: string; project?: string }; }

export type Action =
  | Action_run
  | Action_retry
  | Action_stop
  | Action_claim
  | Action_release
  | Action_accept
  | Action_reject
  | Action_owner_status
  | Action_plan_draft
  | Action_plan_deep
  | Action_check_draft
  | Action_failure_doctor
  | Action_brain_rebuild
  | Action_todo_merge
  | Action_todo_click
  | Action_todo_handoff
  | Action_todo_add_to_project
  | Action_project_archive
  | Action_project_unarchive
  | Action_version_new
  | Action_version_label
  | Action_version_decide
  | Action_family_group
  | Action_state_snapshot_write
  | Action_state_snapshot_resume
  | Action_chat_send
  | Action_chat_stop
  | Action_chat_resume
  | Action_probe
  | Action_sync_import
  | Action_brief_build;
export type ActionType = Action["type"];

export interface DispatchRequest {
  schema: "waypoint.dispatch_request.v1";
  id: string;
  project: string;
  node: string;
  route: DispatchRoute;
  agent?: string;
  model?: string;
  mode?: RunMode;
  brief_path?: string;
  created_at: string;
  requested_by: string;
}
export interface DispatchReceipt {
  schema: "waypoint.dispatch_receipt.v1";
  request_id: string;
  project: string;
  node: string;
  route: DispatchRoute;
  status: "started" | "finished" | "failed" | "refused";
  at: string;
  session_id?: string;
  pid?: number;
  detail?: string;
}
