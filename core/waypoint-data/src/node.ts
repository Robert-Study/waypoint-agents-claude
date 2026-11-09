// Node entry: the browser-safe barrel plus the Node filesystem adapter. Node callers import from here;
// the mod and browser builds import index.ts, which never loads node-fs-adapter.ts.
export * from "./index.ts";
export { createNodeFsAdapter } from "./node-fs-adapter.ts";
