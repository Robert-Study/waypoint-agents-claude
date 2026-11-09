// Action queue writer: mod and web call enqueueAction instead of hand-writing actions/<id>.json.
import type { Action, ActionType } from "./types_ops_queue.ts";
import { OWNER_ONLY_ACTION_TYPES } from "./types_ops_queue.ts";
import type { FsAdapter } from "./fs-adapter.ts";
import { atomicWrite, joinPath } from "./io.ts";

/** Input: an Action without the fields the library fills in (id is optional). */
export type NewAction = {
  [T in ActionType]: Omit<Extract<Action, { type: T }>, "schema" | "id" | "status" | "created_at"> & { id?: string };
}[ActionType];

export interface EnqueueOptions {
  /** Data folder root; the action lands at <root>/actions/<id>.json. */
  root: string;
  now?: () => Date;
  makeId?: () => string;
}
export class ActionRefused extends Error {
  code = "action_refused" as const;
  constructor(message: string) {
    super(message);
    this.name = "ActionRefused";
  }
}

const SECRETISH = /key|token|secret|pass|credential/i;
const rand = () => Math.random().toString(36).slice(2, 8).padEnd(6, "0");

function checkKeys(v: unknown, path: string): void {
  if (Array.isArray(v)) v.forEach((x, i) => checkKeys(x, `${path}[${i}]`));
  else if (v && typeof v === "object")
    for (const [k, x] of Object.entries(v)) {
      if (SECRETISH.test(k)) throw new ActionRefused(`${path}.${k}: a payload never carries a key, token, secret or password`);
      checkKeys(x, `${path}.${k}`);
    }
}

/** Validate and write one queued action file; returns the stored action. Owner-only types need by = "owner". */
export async function enqueueAction(fs: FsAdapter, action: NewAction, o: EnqueueOptions): Promise<Action> {
  if (!action.by) throw new ActionRefused("by is required");
  if ((OWNER_ONLY_ACTION_TYPES as readonly string[]).includes(action.type) && action.by !== "owner")
    throw new ActionRefused(`${action.type} can only be queued by the owner`);
  checkKeys(action.payload, "payload");
  const t = (o.now ?? (() => new Date()))();
  const id = action.id ?? o.makeId?.() ?? `act-${t.toISOString().replace(/\D/g, "").slice(0, 14)}-${action.type}-${rand()}`;
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(id)) throw new ActionRefused(`bad action id: ${id}`);
  const full = { schema: "waypoint.action.v1", id, type: action.type, status: "queued", payload: action.payload, created_at: t.toISOString(), by: action.by } as Action;
  const path = joinPath(o.root, `actions/${id}.json`);
  if (await fs.exists(path)) throw new ActionRefused(`action ${id} already exists`);
  await atomicWrite(fs, path, JSON.stringify(full, null, 2) + "\n", { writer: action.by });
  return full;
}
