import { chromium, type Browser, type BrowserContext, type Page } from "playwright";

let browserPromise: Promise<Browser> | null = null;
let contentContextPromise: Promise<BrowserContext> | null = null;
let contentPagePromise: Promise<Page> | null = null;
let searchPagePromise: Promise<Page> | null = null;

function getBrowser(): Promise<Browser> {
  if (!browserPromise) {
    browserPromise = chromium.launch({ headless: true });
  }
  return browserPromise;
}

// Bind-mounted into the container at this exact path — see workspace.rs's
// `recordings_mount` — so a finished video actually survives `docker rm -f`
// and lands somewhere watchable on the host, rather than being written to a
// container filesystem layer that disappears the moment the container goes.
const RECORDINGS_DIR = "/workspace/recordings";

// The context (not just page) the agent actually browses/reads/fills in,
// kept separate from `search()`'s page below — see the comment on
// `getContentPage()` for why. A real `BrowserContext`, not the browser's
// implicit default context a bare `browser.newPage()` would use, because
// Playwright's video recording (`recordVideo`) is a context-level option —
// there's no way to turn it on for a single page after the fact. Every page
// opened inside this context has its actions captured to a .webm file,
// finalized only once the context is closed (see `finishRecording` below);
// headless recording needs no extra system dependencies (no ffmpeg, no
// virtual display) — Playwright captures frames via the browser's own
// screencast protocol and muxes the video itself.
function getContentContext(): Promise<BrowserContext> {
  if (!contentContextPromise) {
    contentContextPromise = getBrowser().then((browser) =>
      browser.newContext({
        recordVideo: { dir: RECORDINGS_DIR, size: { width: 1280, height: 720 } },
      }),
    );
  }
  return contentContextPromise;
}

// The page the agent actually browses/reads/fills — kept on a separate tab
// from `search()`'s page below. These used to share a single page, which
// meant every `web_search` call silently navigated away from whatever page
// `open_url` had just loaded. If the model then called `read_page` (an easy
// ordering mistake — it takes no argument, so nothing about the call itself
// signals "read *which* page"), it got DuckDuckGo's own results page back
// instead of the page it actually meant to read, which reads as "nothing
// changed" and reliably drives exactly the web_search -> read_page ->
// open_url loop this was reported causing. Splitting the two means
// `web_search` can run any number of times without ever disturbing what
// `read_page`/`click`/`fill` are currently looking at.
function getContentPage(): Promise<Page> {
  if (!contentPagePromise) {
    contentPagePromise = getContentContext().then((context) => context.newPage());
  }
  return contentPagePromise;
}

function getSearchPage(): Promise<Page> {
  if (!searchPagePromise) {
    searchPagePromise = getBrowser().then((browser) => browser.newPage());
  }
  return searchPagePromise;
}

export async function openUrl(url: string): Promise<string> {
  const page = await getContentPage();
  await page.goto(url, { waitUntil: "domcontentloaded" });
  return page.title();
}

export async function getPageText(): Promise<string> {
  const page = await getContentPage();
  const text = await page.innerText("body");
  return text.slice(0, 4000);
}

export async function click(selector: string): Promise<void> {
  const page = await getContentPage();
  await page.click(selector, { timeout: 5000 });
}

export async function fill(selector: string, value: string): Promise<void> {
  const page = await getContentPage();
  await page.fill(selector, value, { timeout: 5000 });
}

export async function screenshotBase64(): Promise<string> {
  const page = await getContentPage();
  const buffer = await page.screenshot({ type: "png" });
  return buffer.toString("base64");
}

export async function currentUrl(): Promise<string> {
  const page = await getContentPage();
  return page.url();
}

// Stops recording the content context's session, finalizes its .webm file
// into RECORDINGS_DIR, and returns the (in-container) path — or `null` if
// nothing was ever opened yet, since there's no context/recording to finish.
// Closing the context is what actually flushes the video to disk (Playwright
// stitches its captured frames into the final file on close, not
// continuously) — this is the only way to get a *watchable* video out of a
// still-running container's session, since an abrupt `docker rm -f` (no
// graceful shutdown signal reaches the container — see workspace.rs) would
// otherwise leave the recording unfinished or lost entirely.
//
// The content context/page caches are reset to null afterward, so the next
// open_url/read_page/etc. call transparently starts a brand new context (and
// therefore a new recording) rather than reusing — and erroring against —
// the now-closed one.
export async function finishRecording(): Promise<string | null> {
  if (!contentPagePromise || !contentContextPromise) {
    return null;
  }
  const page = await contentPagePromise;
  const context = await contentContextPromise;
  const video = page.video();
  await context.close();
  contentContextPromise = null;
  contentPagePromise = null;
  return video ? video.path() : null;
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

export async function search(query: string): Promise<SearchResult[]> {
  const page = await getSearchPage();
  const url = `https://html.duckduckgo.com/html/?q=${encodeURIComponent(query)}`;
  await page.goto(url, { waitUntil: "domcontentloaded" });

  const rawResults = await page.$$eval(".result__a", (anchors) =>
    anchors.map((a) => ({
      title: a.textContent?.trim() ?? "",
      href: a.getAttribute("href") ?? "",
    })),
  );
  if (rawResults.length === 0) return [];

  const snippets = await page.$$eval(".result__snippet", (els) =>
    els.map((el) => el.textContent?.trim() ?? ""),
  );

  return rawResults.slice(0, 8).map((result, i) => ({
    title: result.title,
    url: resolveResultUrl(result.href),
    snippet: snippets[i] ?? "",
  }));
}
