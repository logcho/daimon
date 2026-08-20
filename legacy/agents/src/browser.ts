import { randomUUID } from "node:crypto";
import { copyFile } from "node:fs/promises";
import path from "node:path";

// PinchTab (github.com/pinchtab/pinchtab) drives a real, stealth-patched
// Chrome instance over a local HTTP API instead of vanilla Playwright —
// Playwright's zero-mitigation headless Chromium was getting flagged by bot
// detection on target sites (job application forms, the exact thing this
// product exists to automate). PinchTab patches navigator.webdriver, spoofs
// UA, and humanizes input by default, even in headless mode. It boots as a
// native OS process this session's own workspace.rs spawns and supervises
// directly (no more Docker container — see workspace.rs's module doc),
// bound to loopback only, on a port allocated per-session rather than a
// single fixed one (concurrent sessions no longer get a container network
// namespace each to make a shared fixed port safe) — so, unlike the old
// single-container version, this can't be a hardcoded constant; it's passed
// in via `PINCHTAB_BASE` at spawn time, same pattern as the existing
// `DAIMON_VAULT_DIR`-style env vars below.
const PINCHTAB_BASE = process.env.PINCHTAB_BASE ?? "http://127.0.0.1:9867";
const PINCHTAB_TOKEN = process.env.PINCHTAB_TOKEN;

// Previously a bind-mounted container path; native processes have no mount
// namespace to rely on, so this is now a plain host directory passed in at
// spawn time (see workspace.rs's `DAIMON_RECORDINGS_DIR`), same pattern as
// `vault.ts`'s `DAIMON_VAULT_DIR`. The literal container-path default is
// kept only as a fallback for a stray direct invocation with no env set.
const RECORDINGS_DIR = process.env.DAIMON_RECORDINGS_DIR ?? "/workspace/recordings";

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// A stuck PinchTab call (confirmed empirically: /record/stop on a tab that
// was actively decoding video hung indefinitely — PinchTab reported
// "state":"stopping" with 0 frames captured, seemingly forever) previously
// had nothing bounding it, since `fetch` has no default timeout. That let a
// single hung call block the *entire* agent turn until run.ts's own 60s
// "no output" inactivity watchdog killed everything — not just the
// recording attempt, but any browsing progress already made this turn too.
// 20s is comfortably above every normal PinchTab call's real latency
// (navigate/click/screenshot/text/snapshot are all sub-second in practice)
// while still resolving well before that 60s outer watchdog would otherwise
// be the only thing standing between a stuck call and the whole turn dying.
const PINCHTAB_REQUEST_TIMEOUT_MS = 20_000;

// Belt-and-suspenders on top of `PINCHTAB_REQUEST_TIMEOUT_MS`: confirmed
// live against a real stuck session (a genuine `/record/stop` call against
// an actively-playing YouTube video tab) that PinchTab's recorder can wedge
// itself server-side — `/record/status` kept reporting `"state":"stopping"`
// with 0 frames minutes later, unrecovered — and that this reliably ran the
// whole turn out to `run.ts`'s full 60s inactivity ceiling rather than
// resolving within `PINCHTAB_REQUEST_TIMEOUT_MS`'s own 20s as intended.
// `AbortSignal.timeout` is supposed to bound every individual `fetch` call
// on its own, but evidently isn't enough by itself for whatever this
// specific hang shape is — so wrap the *whole* recording-stop sequence
// (`finishRecording`/`resetRecordingForNewTurn`) in a second, independent
// `setTimeout`-based ceiling that doesn't depend on fetch/AbortSignal
// internals at all, guaranteeing those functions always settle in bounded
// time regardless of what PinchTab's server does.
const RECORDING_OPERATION_TIMEOUT_MS = 25_000;

function withTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}

// Thrown by `pinchtabFetch` instead of a plain `Error` so callers can branch
// on the real HTTP status (see `isTabNotFoundError` below) instead of
// string-matching PinchTab's error text.
class PinchTabHttpError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "PinchTabHttpError";
  }
}

// True for PinchTab's `404 {"error":"tab not found"}` response — the shape
// it returns for any `/tabs/<id>/...` call against a tab that no longer
// exists. The one place this actually happens in practice: `workspace.rs`'s
// memory watchdog recycles the whole PinchTab+Chrome process tree out from
// under this still-running Node process once RSS crosses its soft
// threshold, deliberately without telling Node — see `withContentTab`/
// `withSearchTab` below, which use this to detect and recover from exactly
// that.
function isTabNotFoundError(err: unknown): boolean {
  return err instanceof PinchTabHttpError && err.status === 404;
}

// Thin wrapper around PinchTab's HTTP API — plain `fetch` (Node 22 native),
// no official SDK exists. Throws with PinchTab's own error message on a
// non-2xx response, so a bad ref/selector or a blocked navigation surfaces
// as a normal JS error the tool call sites already know how to handle.
async function pinchtabFetch<T = unknown>(method: string, urlPath: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (PINCHTAB_TOKEN) headers.Authorization = `Bearer ${PINCHTAB_TOKEN}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const res = await fetch(`${PINCHTAB_BASE}${urlPath}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(PINCHTAB_REQUEST_TIMEOUT_MS),
  });

  const raw = await res.text();
  let data: unknown = {};
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch {
      data = raw;
    }
  }

  if (!res.ok) {
    const message =
      typeof data === "object" && data !== null && "error" in data
        ? String((data as { error: unknown }).error)
        : raw;
    throw new PinchTabHttpError(`PinchTab ${method} ${urlPath} failed (${res.status}): ${message}`, res.status);
  }

  return data as T;
}

interface NavigateResponse {
  tabId: string;
  title?: string;
  url?: string;
}

// The tab the agent actually browses/reads/fills — kept on a separate tab
// from `search()`'s tab below. These used to share a single page, which
// meant every `web_search` call silently navigated away from whatever page
// `open_url` had just loaded. If the model then called `read_page` (an easy
// ordering mistake — it takes no argument, so nothing about the call itself
// signals "read *which* page"), it got DuckDuckGo's own results page back
// instead of the page it actually meant to read, which reads as "nothing
// changed" and reliably drives exactly the web_search -> read_page ->
// open_url loop this was reported causing. Splitting the two means
// `web_search` can run any number of times without ever disturbing what
// `read_page`/`click`/`fill` are currently looking at.
//
// Created lazily on first use, exactly like the old Playwright
// context/page — a fresh `about:blank` tab via PinchTab's own `/navigate`,
// not a real page load. Immediately starts recording on that tab (best
// effort — see `startRecording` below) so `finish_recording` always has
// something to save regardless of whether the model ever explicitly asked
// for one, matching the old Playwright version's context-level
// `recordVideo` being on unconditionally from the moment the context
// existed.
let contentTabIdPromise: Promise<string> | null = null;
let searchTabIdPromise: Promise<string> | null = null;

async function openBlankTab(): Promise<string> {
  const res = await pinchtabFetch<NavigateResponse>("POST", "/navigate", {
    url: "about:blank",
    newTab: true,
  });
  return res.tabId;
}

async function startRecording(tabId: string): Promise<void> {
  try {
    await pinchtabFetch("POST", "/record/start", { format: "webm", fps: 5, quality: 80, tabId });
  } catch (err) {
    // Best-effort, matching `run.ts`'s live-frame poll and the tool-facing
    // contract of finish_recording ("no recording in progress" is a normal,
    // reportable outcome, not a crash) — a failed recording start should
    // never break browsing itself.
    console.error(`[daimon-agent] failed to start PinchTab recording: ${err}`);
  }
}

// Called once at the start of every turn (see `run.ts`'s `runTurn`) so a
// `finish_recording` call later in *this* turn never sweeps up footage from
// an earlier turn the user didn't ask to record. Without this, recording
// runs continuously from the moment the content tab was first created
// (session start) or the last explicit `finish_recording` call, straight
// through every turn in between — `finish_recording`'s own tool description
// promises "everything ... so far this turn," which was never actually true
// before this reset existed.
//
// Uses PinchTab's `discard: true` stop — confirmed against its real source
// (internal/handlers/record_handlers.go's HandleRecordStop) that this drops
// buffered frames with no encoding, unlike the full save path
// `finishRecording` uses — so this is cheap enough to run unconditionally on
// every turn, not just ones that end up asking to record. `HandleRecordStart`
// 409s if called while already recording, so this has to be stop-then-start,
// not just start.
//
// No-ops if no content tab exists yet this session — the first real browse
// action this turn will create one via `getContentTabId` below, which
// already starts a clean recording at that point. Best-effort throughout,
// matching `startRecording`'s own contract: a failure here (e.g. the tab is
// stale from a PinchTab recycle) should never break the turn — the next real
// content-tab call self-heals via `withContentTab` regardless.
export async function resetRecordingForNewTurn(): Promise<void> {
  if (!contentTabIdPromise) return;
  const tabId = await contentTabIdPromise;

  const doReset = async () => {
    try {
      await pinchtabFetch("POST", "/record/stop", { discard: true });
    } catch (err) {
      // Not fatal to the reset — e.g. PinchTab 400s with "no frames captured"
      // for a genuinely idle/static tab (confirmed empirically: a screencast
      // with nothing to repaint can sit at zero frames indefinitely), or the
      // tab is stale from a PinchTab recycle. Either way there's nothing to
      // discard; `startRecording` below still needs to run regardless so this
      // turn isn't left with recording permanently off.
      console.error(`[daimon-agent] discard-stop before new turn found nothing to reset: ${err}`);
    }
    await startRecording(tabId);
  };

  // See `RECORDING_OPERATION_TIMEOUT_MS`'s doc comment — this runs
  // unconditionally at the start of every turn, so it must never be able to
  // stall a turn on its own even if PinchTab's recorder is wedged.
  try {
    await withTimeout(doReset(), RECORDING_OPERATION_TIMEOUT_MS, "resetRecordingForNewTurn");
  } catch (err) {
    console.error(`[daimon-agent] ${err}`);
  }
}

function getContentTabId(): Promise<string> {
  if (!contentTabIdPromise) {
    contentTabIdPromise = openBlankTab().then(async (tabId) => {
      await startRecording(tabId);
      return tabId;
    });
  }
  return contentTabIdPromise;
}

function getSearchTabId(): Promise<string> {
  if (!searchTabIdPromise) {
    searchTabIdPromise = openBlankTab();
  }
  return searchTabIdPromise;
}

// Runs `op` against the current content tab; if PinchTab reports that tab
// gone (see `isTabNotFoundError`) — the signature of a mid-session PinchTab
// recycle, not a genuine action failure — drops the stale cache, opens a
// fresh tab (which also restarts recording, see `getContentTabId` above),
// and retries `op` exactly once against it. Every content-tab-scoped
// PinchTab call should go through this rather than calling
// `getContentTabId()` directly, so a recycle mid-session self-heals within
// one call instead of leaving every subsequent open_url/read_page/click/
// fill/screenshot silently broken for the rest of the session.
async function withContentTab<T>(op: (tabId: string) => Promise<T>): Promise<T> {
  const tabId = await getContentTabId();
  try {
    return await op(tabId);
  } catch (err) {
    if (!isTabNotFoundError(err)) throw err;
    console.error(`[daimon-agent] content tab ${tabId} is gone (likely a PinchTab recycle) — recreating`);
    contentTabIdPromise = null;
    const freshTabId = await getContentTabId();
    return op(freshTabId);
  }
}

// Same recovery, for `search()`'s separate tab — see `withContentTab`.
async function withSearchTab<T>(op: (tabId: string) => Promise<T>): Promise<T> {
  const tabId = await getSearchTabId();
  try {
    return await op(tabId);
  } catch (err) {
    if (!isTabNotFoundError(err)) throw err;
    console.error(`[daimon-agent] search tab ${tabId} is gone (likely a PinchTab recycle) — recreating`);
    searchTabIdPromise = null;
    const freshTabId = await getSearchTabId();
    return op(freshTabId);
  }
}

export async function openUrl(url: string): Promise<string> {
  return withContentTab(async (tabId) => {
    const res = await pinchtabFetch<NavigateResponse>("POST", `/tabs/${tabId}/navigate`, { url });
    return res.title ?? "";
  });
}

export interface PageElement {
  ref: string;
  role: string;
  label: string;
}

export interface PageContent {
  url: string;
  text: string;
  elements: PageElement[];
}

interface TextResponse {
  url?: string;
  text?: string;
}

interface SnapshotNode {
  ref: string;
  role: string;
  name: string;
}

interface SnapshotResponse {
  nodes?: SnapshotNode[];
}

// Replaces the old getPageText() + currentUrl() pair — PinchTab keeps those
// as two separate calls too (`/text`, `/snapshot`), so both are fetched
// together and combined into the one shape `read_page` (tools.ts) needs:
// page text plus the ref-addressable interactive elements that
// `click`/`fill_field` now require instead of a CSS selector.
export async function readPage(): Promise<PageContent> {
  return withContentTab(async (tabId) => {
    const [textRes, snapRes] = await Promise.all([
      // `mode=raw` for document.body.innerText (matches the old
      // page.innerText("body") behavior exactly, not PinchTab's default
      // Readability-style extraction, which would drop non-article UI like
      // form fields and buttons the agent still needs to see).
      // `maxChars=4000` mirrors the old code's own truncation, done
      // server-side instead of after the fact.
      pinchtabFetch<TextResponse>("GET", `/tabs/${tabId}/text?mode=raw&maxChars=4000`),
      // `maxTokens` bounds response size on pages with a large number of
      // interactive elements, the same purpose PinchTab's own CLI example
      // (`pinchtab snap --max-tokens 2000`) uses it for.
      pinchtabFetch<SnapshotResponse>("GET", `/tabs/${tabId}/snapshot?filter=interactive&maxTokens=2000`),
    ]);

    const elements: PageElement[] = (snapRes.nodes ?? []).map((node) => ({
      ref: node.ref,
      role: node.role,
      label: node.name,
    }));

    return { url: textRes.url ?? "", text: textRes.text ?? "", elements };
  });
}

// `waitNav: true` on both actions below — confirmed empirically against a
// live instance, not documented as required: *without* it, an action that
// happens to trigger navigation (a link click, a form submit-on-enter) is
// flaky rather than just "doesn't wait" — it either 500s with "unexpected
// page navigation" or silently eats an extra ~1s while PinchTab's own
// ref-recovery machinery salvages it. With `waitNav: true` a navigating
// action succeeds cleanly and an action that *doesn't* navigate returns
// immediately with no added latency (also confirmed empirically) — so
// there's no downside to always setting it, only a downside to omitting it.
export async function click(ref: string): Promise<void> {
  await withContentTab(async (tabId) => {
    await pinchtabFetch("POST", `/tabs/${tabId}/action`, { kind: "click", ref, waitNav: true });
  });
}

export async function fill(ref: string, text: string): Promise<void> {
  await withContentTab(async (tabId) => {
    await pinchtabFetch("POST", `/tabs/${tabId}/action`, { kind: "fill", ref, text, waitNav: true });
  });
}

interface ScreenshotResponse {
  base64: string;
}

export async function screenshotBase64(): Promise<string> {
  return withContentTab(async (tabId) => {
    // `format=png` — the frontend's live-view (src/components/ScreenPanel.tsx)
    // hardcodes `data:image/png;base64,...`, so this has to stay PNG to keep
    // that contract; nothing about the frontend changed as part of this
    // migration.
    const res = await pinchtabFetch<ScreenshotResponse>("GET", `/tabs/${tabId}/screenshot?format=png`);
    return res.base64;
  });
}

interface RecordStatusResponse {
  active: boolean;
  state: string;
  outputPath?: string;
}

// Stops recording the content tab's session, finalizes its .webm file, and
// returns the (in-container, RECORDINGS_DIR) path — or `null` if nothing
// was ever opened yet (no content tab, and therefore no recording, exists),
// or if PinchTab reports no active recording (e.g. it silently hit its own
// ~600-frame/~2min cap — see the "capped clips, no stitching" decision this
// migration made; there's no chunk+stitch logic here by design).
//
// Unlike the old Playwright version, this does *not* need to close and
// recreate the content tab to finalize a recording — PinchTab's
// record/start+stop is independent of tab lifecycle, so browsing state
// (current URL, page) survives a finish_recording call untouched, which is
// actually simpler than Playwright's context-teardown requirement.
export async function finishRecording(): Promise<string | null> {
  if (!contentTabIdPromise) return null;
  const tabId = await contentTabIdPromise;

  const doFinish = async (): Promise<string | null> => {
    let stopped: { path?: string } | null = null;
    try {
      stopped = await pinchtabFetch("POST", "/record/stop");
    } catch {
      // No active recording (e.g. it never started, or already hit its cap
      // and self-terminated) — a normal, reportable outcome, not an error.
      return null;
    }
    if (!stopped) return null;

    // `/record/stop` only *starts* encoding for webm/mp4 (piped through
    // ffmpeg) — confirmed empirically against a live instance: its own
    // response is `{"status":"encoding",...}`, not finished bytes. Poll
    // `/record/status` until PinchTab reports the file is actually done.
    let outputPath: string | undefined;
    for (let attempt = 0; attempt < 30; attempt++) {
      const status = await pinchtabFetch<RecordStatusResponse>("GET", "/record/status");
      if (status.state === "finished" && status.outputPath) {
        outputPath = status.outputPath;
        break;
      }
      if (status.state === "error") return null;
      await sleep(500);
    }
    if (!outputPath) return null;

    const filename = `${Date.now()}-${randomUUID()}.webm`;
    const destPath = path.join(RECORDINGS_DIR, filename);
    // Both PinchTab and this Node process live in the same container, so the
    // encoded file at PinchTab's own state-dir path is a plain local copy —
    // no HTTP transfer needed once we know where it landed. `/record/status`
    // reporting `state: "finished"` with an `outputPath` doesn't guarantee the
    // file is actually on disk yet at that instant — confirmed empirically:
    // the very first copy attempt right after "finished" can still ENOENT,
    // with the file appearing moments later (presumably PinchTab reports
    // "finished" the moment its own encode goroutine completes, fractionally
    // before the OS-level rename/flush that makes it visible). Retry a few
    // times with a short backoff rather than treating one ENOENT here as "no
    // recording" — that's a false negative, not PinchTab reporting no video
    // ever existed.
    for (let attempt = 0; ; attempt++) {
      try {
        await copyFile(outputPath, destPath);
        break;
      } catch (err) {
        const isEnoent = err instanceof Error && "code" in err && err.code === "ENOENT";
        if (!isEnoent || attempt >= 5) throw err;
        await sleep(200);
      }
    }

    // Immediately start a fresh recording so anything that happens after this
    // call keeps being captured, mirroring the old version's "closing finishes
    // the old recording, the next action transparently starts a new one."
    await startRecording(tabId);

    return destPath;
  };

  // See `RECORDING_OPERATION_TIMEOUT_MS`'s doc comment — confirmed live
  // against a real stuck session that this whole sequence can otherwise hang
  // well past `PINCHTAB_REQUEST_TIMEOUT_MS`'s own per-request bound. On
  // timeout, report "no recording" rather than let the turn stall — the same
  // outcome as PinchTab genuinely having nothing to save, just reached by a
  // different path.
  try {
    return await withTimeout(doFinish(), RECORDING_OPERATION_TIMEOUT_MS, "finishRecording");
  } catch (err) {
    console.error(`[daimon-agent] ${err}`);
    return null;
  }
}

export interface SearchResult {
  title: string;
  url: string;
  snippet: string;
}

// DuckDuckGo's HTML-only endpoint (no JS/API key required) sometimes wraps
// result hrefs in an internal redirect (`//duckduckgo.com/l/?uddg=<encoded>`)
// rather than linking straight to the target — unwrap that so the agent gets
// a URL it can actually `open_url` directly.
function resolveResultUrl(href: string): string {
  try {
    const absolute = href.startsWith("//") ? `https:${href}` : href;
    const parsed = new URL(absolute, "https://duckduckgo.com");
    const wrapped = parsed.searchParams.get("uddg");
    return wrapped ? decodeURIComponent(wrapped) : absolute;
  } catch {
    return href;
  }
}

// PinchTab's `/snapshot` returns accessibility-tree refs (role + visible
// name) but, confirmed against a live instance, no `href` attribute on link
// nodes — snapshots aren't meant to carry raw DOM attributes. Getting a
// clickable, "open directly" URL out of a search result therefore needs a
// different extraction strategy than the old Playwright `$$eval(".result__a",
// ...)`, which read `href` straight off the DOM: fetch each result's href
// individually via PinchTab's ungated `GET /tabs/<id>/attr` (reads one named
// HTML attribute by ref/selector) instead of trying to force it out of
// `/snapshot` or `/text`, and enable `security.allowEvaluate` for a
// single-purpose JS eval isn't worth the widened attack surface it's gated
// behind when `/attr` already does exactly this.
//
// html.duckduckgo.com's markup (confirmed against a live search) repeats a
// fixed 5-node group per result within its `#links` container: a heading,
// the title link, an icon-only link, the displayed-domain link, and —
// notably — the snippet itself, also wrapped in its own `<a>` (not a plain
// text node). Group boundaries are detected by role rather than a fixed
// offset, so a result missing one of the non-title links (no icon, no
// separate domain link) doesn't misalign the rest of the page's groups.
async function extractSearchResults(tabId: string): Promise<SearchResult[]> {
  const snap = await pinchtabFetch<SnapshotResponse>(
    "GET",
    `/tabs/${tabId}/snapshot?filter=interactive&selector=${encodeURIComponent("#links")}`,
  );
  const nodes = snap.nodes ?? [];
  const results: SearchResult[] = [];

  for (let i = 0; i < nodes.length && results.length < 8; i++) {
    if (nodes[i].role !== "heading") continue;
    const titleLink = nodes[i + 1];
    if (!titleLink || titleLink.role !== "link" || !titleLink.name) continue;

    // The snippet is the last non-empty link before the next heading (or
    // the end of the list) — the icon-only link and the displayed-domain
    // link both come before it in document order, so taking the last match
    // naturally lands on the actual snippet text.
    let snippet = "";
    let j = i + 2;
    while (j < nodes.length && nodes[j].role !== "heading") {
      if (nodes[j].role === "link" && nodes[j].name) snippet = nodes[j].name;
      j++;
    }

    let url = "";
    try {
      const attr = await pinchtabFetch<{ value: string | null }>(
        "GET",
        `/tabs/${tabId}/attr?selector=${encodeURIComponent(titleLink.ref)}&name=href`,
      );
      if (attr.value) url = resolveResultUrl(attr.value);
    } catch {
      // Leave url empty rather than dropping an otherwise-useful result —
      // the model still gets the title/snippet to reason about.
    }

    results.push({ title: titleLink.name, url, snippet });
    i = j - 1;
  }

  return results;
}

export async function search(query: string): Promise<SearchResult[]> {
  return withSearchTab(async (tabId) => {
    const url = `https://html.duckduckgo.com/html/?q=${encodeURIComponent(query)}`;
    await pinchtabFetch<NavigateResponse>("POST", `/tabs/${tabId}/navigate`, { url });
    return extractSearchResults(tabId);
  });
}
