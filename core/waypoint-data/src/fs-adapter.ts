// FsAdapter: the only file-system surface the data library touches. The mod fills it from $.fs (reads) plus the
// waypoint-fs.mjs helper (atomic write, exclusive create, rename); Node callers use createNodeFsAdapter from node-fs-adapter.ts
// (kept out of this file: the mod's static check refuses a dynamic import() in any file a hooks module loads).
// No static node: import here, so this file stays safe in browser bundles.
export interface FsAdapter {
  readFile(path: string): Promise<string>;
  /** `exclusive` must fail with code EEXIST when the path already exists. */
  writeFile(path: string, data: string, opts?: { exclusive?: boolean }): Promise<void>;
  rename(from: string, to: string): Promise<void>;
  remove(path: string): Promise<void>;
  exists(path: string): Promise<boolean>;
  size(path: string): Promise<number>;
  mkdir(path: string): Promise<void>;
  /** Entry names under dir (an absent folder may yield an empty list). */
  listDir(dir: string): Promise<string[]>;
}

export function errCode(e: unknown): string | undefined {
  return typeof e === "object" && e !== null ? (e as { code?: string }).code : undefined;
}
