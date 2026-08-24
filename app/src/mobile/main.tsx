import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import "../index.css";
import { LinkViewer } from "../components/LinkViewer";
import { useLinkInterception } from "../hooks/useLinkInterception";
import { Pair } from "./Pair";
import { Remote } from "./Remote";
import { storedToken } from "./transport";

function App() {
  const [paired, setPaired] = useState(() => storedToken() !== null);
  // A link in a note used to navigate this whole page, and an installed PWA is
  // `display: standalone` — no back gesture, no address bar, nothing to press
  // (#21). The viewer at least has a ✕.
  const [linkUrl, setLinkUrl] = useState<string | null>(null);
  // `_blank` from a standalone PWA hands off to Safari, which is the only exit
  // that always works — no native command needed on this side.
  const openInBrowser = (url: string) => void window.open(url, "_blank");
  useLinkInterception(setLinkUrl, openInBrowser);

  return (
    <>
      {paired
        ? <Remote onUnauthorized={() => setPaired(false)} />
        : <Pair onPaired={() => setPaired(true)} />}
      {linkUrl && (
        <div className="fixed inset-0 z-40">
          <LinkViewer
            url={linkUrl}
            onClose={() => setLinkUrl(null)}
            onOpenExternally={openInBrowser}
            safeArea
          />
        </div>
      )}
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
