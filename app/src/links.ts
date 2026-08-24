/** What a clicked link should do, decided from its `href` alone.
 *
 *  Every markdown surface in the app renders links through `ReactMarkdown`,
 *  which emits plain `<a href>`. In a `decorations: false` Tauri window and a
 *  `display: standalone` PWA, letting one of those navigate replaces the whole
 *  app with a page that has no back button, no close, and no address bar — the
 *  only way out is quitting. So no anchor is ever allowed to navigate; each one
 *  is classified here and routed somewhere with a way back.
 *
 *  Kept out of the components, and pure, for the same reason `wikilinks.ts` is:
 *  the scheme rules are the part with real edge cases, and they should be
 *  testable without a DOM.
 */

export type LinkAction =
  /** `http(s)` — show it in the in-app viewer, which has a close button. */
  | { kind: "view"; url: string }
  /** `mailto:`/`tel:` — hand to the OS. There is nothing to frame. */
  | { kind: "external"; url: string }
  /** No scheme — a path inside the vault, resolved against the open note. */
  | { kind: "vault"; target: string }
  /** Nothing to do: an in-page anchor, or a scheme we refuse to act on. */
  | { kind: "ignore" };

/** A scheme at the start of the href — `mailto:`, `javascript:`, `asset:`.
 *  The character class excludes `/`, so a relative path that happens to carry
 *  a colon (`notes/a:b.md`) is not mistaken for one. */
const SCHEME_RE = /^([a-z][a-z0-9+.-]*):/i;

/** Schemes that may leave the app, and how. Everything absent from this map is
 *  refused — deny-by-default, because the list of schemes worth *blocking*
 *  (`javascript:`, `data:`, `file:`, `asset:`, `blob:`) is open-ended and the
 *  list worth allowing is four entries long. Note text is not trusted input:
 *  the agent writes notes, and so does anything the agent read.
 */
const ALLOWED: Record<string, LinkAction["kind"]> = {
  http: "view",
  https: "view",
  mailto: "external",
  tel: "external",
};

export function classifyLink(href: string): LinkAction {
  const raw = href.trim();
  // A bare `#section` moves within the page it is already on — not a
  // navigation away, and nothing to intercept.
  if (!raw || raw.startsWith("#")) return { kind: "ignore" };

  // `//example.com` inherits the app's own scheme, which is `tauri:` on the
  // desktop — a dead end. Read it the way a browser on the open web would.
  if (raw.startsWith("//")) return { kind: "view", url: `https:${raw}` };

  const scheme = SCHEME_RE.exec(raw)?.[1].toLowerCase();
  if (!scheme) return { kind: "vault", target: raw };

  const allowed = ALLOWED[scheme];
  if (allowed === "view") return { kind: "view", url: raw };
  if (allowed === "external") return { kind: "external", url: raw };
  return { kind: "ignore" };
}

/** The nearest enclosing link of a click, or `null`.
 *
 *  A click inside a link usually lands on something *within* the anchor — the
 *  `<code>` of `` [`foo`](…) ``, an emphasis span, an image — so the target
 *  element is rarely the `<a>` itself.
 */
export function linkFromEvent(event: MouseEvent): HTMLAnchorElement | null {
  const target = event.target as Element | null;
  if (!target || typeof target.closest !== "function") return null;
  return target.closest("a[href]");
}

/** Whether a click should be left entirely alone.
 *
 *  Modifier-clicks and middle-clicks mean "open somewhere else" everywhere
 *  else in computing, and in this webview they open nothing at all — so they
 *  are not a way out and get treated as ordinary clicks. What this does skip is
 *  a click the page already handled (`defaultPrevented`), so a component that
 *  binds its own anchor keeps its behaviour.
 */
export function alreadyHandled(event: MouseEvent): boolean {
  return event.defaultPrevented || event.button !== 0;
}
