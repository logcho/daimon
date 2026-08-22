import { useState } from "react";
import { pair } from "./transport";

/** First run: prove you are the person the machine is showing a code to.
 *
 *  The code is normalised server-side, so the input is free to be forgiving
 *  about case and spacing — it is typed by hand off another screen. */
export function Pair({ onPaired }: { onPaired: () => void }) {
  const [code, setCode] = useState("");
  const [label, setLabel] = useState(defaultLabel());
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const result = await pair(code.trim(), label.trim() || "device");
    setBusy(false);
    if (result.ok) onPaired();
    else setError(result.error ?? "pairing failed");
  }

  return (
    <div className="flex min-h-[100dvh] flex-col items-center justify-center px-6 text-neutral-100">
      <div className="w-full max-w-sm">
        <h1 className="text-2xl font-semibold tracking-tight">Daimon</h1>
        <p className="mt-2 text-sm text-neutral-400">
          Run <code className="rounded bg-white/10 px-1.5 py-0.5 text-neutral-200">daimon-remote</code> on
          your Mac and type the code it prints.
        </p>

        <form onSubmit={submit} className="mt-8 space-y-4">
          <input
            value={code}
            onChange={(e) => setCode(e.target.value.toUpperCase())}
            placeholder="ABCD2345"
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
            inputMode="text"
            className="w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-4 text-center font-mono text-2xl tracking-[0.3em] text-neutral-100 outline-none focus:border-[#4f8dff]/60"
          />
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="this device"
            className="w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm text-neutral-200 outline-none focus:border-[#4f8dff]/60"
          />
          {error && <p className="text-sm text-red-400">{error}</p>}
          <button
            type="submit"
            disabled={busy || code.trim().length < 4}
            className="w-full rounded-2xl bg-[#4f8dff] px-4 py-3.5 font-medium text-white transition active:scale-[0.98] disabled:opacity-40"
          >
            {busy ? "pairing…" : "Pair this device"}
          </button>
        </form>

        <p className="mt-6 text-xs leading-relaxed text-neutral-500">
          The code works once and expires. This device then holds a token of its
          own, which you can revoke from the Mac at any time.
        </p>
      </div>
    </div>
  );
}

function defaultLabel(): string {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return "iPhone";
  if (/iPad/.test(ua)) return "iPad";
  if (/Android/.test(ua)) return "Android";
  return "browser";
}
