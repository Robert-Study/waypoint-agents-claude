// Float and critical path: the ONE computation. Web and mod must not recompute it.
// Pure function, no I/O. Durations are node `estimate` (default 1).
//
// Rules (replayed by fixtures/float.json, also in Python by data-pyclient):
// - Edges are finish-to-start: a node starts when all its deps have finished.
// - Sub-dependencies: a node with children (`parent` field) also waits for all of its children.
// - Done / skipped / carried nodes are zero remaining work: early=late=float=0, critical=false,
//   and they never delay or constrain anything else.
// - A dep id that is not in the graph (deleted) is ignored.
// - A bad estimate (not finite, or negative) counts as 1; 0 is allowed (milestone).
// - A cycle throws an Error whose message starts "cycle:" and lists the node ids.

export interface FloatDep {
  id: string;
  crosses?: string;
}
export interface FloatInput {
  id: string;
  deps?: Array<string | FloatDep>;
  parent?: string | null;
  estimate?: number;
  status?: string;
}
export interface FloatResult {
  id: string;
  early: number; // earliest start
  late: number; // latest start without delaying the project
  float: number; // late - early (total float)
  critical: boolean;
}

const FINISHED = new Set(["done", "skipped", "carried"]);
const EPS = 1e-9;

function duration(n: FloatInput): number {
  if (FINISHED.has(n.status ?? "")) return 0;
  const e = n.estimate;
  return typeof e === "number" && Number.isFinite(e) && e >= 0 ? e : 1;
}

export function computeFloat(nodes: FloatInput[]): FloatResult[] {
  const ids = new Set(nodes.map((n) => n.id));
  const preds = new Map<string, Set<string>>();
  const succs = new Map<string, Set<string>>();
  for (const n of nodes) {
    preds.set(n.id, new Set());
    succs.set(n.id, new Set());
  }
  const edge = (from: string, to: string) => {
    if (!ids.has(from) || !ids.has(to)) return;
    preds.get(to)!.add(from);
    succs.get(from)!.add(to);
  };
  for (const n of nodes) {
    for (const d of n.deps ?? []) edge(typeof d === "string" ? d : d.id, n.id);
    if (n.parent) edge(n.id, n.parent); // child finishes before parent
  }

  // Topological order (Kahn); leftovers are a cycle.
  const indeg = new Map<string, number>();
  for (const n of nodes) indeg.set(n.id, preds.get(n.id)!.size);
  const order: string[] = [];
  const queue = nodes.filter((n) => indeg.get(n.id) === 0).map((n) => n.id);
  while (queue.length) {
    const id = queue.shift()!;
    order.push(id);
    for (const s of succs.get(id)!) {
      indeg.set(s, indeg.get(s)! - 1);
      if (indeg.get(s) === 0) queue.push(s);
    }
  }
  if (order.length !== nodes.length) {
    const stuck = nodes.map((n) => n.id).filter((id) => !order.includes(id));
    throw new Error(`cycle: ${stuck.join(" -> ")}`);
  }

  const byId = new Map(nodes.map((n) => [n.id, n]));
  const done = (id: string) => FINISHED.has(byId.get(id)!.status ?? "");
  const dur = new Map(nodes.map((n) => [n.id, duration(n)]));

  const early = new Map<string, number>();
  const finish = new Map<string, number>();
  let end = 0;
  for (const id of order) {
    let es = 0;
    if (!done(id)) for (const p of preds.get(id)!) es = Math.max(es, finish.get(p)!);
    early.set(id, es);
    finish.set(id, es + dur.get(id)!);
    end = Math.max(end, finish.get(id)!);
  }

  const lateStart = new Map<string, number>();
  for (const id of [...order].reverse()) {
    if (done(id)) {
      lateStart.set(id, 0);
      continue;
    }
    let lf = end;
    for (const s of succs.get(id)!) if (!done(s)) lf = Math.min(lf, lateStart.get(s)!);
    lateStart.set(id, lf - dur.get(id)!);
  }

  return nodes.map((n) => {
    const e = early.get(n.id)!;
    const l = lateStart.get(n.id)!;
    const f = done(n.id) ? 0 : Math.max(0, l - e);
    const clean = Math.abs(f) < EPS ? 0 : f;
    return { id: n.id, early: e, late: clean === 0 ? e : l, float: clean, critical: !done(n.id) && clean === 0 };
  });
}
