import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import type { BusClient } from "../lib/busClient";
import { decodeBase64, encodeBase64 } from "../lib/busClient";

const THEME = {
  background: "#0a0a0a",
  foreground: "#e5e5e5",
  cursor: "#4f8dff",
  selectionBackground: "rgba(79, 141, 255, 0.35)",
};

/** Keys a phone keyboard has no way to send, and a shell cannot be driven
 *  without. Escape and Ctrl-C alone are the difference between "I can look at
 *  my terminal" and "I can use it". */
const KEYS: [string, string][] = [
  ["esc", "\x1b"],
  ["tab", "\t"],
  ["^C", "\x03"],
  ["^D", "\x04"],
  ["^Z", "\x1a"],
  ["↑", "\x1b[A"],
  ["↓", "\x1b[B"],
  ["←", "\x1b[D"],
  ["→", "\x1b[C"],
];

export function TerminalView({
  bus,
  workspace,
  id,
  onData,
}: {
  bus: BusClient;
  workspace: string;
  id: string;
  /** Registers this view's byte sink with the parent, which owns the one
   *  socket and routes frames by terminal id. */
  onData: (id: string, sink: ((bytes: Uint8Array) => void) | null) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);

  function key(sequence: string) {
    bus.post("term.input", {
      workspace,
      id,
      data: encodeBase64(new TextEncoder().encode(sequence)),
      encoding: "base64",
    });
    termRef.current?.focus();
  }

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const term = new Terminal({
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
      // Small enough that a useful number of columns fits on a phone. The pty
      // takes the smallest attached client's grid, so this is also the size
      // the desktop will be squeezed to while this view is open — deliberate,
      // and the reason detaching restores it.
      fontSize: 11,
      cursorBlink: true,
      theme: THEME,
      allowProposedApi: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(container);
    termRef.current = term;

    onData(id, (bytes) => term.write(bytes));

    const refit = () => {
      if (!container.clientWidth || !container.clientHeight) return;
      term.resize(term.cols, term.rows); // re-measure the cell before fitting
      fit.fit();
      bus.post("term.resize", { workspace, id, cols: term.cols, rows: term.rows });
    };

    const disposable = term.onData((data) => bus.post("term.input", { workspace, id, data }));

    void (async () => {
      const ack = await bus.send("term.attach", { workspace, id, cols: term.cols, rows: term.rows });
      if (!ack.ok) term.write(`\r\n\x1b[31mcould not attach: ${ack.error ?? "unknown"}\x1b[0m\r\n`);
      refit();
    })();

    let timer: ReturnType<typeof setTimeout> | undefined;
    const observer = new ResizeObserver(() => {
      clearTimeout(timer);
      timer = setTimeout(refit, 80);
    });
    observer.observe(container);

    return () => {
      // Detach, never close: the shell is not ours, we are only looking at it.
      bus.post("term.detach", { workspace, id });
      onData(id, null);
      clearTimeout(timer);
      observer.disconnect();
      disposable.dispose();
      term.dispose();
      termRef.current = null;
    };
  }, [bus, workspace, id, onData]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div ref={containerRef} className="min-h-0 flex-1 overflow-hidden px-2 pt-2" />
      <div className="flex gap-1.5 overflow-x-auto border-t border-white/10 px-2 py-2">
        {KEYS.map(([label, sequence]) => (
          <button
            key={label}
            onClick={() => key(sequence)}
            className="shrink-0 rounded-lg border border-white/10 bg-white/5 px-3 py-2 font-mono text-xs text-neutral-200 active:bg-white/15"
          >
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}

export { decodeBase64 };
