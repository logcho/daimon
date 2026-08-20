import { useEffect, useState } from "react";

/** Two-step delete: the first click arms, the second confirms. Deleting is not
 *  undoable and the target is a file the agent relies on, so a single stray
 *  click shouldn't do it — and a modal for one button is heavier than the
 *  decision warrants.
 *
 *  Shared by SkillsPanel and VaultPanel, which delete the same way. The arm
 *  step carries more weight than it used to: the row now disappears the instant
 *  it's confirmed, without waiting on the server, so this is the only pause
 *  between the click and the file being gone. */
export function DeleteButton({ name, onDelete }: { name: string; onDelete: () => void }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 3000);
    return () => clearTimeout(t);
  }, [armed]);

  // The button lives in the detail header and is reused across selections, so
  // arming it for one file and then switching would otherwise leave the *next*
  // file one click from deletion.
  useEffect(() => setArmed(false), [name]);

  return (
    <button
      type="button"
      title={armed ? `click again to delete ${name}` : `delete ${name}`}
      onClick={(e) => {
        e.stopPropagation();
        if (armed) onDelete();
        else setArmed(true);
      }}
      className={`shrink-0 rounded-md px-2 py-0.5 text-xs transition ${
        armed
          ? "bg-red-500/20 text-red-300"
          : "text-neutral-600 hover:bg-white/5 hover:text-red-400"
      }`}
    >
      {armed ? "confirm" : "delete"}
    </button>
  );
}
