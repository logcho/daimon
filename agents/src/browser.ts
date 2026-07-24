import { chromium, type Browser, type Page } from "playwright";

let browserPromise: Promise<Browser> | null = null;
let contentPagePromise: Promise<Page> | null = null;
let searchPagePromise: Promise<Page> | null = null;

function getBrowser(): Promise<Browser> {
  if (!browserPromise) {
    browserPromise = chromium.launch({ headless: true });
  }
  return browserPromise;
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
    contentPagePromise = getBrowser().then((browser) => browser.newPage());
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
