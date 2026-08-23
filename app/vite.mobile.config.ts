import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { renameSync } from "fs";
import { resolve } from "path";

// The mobile client is a second entry in this project rather than a project of
// its own, because it reuses `src/sessionEvents.ts`, `src/types.ts` and the
// message-rendering components verbatim — and with no workspace tooling in
// this repo, a sibling package could not import across the boundary.
//
// It builds straight into the Python package so `daimon-remote` can serve it:
// hatchling ships non-Python files under src/daimon_agent, and there is no CI
// to build it on the way past.
const OUT_DIR = resolve(__dirname, "../agents/src/daimon_agent/remote/web");

/** Vite names the emitted html after its input, so the entry has to be called
 *  mobile.html to sit next to index.html without colliding — but what gets
 *  served is the root document of a static directory, which is index.html. */
const emitAsIndex = {
  name: "daimon-emit-as-index",
  closeBundle() {
    renameSync(resolve(OUT_DIR, "mobile.html"), resolve(OUT_DIR, "index.html"));
  },
};

export default defineConfig({
  plugins: [react(), tailwindcss(), emitAsIndex],
  publicDir: resolve(__dirname, "public-mobile"),
  build: {
    outDir: OUT_DIR,
    emptyOutDir: true,
    rollupOptions: {
      input: resolve(__dirname, "mobile.html"),
      // Stable names, no content hash. The built client is committed — the
      // Python package ships it and there is no CI to build it on the way
      // past — so a hashed name means every rebuild adds a fresh ~700 KB blob
      // to history rather than replacing the last one. Thirty-three of them
      // accumulated before this was noticed, and the push that found it
      // failed on payload size.
      //
      // Cache-busting is handled by the gateway instead, which serves these
      // with `no-cache` (see remote/gateway.py): revalidating one small file
      // per load is cheaper than an unbounded repository.
      output: {
        entryFileNames: "assets/mobile.js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name][extname]",
      },
    },
  },
});
