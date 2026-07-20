import { chromium, type Browser, type Page } from "playwright";

let browserPromise: Promise<Browser> | null = null;
let pagePromise: Promise<Page> | null = null;

function getBrowser(): Promise<Browser> {
  if (!browserPromise) {
    browserPromise = chromium.launch({ headless: true });
  }
  return browserPromise;
}

function getPage(): Promise<Page> {
  if (!pagePromise) {
    pagePromise = getBrowser().then((browser) => browser.newPage());
  }
  return pagePromise;
}

export async function openUrl(url: string): Promise<string> {
  const page = await getPage();
  await page.goto(url, { waitUntil: "domcontentloaded" });
  return page.title();
}

export async function getPageText(): Promise<string> {
  const page = await getPage();
  const text = await page.innerText("body");
  return text.slice(0, 4000);
}

export async function click(selector: string): Promise<void> {
  const page = await getPage();
  await page.click(selector, { timeout: 5000 });
}

export async function fill(selector: string, value: string): Promise<void> {
  const page = await getPage();
  await page.fill(selector, value, { timeout: 5000 });
}

export async function screenshotBase64(): Promise<string> {
  const page = await getPage();
  const buffer = await page.screenshot({ type: "png" });
  return buffer.toString("base64");
}

export async function currentUrl(): Promise<string> {
  const page = await getPage();
  return page.url();
}
