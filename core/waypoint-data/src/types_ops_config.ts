// Types for the ops-config schemas in ../schema. Keep in step with the schemas (the check enforces property names).

export type AgentKind = "agent" | "route" | "model" | "addon";

/** agents/<id>.json */
export interface AgentEntry {
  id: string;
  kind: AgentKind;
  enabled: boolean;
  route?: string | null;
  pool?: string | null;
  label?: string;
  note?: string;
}

/** One floor. min_percent null = unset = 0% (use it or lose it). There is no default value. */
export interface FloorEntry {
  min_percent: number | null;
  set_by: string | null;
  set_at: string | null;
}

/** settings/floors.json */
export interface Floors {
  pools: Record<string, FloorEntry>;
}

export type SyncMode = "cloud" | "local";

/** settings/settings.json. Never holds a key or token. */
export interface Settings {
  usage_fresh_minutes: number;
  max_retries: number;
  resume_cold_after_minutes?: number; // idle minutes before an asking session counts as cold; default 60
  sync_mode: Record<string, SyncMode>;
  web_root?: string | null;
  web_url?: string | null;
  gemini_key_slot?: string | null;
  python: string;
  runner_dir?: string;
  model_pick?: { task?: string | null; chat?: string | null; plan?: string | null };
  mute_kinds?: string[];
  chime?: boolean;
  panel_tabs?: Record<string, boolean>;
  mod?: { engineUrl?: string; ownedFilesGuard?: boolean; notifyQuestions?: boolean; workerTools?: boolean };
  demo?: boolean;
}

/** usage/<pool>.json */
export interface UsageReading {
  pool: string;
  remaining_percent: number | null;
  resets_at: string | null;
  read_at: string | null;
  source: string;
}

export type ScheduleMode = "frugal" | "balanced" | "push" | "max";

/** schedules/<id>.json */
export interface Schedule {
  id: string;
  project: string;
  mode: ScheduleMode;
  model?: string | null;
  effort?: "low" | "medium" | "high" | "xhigh" | "max" | null;
  spend_ceiling: number;
  start?: string | null;
  window?: string | null;
  order?: string[];
  last_run?: { slot?: string | null; started_at?: string | null; ended_at?: string | null; result?: string | null };
}

/** health/<route>.json */
export interface RouteHealth {
  route?: string;
  ok: boolean;
  reason: string;
  checked_at: string;
}
