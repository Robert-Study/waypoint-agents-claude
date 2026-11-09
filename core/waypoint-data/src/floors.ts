// Usage floor decision and reading freshness. Pure: no I/O, no clock except the `now` argument.
//
// FLOOR DECISION (Owner, 7 Nov 2026, EXPLICIT_OWNER_DECISION): no floor is hard-coded or seeded.
// Floors are settings Owner sets in Waypoint. An UNSET floor means 0% (use it or lose it): the route runs and
// no to-do is raised. Only a floor Owner has set holds a route back.
// A set floor is always enforced against a FRESH reading; STALE or UNKNOWN readings never count as FRESH.
// With an unset floor the reading is not consulted, so stale or unknown usage never blocks the route.

import type { AgentEntry, Settings, Floors, UsageReading } from "./types_ops_config.ts";

export type Freshness = "FRESH" | "STALE" | "UNKNOWN";
export type Reason = "ok" | "disabled" | "floor_unset" | "below_floor" | "stale" | "unknown";

export interface Decision {
  allowed: boolean;
  reason: Reason;
  /** Freshness of the reading that was judged; null when the route was refused before any reading was needed. */
  freshness: Freshness | null;
  /** Decisions to-do text to raise, or null. */
  todo: string | null;
}

/** Route as the rule needs it: an agents/<id>.json entry (id, enabled, pool). */
export type RouteInput = Pick<AgentEntry, "id" | "enabled" | "pool">;
/** Settings plus the floors file. */
export type SettingsInput = Pick<Settings, "usage_fresh_minutes"> & { floors: Floors };

/** Pools that may run with no floor set (free or local). Matched by name or `<name>_` prefix. Everything else with a pool is metered. */
const FREE_POOLS = ["groq", "gemini", "ollama", "freebuff", "github_models"];

const MS_PER_MINUTE = 60000;

export function isFreePool(pool: string | null | undefined): boolean {
  if (!pool) return true; // draws from no pool: free or local
  return FREE_POOLS.some((p) => pool === p || pool.startsWith(p + "_"));
}

function parse(ts: string | null | undefined): number | null {
  if (!ts) return null;
  const t = Date.parse(ts);
  return Number.isNaN(t) ? null : t;
}

/** FRESH: read within usage_fresh_minutes, not from the future, not taken before a reset that has since passed. */
export function classifyReading(
  reading: UsageReading | null | undefined,
  freshMinutes: number,
  now: Date,
): Freshness {
  if (!reading || reading.remaining_percent === null || reading.remaining_percent === undefined) return "UNKNOWN";
  const readAt = parse(reading.read_at);
  if (readAt === null) return "UNKNOWN";
  const nowMs = now.getTime();
  if (readAt > nowMs) return "UNKNOWN";
  if (nowMs - readAt > freshMinutes * MS_PER_MINUTE) return "STALE";
  const resetsAt = parse(reading.resets_at);
  if (resetsAt !== null && resetsAt <= nowMs && readAt < resetsAt) return "STALE";
  return "FRESH";
}

export function decide(
  route: RouteInput,
  settings: SettingsInput,
  reading: UsageReading | null | undefined,
  now: Date,
): Decision {
  if (!route.enabled) return { allowed: false, reason: "disabled", freshness: null, todo: null };

  const pool = route.pool ?? null;
  const own = pool !== null && reading && reading.pool === pool ? reading : null;
  const freshness = classifyReading(own, settings.usage_fresh_minutes, now);
  const floor = pool === null ? null : (settings.floors.pools[pool]?.min_percent ?? null);

  if (floor === null) return { allowed: true, reason: "ok", freshness, todo: null };

  if (freshness === "STALE") return { allowed: false, reason: "stale", freshness, todo: null };
  if (freshness !== "FRESH" || own === null || own.remaining_percent === null) {
    return { allowed: false, reason: "unknown", freshness: "UNKNOWN", todo: null };
  }
  if (own.remaining_percent < floor) return { allowed: false, reason: "below_floor", freshness, todo: null };
  return { allowed: true, reason: "ok", freshness, todo: null };
}
