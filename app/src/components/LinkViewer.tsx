import { useEffect, useState } from "react";

/** How long to wait before pointing at the browser button.
 *
 *  Long enough that a page which loads normally has already loaded, so the
 *  line reads as an aside rather than a warning about a page that is fine. */
const OFFER_BROWSER_AFTER_MS = 2500;

export interface LinkViewerProps {
  url: string;
  onClose: () => void;
  /** Hand the URL to the OS — a Tauri command on the desktop, a new tab on the
   *  phone. The one thing that always works, whatever the site allows. */
  onOpenExternally: (url: string) => void;
  /** Pad the bottom past the home indicator. Phone only. */
  safeArea?: boolean;
  /** Start a window drag from the bar, the way `Panel`'s own header does.
   *
   *  The viewer covers that header, so without this the window becomes
   *  undraggable for as long as a page is open — an ambient always-on-top
   *  widget you cannot move out of the way. Desktop only; the phone passes
   *  nothing and the bar is inert. */
  onBarDrag?: (event: React.MouseEvent<HTMLElement>) => void;
}

/**
 * A web page shown inside the app, with a way out.
 *
 * The way out is the entire point. Before this existed, a link in a note
 * navigated the app's own webview: the panel, the tree and the pill were
 * replaced by a page with no chrome to escape with, and quitting was the only
 * exit (issue #21).
 *
 * Two details do the real work:
 *
 * 1. **The sandbox omits `allow-top-navigation`.** Without it a framed page can
 *    navigate the *top* frame, which would recreate the original bug from
 *    inside the fix — one click on the wrong page and the app is gone again.
 *    Everything else a normal page needs is granted.
 *
 * 2. **There is no back button.** `history` is not on the cross-origin window
 *    allowlist, so the frame's history is genuinely unreadable and undrivable
 *    from here — a back button would be a dead control. Reload returns to the
 *    link's own URL instead, which is the reachable half of the same intent.
 *
 * And one thing it deliberately does *not* do: claim to know whether the site
 * refused to be framed. Plenty do (`X-Frame-Options`, `frame-ancestors`), and
 * the result is a blank white frame — but a refused frame fires `load` and no
 * `error`, exactly like a successful one, and its document is cross-origin and
 * unreadable. There is no signal to test. So rather than a detector that is
 * silently wrong, the browser button is always present and a neutral line
 * points at it once a working page would already have painted.
 */
export function LinkViewer({
  url,
  onClose,
  onOpenExternally,
  safeArea,
  onBarDrag,
}: LinkViewerProps) {
  // Bumped to remount the iframe. Cheaper and more reliable than reaching into
  // `contentWindow.location`, which is write-only across origins and silently
  // does nothing when the frame is mid-navigation.
  const [epoch, setEpoch] = useState(0);
  const [offerBrowser, setOfferBrowser] = useState(false);

  useEffect(() => {
    setOfferBrowser(false);
    const timer = setTimeout(() => setOfferBrowser(true), OFFER_BROWSER_AFTER_MS);
    return () => clearTimeout(timer);
  }, [url, epoch]);

  let host = url;
  try {
    host = new URL(url).host || url;
  } catch {
    // Shown as-is. The classifier already vetted the scheme; a URL that only
    // `new URL` objects to is still fine to display.
  }

  // The panel's own chip vocabulary (Panel.tsx's view tabs): pill-shaped, xs,
  // tracking-tight, quiet until hovered. Reused verbatim rather than
  // approximated, so the bar reads as the same app one layer down.
  const chip =
    "shrink-0 rounded-full px-2.5 py-1 text-xs font-medium tracking-tight text-neutral-400 " +
    "transition duration-200 hover:bg-white/5 hover:text-neutral-100 active:scale-90";

  return (
    <div className="absolute inset-0 z-30 flex flex-col bg-[#0a0a0a]">
      {/* Same measurements and specular top edge as Panel's header, because it
          is standing in for it — the viewer covers that header while open. */}
      <header
        onMouseDown={onBarDrag}
        className="flex shrink-0 items-center gap-2 border-b border-white/[0.08] px-4 py-3 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.06)]"
      >
        <button type="button" onClick={onClose} title="back to the app (Esc)" className={chip}>
          ‹ back
        </button>
        {/* The host, not the full URL: it is the part that says where you are,
            and the rest would truncate away anyway. Full URL on hover. */}
        <span
          className="min-w-0 flex-1 truncate text-xs tracking-tight text-neutral-500"
          title={url}
        >
          {host}
        </span>
        <button type="button" onClick={() => setEpoch((e) => e + 1)} title="reload" className={chip}>
          reload
        </button>
        <button
          type="button"
          onClick={() => onOpenExternally(url)}
          title="open in your browser"
          className="shrink-0 rounded-full bg-[#4f8dff]/20 px-2.5 py-1 text-xs font-medium tracking-tight text-[#4f8dff] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.12)] transition duration-200 hover:bg-[#4f8dff]/30 active:scale-90"
        >
          open in browser
        </button>
      </header>

      {/* Phrased as a question because it is one: a blank frame here means the
          site refused to be embedded, and that is indistinguishable from a
          page that simply looks empty. */}
      {offerBrowser && (
        <p className="shrink-0 border-b border-white/[0.08] bg-white/[0.02] px-4 py-1.5 text-[11px] tracking-tight text-neutral-500">
          Nothing here? Some sites won't open inside an app —{" "}
          <button
            type="button"
            onClick={() => onOpenExternally(url)}
            className="underline transition hover:text-neutral-300"
          >
            open it in your browser
          </button>
          .
        </p>
      )}

      <iframe
        key={epoch}
        src={url}
        title={url}
        // No `allow-top-navigation` — see the note above; that omission is the
        // fix. `allow-same-origin` only preserves the *page's* own origin, so
        // it can reach its cookies and storage and still cannot touch the app.
        sandbox="allow-scripts allow-forms allow-popups allow-same-origin"
        referrerPolicy="no-referrer"
        className="min-h-0 w-full flex-1 border-0 bg-white"
        style={safeArea ? { paddingBottom: "env(safe-area-inset-bottom)" } : undefined}
      />
    </div>
  );
}
