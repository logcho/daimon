import { useEffect, useRef } from "react";
import { register, unregister } from "@tauri-apps/plugin-global-shortcut";

const SHORTCUT = "CommandOrControl+Shift+Space";

export function useHotkeyToggle(onToggle: () => void) {
  const handlerRef = useRef(onToggle);
  handlerRef.current = onToggle;

  useEffect(() => {
    register(SHORTCUT, () => handlerRef.current()).catch((err) => {
      console.error("failed to register global shortcut", err);
    });

    return () => {
      unregister(SHORTCUT).catch(() => {});
    };
  }, []);
}
