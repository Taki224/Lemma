import { useRef, useState } from "react";

export type Corner = "tl" | "tr" | "bl" | "br";

const STORAGE_KEY = "lemma_chat_bubble_v1";
const CORNERS: readonly Corner[] = ["tl", "tr", "bl", "br"];

interface BubbleState {
  minimised: boolean;
  corner: Corner;
}

const DEFAULT_STATE: BubbleState = { minimised: false, corner: "br" };

function load(): BubbleState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw === null) return DEFAULT_STATE;
    const parsed = JSON.parse(raw);
    if (
      parsed &&
      typeof parsed.minimised === "boolean" &&
      CORNERS.includes(parsed.corner)
    ) {
      return { minimised: parsed.minimised, corner: parsed.corner };
    }
  } catch {
    // corrupt data -> defaults
  }
  return DEFAULT_STATE;
}

function save(state: BubbleState): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // storage unavailable: keep in-memory state only
  }
}

export function nearestCorner(
  point: { x: number; y: number },
  bounds: { width: number; height: number },
): Corner {
  const vertical = point.y < bounds.height / 2 ? "t" : "b";
  const horizontal = point.x < bounds.width / 2 ? "l" : "r";
  return `${vertical}${horizontal}` as Corner;
}

export function useChatBubble(): {
  minimised: boolean;
  corner: Corner;
  setMinimised: (b: boolean) => void;
  setCorner: (c: Corner) => void;
} {
  const [state, setState] = useState<BubbleState>(load);

  // Latest state, readable synchronously so the save happens outside the state updater.
  const stateRef = useRef<BubbleState>(state);

  const update = (patch: Partial<BubbleState>) => {
    const next = { ...stateRef.current, ...patch };
    stateRef.current = next;
    setState(next);
    save(next);
  };

  return {
    minimised: state.minimised,
    corner: state.corner,
    setMinimised: (b) => update({ minimised: b }),
    setCorner: (c) => update({ corner: c }),
  };
}
