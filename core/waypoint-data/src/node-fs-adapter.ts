// Node implementation of FsAdapter. Separate from fs-adapter.ts so the mod, which cannot load node: modules, never loads it.
import { errCode, type FsAdapter } from "./fs-adapter.ts";

/** Node implementation. Loads node:fs/promises lazily so browser builds never evaluate it. */
export async function createNodeFsAdapter(): Promise<FsAdapter> {
  const fsp = await import("node:fs/promises");
  return {
    readFile: (p) => fsp.readFile(p, "utf8"),
    writeFile: (p, d, o) => fsp.writeFile(p, d, { encoding: "utf8", flag: o?.exclusive ? "wx" : "w" }),
    rename: (a, b) => fsp.rename(a, b),
    remove: (p) => fsp.rm(p, { force: true }),
    exists: async (p) => {
      try {
        await fsp.stat(p);
        return true;
      } catch {
        return false;
      }
    },
    size: async (p) => (await fsp.stat(p)).size,
    mkdir: async (p) => {
      await fsp.mkdir(p, { recursive: true });
    },
    listDir: async (d) => {
      try {
        return await fsp.readdir(d);
      } catch (e) {
        if (errCode(e) === "ENOENT") return [];
        throw e;
      }
    },
  };
}
