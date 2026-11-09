// Location-neutral path resolver for the Waypoint 1.2 data folder.
// Shared data stores cwd as {machine, rel}; the absolute code root for each machine lives in
// <data root>/machines/<id>.json, which is local-only and git-ignored. Never store an absolute drive path in shared data.
import nodePath from "node:path";

export type Platform = "win32" | "posix";
export type DataRootSource = "env" | "pointer" | "default";
export interface CwdRef { machine: string; rel: string }

export class PathError extends Error {
  code: "unknown_machine" | "bad_rel" | "outside_root" | "bad_machine_file";
  constructor(code: PathError["code"], message: string) {
    super(message);
    this.name = "PathError";
    this.code = code;
  }
}

const P = (platform: Platform) => (platform === "win32" ? nodePath.win32 : nodePath.posix);
const SEP = /[\\/]+/;
const TRAILING = /(.)[\\/]+$/;

export interface ResolveOptions {
  env: Record<string, string | undefined>;
  home: string;
  /** Returns the file's text, or null when it does not exist. */
  readFile: (path: string) => string | null;
  platform?: Platform;
}

/** Order: WAYPOINT_DATA_DIR, then <home>/.waypoint/data-dir (first non-empty line), then <home>/.waypoint/data. */
export function resolveDataRoot(o: ResolveOptions): { root: string; source: DataRootSource } {
  const p = P(o.platform ?? "win32");
  const fromEnv = o.env.WAYPOINT_DATA_DIR?.trim();
  if (fromEnv) return { root: p.normalize(fromEnv).replace(TRAILING, "$1"), source: "env" };
  const text = o.readFile(p.join(o.home, ".waypoint", "data-dir"));
  if (text !== null) {
    const line = text.replace(/^﻿/, "").split(/\r?\n/).map((s) => s.trim()).find((s) => s !== "");
    const val = line?.replace(/^"(.*)"$/, "$1").trim();
    if (val) return { root: p.resolve(o.home, val).replace(TRAILING, "$1"), source: "pointer" };
  }
  return { root: p.join(o.home, ".waypoint", "data"), source: "default" };
}

export function machineFilePath(dataRoot: string, machineId: string, platform: Platform = "win32"): string {
  return P(platform).join(dataRoot, "machines", `${machineId}.json`);
}

/** Reads the code root out of the text of machines/<id>.json. */
export function parseMachineFile(text: string): string {
  let j: any;
  try { j = JSON.parse(text); } catch { throw new PathError("bad_machine_file", "machine file is not valid JSON"); }
  if (!j || typeof j.code_root !== "string" || !j.code_root.trim()) throw new PathError("bad_machine_file", "machine file has no code_root");
  return j.code_root;
}

function checkRel(rel: string): string[] {
  if (/^[A-Za-z]:/.test(rel) || /^[\\/]/.test(rel)) throw new PathError("bad_rel", `rel must be relative: ${rel}`);
  const parts = rel.split(SEP).filter((s) => s !== "" && s !== ".");
  if (parts.includes("..")) throw new PathError("bad_rel", `rel must stay inside the code root: ${rel}`);
  return parts;
}

/** Maps a stored {machine, rel} to a local path. Pure string work: the drive need not exist. */
export function toLocalPath(cwd: CwdRef, machines: Record<string, string>, platform: Platform = "win32"): string {
  const p = P(platform);
  const root = machines[cwd.machine];
  if (root === undefined) throw new PathError("unknown_machine", `no machine file for ${cwd.machine}`);
  const parts = checkRel(cwd.rel);
  return parts.length ? p.join(root, ...parts) : p.normalize(root).replace(TRAILING, "$1");
}

/** Maps an absolute local path to the shareable {machine, rel}. Refuses anything outside the code root. */
export function toCwdRef(machine: string, codeRoot: string, abs: string, platform: Platform = "win32"): CwdRef {
  const p = P(platform);
  const fold = (s: string) => (platform === "win32" ? s.toLowerCase() : s);
  const root = p.resolve(codeRoot);
  const target = p.resolve(abs);
  const rel = p.relative(fold(root), fold(target));
  if (rel === "") return { machine, rel: "" };
  if (rel.startsWith("..") || p.isAbsolute(rel)) throw new PathError("outside_root", `${abs} is outside ${codeRoot}`);
  // keep the original case of the target for the tail
  const tail = target.slice(root.length).replace(/^[\\/]+/, "");
  return { machine, rel: tail.split(SEP).join("/") };
}
