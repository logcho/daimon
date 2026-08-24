import { useEffect, useRef } from "react";
import { alreadyHandled, classifyLink, linkFromEvent } from "../links";

/**
 * Stops any `<a>` in the app from navigating the app away, and routes it
 * somewhere with a way back instead.
 *
 * One delegated listener rather than an `a` override in each renderer. There
 * are six places that hand markdown to `ReactMarkdown` — the vault preview, the
 * chat transcript, both ask prompts, the skills viewer, the phone's notes — and
 * every one of them emitted a bare `<a href>`. Catching the click above all of
 * them fixes the six together, and keeps the seventh (whenever it is written)
 * from reintroducing the bug.
 *
 * Capture phase, on `document`: it runs before any handler on the anchor
 * itself, so the decision is made before anything else reacts.
 */
export function useLinkInterception(
  /** An `http(s)` link — show it in the in-app viewer. */
  onView: (url: string) => void,
  /** A `mailto:`/`tel:` link — hand it to the OS. */
  onExternal: (url: string) => void,
) {
  // Read through refs so the listener registers exactly once, rather than
  // tearing down on every render of a parent that passes fresh closures.
  const viewRef = useRef(onView);
  viewRef.current = onView;
  const externalRef = useRef(onExternal);
  externalRef.current = onExternal;

  useEffect(() => {
    function handler(event: MouseEvent) {
      if (alreadyHandled(event)) return;
      const anchor = linkFromEvent(event);
      if (!anchor) return;

      // `getAttribute` rather than `.href`: the property is already resolved
      // against the app's own origin, so a relative in-vault target arrives as
      // `tauri://localhost/other.md` with the relativeness lost.
      const action = classifyLink(anchor.getAttribute("href") ?? "");
      if (action.kind === "ignore") return;

      // Past this point nothing navigates the top frame, so the browser's
      // default must not either.
      event.preventDefault();

      if (action.kind === "view") viewRef.current(action.url);
      else if (action.kind === "external") externalRef.current(action.url);
      // `vault` stops here. Only the surface that knows which note is open can
      // resolve a relative target, and MarkdownView does that itself before the
      // click ever reaches this listener. Anywhere else it is a link that does
      // nothing — which is the whole improvement over one that eats the app.
    }

    document.addEventListener("click", handler, { capture: true });
    return () => document.removeEventListener("click", handler, { capture: true });
  }, []);
}
