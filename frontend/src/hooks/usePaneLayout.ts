import { useRef, useState } from "react";

export type PaneId = "left" | "center" | "right";

export type PaneLayout = {
  widths: { left: number; right: number };
  visible: Record<PaneId, boolean>;
};

export type LayoutStorage = Pick<Storage, "getItem" | "setItem">;

export const PANE_LAYOUT_KEY = "lemma_pane_layout_v1";

export const DEFAULT_LAYOUT: PaneLayout = {
  widths: { left: 260, right: 420 },
  visible: { left: true, center: true, right: true },
};

export const MIN_WIDTHS: Record<PaneId, number> = { left: 160, center: 280, right: 240 };

/** Stored side-pane widths above this are treated as corrupt. */
export const MAX_STORED_WIDTH = 4000;

/** Width of one resize handle (matches `.lemma-resize-handle`); the grid always renders two. */
export const HANDLE_PX = 4;

function defaultLayout(): PaneLayout {
  return {
    widths: { ...DEFAULT_LAYOUT.widths },
    visible: { ...DEFAULT_LAYOUT.visible },
  };
}

function defaultStorage(): LayoutStorage | undefined {
  try {
    return typeof localStorage === "undefined" ? undefined : localStorage;
  } catch {
    return undefined;
  }
}

function isPositiveFinite(n: unknown): n is number {
  return typeof n === "number" && Number.isFinite(n) && n > 0;
}

export function loadLayout(storage: LayoutStorage | undefined = defaultStorage()): PaneLayout {
  try {
    const raw = storage?.getItem(PANE_LAYOUT_KEY);
    if (!raw) return defaultLayout();
    const p = JSON.parse(raw);
    const w = p?.widths;
    const v = p?.visible;
    if (
      !isPositiveFinite(w?.left) ||
      !isPositiveFinite(w?.right) ||
      typeof v?.left !== "boolean" ||
      typeof v?.center !== "boolean" ||
      typeof v?.right !== "boolean"
    ) {
      return defaultLayout();
    }
    const inRange = (n: number, pane: "left" | "right") => n >= MIN_WIDTHS[pane] && n <= MAX_STORED_WIDTH;
    const visible = { left: v.left, center: v.center, right: v.right };
    if (!visible.left && !visible.center && !visible.right) visible.center = true;
    return {
      widths: {
        left: inRange(w.left, "left") ? w.left : DEFAULT_LAYOUT.widths.left,
        right: inRange(w.right, "right") ? w.right : DEFAULT_LAYOUT.widths.right,
      },
      visible,
    };
  } catch {
    return defaultLayout();
  }
}

export function saveLayout(
  layout: PaneLayout,
  storage: LayoutStorage | undefined = defaultStorage(),
): void {
  try {
    storage?.setItem(PANE_LAYOUT_KEY, JSON.stringify(layout));
  } catch {
    // Quota / privacy-mode errors are ignored; layout simply won't persist.
  }
}

export function canHide(layout: PaneLayout, pane: PaneId): boolean {
  if (!layout.visible[pane]) return true;
  return (["left", "center", "right"] as PaneId[]).some((p) => p !== pane && layout.visible[p]);
}

export function clampLayout(layout: PaneLayout, containerWidth: number): PaneLayout {
  const visible = { ...layout.visible };
  if (!visible.left && !visible.center && !visible.right) visible.center = true;

  const widths = {
    left: Math.max(MIN_WIDTHS.left, layout.widths.left),
    right: Math.max(MIN_WIDTHS.right, layout.widths.right),
  };

  // Every pane the user left visible stays visible. A short window shrinks
  // those panes instead of collapsing one and disabling its toggle.
  if (Number.isFinite(containerWidth)) {
    const shares: { pane: "left" | "center" | "right"; size: number }[] = [];
    if (visible.left) shares.push({ pane: "left", size: widths.left });
    if (visible.center) shares.push({ pane: "center", size: MIN_WIDTHS.center });
    if (visible.right) shares.push({ pane: "right", size: widths.right });
    const preferred = shares.reduce((sum, share) => sum + share.size, 0);
    const chrome = 2 * HANDLE_PX;
    if (preferred > 0 && preferred + chrome > containerWidth) {
      const scale = Math.max(0, containerWidth - chrome) / preferred;
      for (const share of shares) {
        const sized = Math.floor(share.size * scale);
        if (share.pane === "left") widths.left = sized;
        if (share.pane === "right") widths.right = sized;
      }
    }
  }

  return { widths, visible };
}

export function maxWidthFor(layout: PaneLayout, pane: "left" | "right", containerWidth: number): number {
  const visible = clampLayout(layout, containerWidth).visible;
  const other = pane === "left" ? "right" : "left";
  const reserved =
    2 * HANDLE_PX + (visible.center ? MIN_WIDTHS.center : 0) + (visible[other] ? MIN_WIDTHS[other] : 0);
  return Math.max(MIN_WIDTHS[pane], containerWidth - reserved);
}

export function usePaneLayout(containerWidth: number): {
  layout: PaneLayout;
  effective: PaneLayout;
  setWidth: (pane: "left" | "right", px: number) => void;
  /** Grow `pane` by `delta` px (clamped). Returns the delta actually applied. */
  resizeBy: (pane: "left" | "right", delta: number) => number;
  togglePane: (pane: PaneId) => void;
  canHide: (pane: PaneId) => boolean;
} {
  const [layout, setLayout] = useState<PaneLayout>(() => loadLayout());
  // Latest committed layout, readable synchronously so several updates before a re-render compose.
  const layoutRef = useRef<PaneLayout>(layout);
  const containerRef = useRef<number>(containerWidth);
  containerRef.current = containerWidth;

  const update = (fn: (prev: PaneLayout) => PaneLayout) => {
    const prev = layoutRef.current;
    const next = fn(prev);
    if (next === prev) return;
    layoutRef.current = next;
    setLayout(next);
    saveLayout(next);
  };

  const clampedWidth = (prev: PaneLayout, pane: "left" | "right", px: number) =>
    Math.min(maxWidthFor(prev, pane, containerRef.current), Math.max(MIN_WIDTHS[pane], px));

  const effective = clampLayout(layout, containerWidth);

  return {
    layout,
    effective,
    setWidth: (pane, px) =>
      update((prev) => ({
        ...prev,
        widths: { ...prev.widths, [pane]: clampedWidth(prev, pane, px) },
      })),
    resizeBy: (pane, delta) => {
      const before = layoutRef.current.widths[pane];
      update((prev) => ({
        ...prev,
        widths: { ...prev.widths, [pane]: clampedWidth(prev, pane, prev.widths[pane] + delta) },
      }));
      return layoutRef.current.widths[pane] - before;
    },
    togglePane: (pane) =>
      update((prev) => {
        const eff = clampLayout(prev, containerRef.current);
        if (eff.visible[pane] && !canHide(eff, pane)) return prev;
        return { ...prev, visible: { ...prev.visible, [pane]: !prev.visible[pane] } };
      }),
    canHide: (pane) => canHide(effective, pane),
  };
}
