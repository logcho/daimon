---
name: daimon-web-verify
description: Use whenever you've changed frontend/UI code in this repo — either the Tauri app's React frontend (src/) or the landing site (website/) — before saying the work is done. Runs the correct typecheck/build for whichever surface changed and cleans up any dev server it starts. Trigger on "verify", "test the site", "check the build", "does it compile", or right after editing .tsx/.astro/.css files.
---

# Verifying frontend changes in this repo

This repo has **two independent frontend projects** — don't run the wrong one's commands, and don't assume a change to one affects the other.

## If you changed `src/`, `src-tauri/`, or anything under the repo root (the desktop app)

```sh
npm run build          # tsc + vite build, from repo root
cd src-tauri && cargo check
```

If the change touches IPC commands, window config, or the workspace/agent pipeline, that's `cargo test` territory — run it if the change is anywhere near that surface, not just `cargo check`. The real integration tests live in `src-tauri/src/session.rs` and drive `start_session`/`send_message`/`end_session` through Tauri's test harness against a live workspace (PinchTab + the Node agent process).

Those session/automation tests need `pinchtab` resolvable on `PATH`, which a plain dev checkout doesn't have — the sidecar in `src-tauri/binaries/` is only found by a bundled `.app`. Expect 4 failures with ``failed to run `pinchtab config init` `` on a machine without it; that's environmental, not a regression. Every `*_clears_the_acl` test does run, and those are the ones that catch a command registered in `lib.rs` but missing from `build.rs` or `capabilities/default.json`.

If you changed anything under `agents/`, that's its own project: `cd agents && npm run typecheck && npm test`.

Don't launch `npm run tauri dev` to "check" a UI change unless you actually need to see it render — a clean `npm run build` + `cargo check` already proves it compiles. If you do launch it (e.g. to confirm no runtime panic), background it, give it ~8-10s to compile and start, check the log, then kill it:

```sh
(npm run tauri dev > /tmp/daimon-dev.log 2>&1 &)
sleep 10 && tail -15 /tmp/daimon-dev.log
pkill -f "target/debug/daimon"; pkill -f "npm run tauri dev"
```

Check whether port 1420 is already taken before launching — the user may well have their own `tauri dev` running, and killing it to run yours is not your call. `lsof -ti:1420` tells you; if it's occupied, `npm run build` + `cargo check` is enough on its own.

Never leave a dev process or a stray agent server running when you're done (`pkill -f "tsx src/server.ts"` if you started one to exercise `agents/` directly).

## If you changed anything under `website/`

It's a fully separate npm project — `cd website` first, its own `package.json`.

```sh
cd website
npx astro check   # typecheck — catches prop mismatches, missing imports, etc.
npm run build     # astro build; also re-runs astro:assets image optimization
```

To eyeball rendering (not just that it compiles):

```sh
(npm run dev > /tmp/website-dev.log 2>&1 &)
sleep 4
curl -s http://localhost:4321/ -o /tmp/website-page.html
# grep for whatever markup/class you just added to confirm it actually rendered
npx astro dev stop
```

`astro dev stop` is the reliable way to kill it — a background `pkill` can miss it and leave a stale server on port 4321 that silently serves an old build (a `FailedToLoadModuleSSR` error on the next curl is the tell that this already happened — the fix is `astro dev stop` then relaunch, not debugging the "error").

## Don't screenshot the user's live desktop to verify

For the Tauri app specifically: don't take a full-screen `screencapture` to check UI changes — it captures whatever else is on the user's screen too, which is both a privacy overstep and contrary to the whole point of this product (an ambient tool that stays out of the way). Verify via build success, `curl`-ing rendered HTML, log output, or ask the user to look and describe what they see.
