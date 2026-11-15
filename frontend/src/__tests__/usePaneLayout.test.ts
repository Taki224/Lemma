import React from "react";
import { test, expect, beforeEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import {
  usePaneLayout,
  DEFAULT_LAYOUT,
  PANE_LAYOUT_KEY,
  canHide,
  clampLayout,
  loadLayout,
  saveLayout,
  type PaneLayout,
} from "../hooks/usePaneLayout";

function fakeStorage(initial: Record<string, string> = {}) {
  const data: Record<string, string> = { ...initial };
  return {
    data,
    getItem: (k: string) => (k in data ? data[k] : null),
    setItem: (k: string, v: string) => {
      data[k] = v;
    },
  };
}

test("test_load_defaults_on_missing", () => {
  expect(loadLayout(fakeStorage())).toEqual(DEFAULT_LAYOUT);
  expect(DEFAULT_LAYOUT).toEqual({
    widths: { left: 260, right: 420 },
    visible: { left: true, center: true, right: true },
  });
});

test("test_load_defaults_on_corrupt_json", () => {
  expect(loadLayout(fakeStorage({ [PANE_LAYOUT_KEY]: "{not json" }))).toEqual(DEFAULT_LAYOUT);
  expect(loadLayout(fakeStorage({ [PANE_LAYOUT_KEY]: JSON.stringify({ widths: { left: "x" } }) }))).toEqual(
    DEFAULT_LAYOUT,
  );
  expect(
    loadLayout(
      fakeStorage({
        [PANE_LAYOUT_KEY]: JSON.stringify({
          widths: { left: -5, right: 420 },
          visible: { left: true, center: true, right: true },
        }),
      }),
    ),
  ).toEqual(DEFAULT_LAYOUT);
});

test("test_roundtrip_save_load", () => {
  const s = fakeStorage();
  const layout: PaneLayout = {
    widths: { left: 300, right: 350 },
    visible: { left: false, center: true, right: true },
  };
  saveLayout(layout, s);
  expect(loadLayout(s)).toEqual(layout);
  const throwing = {
    getItem: () => null,
    setItem: () => {
      throw new Error("quota");
    },
  };
  saveLayout(layout, throwing); // must not throw
});

test("test_clamp_enforces_min_widths", () => {
  const out = clampLayout(
    { widths: { left: 50, right: 100 }, visible: { left: true, center: true, right: true } },
    2000,
  );
  expect(out.widths).toEqual({ left: 160, right: 240 });
});

test("test_clamp_never_all_hidden", () => {
  const out = clampLayout(
    { widths: { left: 260, right: 420 }, visible: { left: false, center: false, right: false } },
    2000,
  );
  expect(out.visible).toEqual({ left: false, center: true, right: false });
});

test("test_clamp_keeps_every_pane_visible_on_a_narrow_container", () => {
  const input: PaneLayout = {
    widths: { left: 260, right: 420 },
    visible: { left: true, center: true, right: true },
  };
  // 260 + 280 + 420 + 8 handles = 968. At 600 the panes shrink and stay shown.
  // available 592 / preferred 960.
  const a = clampLayout(input, 600);
  expect(a.visible).toEqual({ left: true, center: true, right: true });
  expect(a.widths).toEqual({ left: 160, right: 259 });
  const b = clampLayout(input, 300);
  expect(b.visible).toEqual({ left: true, center: true, right: true });
  expect(b.widths).toEqual({ left: 79, right: 127 });
  expect(input.visible.right).toBe(true);
  expect(input.widths).toEqual({ left: 260, right: 420 });
});

test("test_canHide_false_for_last_visible", () => {
  const only: PaneLayout = {
    widths: { left: 260, right: 420 },
    visible: { left: false, center: true, right: false },
  };
  expect(canHide(only, "center")).toBe(false);
  expect(canHide(DEFAULT_LAYOUT, "center")).toBe(true);
  expect(canHide(only, "left")).toBe(true);
});

type PaneHook = ReturnType<typeof usePaneLayout>;
let latest: PaneHook | null = null;

function Probe({ width }: { width: number }): React.ReactElement {
  latest = usePaneLayout(width);
  return React.createElement("div", null);
}

function mount(width: number): void {
  latest = null;
  render(React.createElement(Probe, { width }));
}

function stored(): PaneLayout {
  return JSON.parse(localStorage.getItem(PANE_LAYOUT_KEY)!);
}

beforeEach(() => {
  cleanup();
  localStorage.clear();
});

test("test_setWidth_clamps_and_persists", () => {
  mount(2000);
  latest!.setWidth("left", 50);
  expect(latest!.layout.widths.left).toBe(160);
  expect(stored().widths.left).toBe(160);
  latest!.setWidth("right", 500);
  expect(latest!.layout.widths.right).toBe(500);
  expect(stored().widths).toEqual({ left: 160, right: 500 });
});

test("test_toggle_hides_and_shows", () => {
  mount(2000);
  latest!.togglePane("left");
  expect(latest!.layout.visible.left).toBe(false);
  expect(stored().visible.left).toBe(false);
  latest!.togglePane("left");
  expect(latest!.layout.visible.left).toBe(true);
  expect(stored().visible.left).toBe(true);
});

test("test_toggle_last_visible_is_noop", () => {
  mount(2000);
  latest!.togglePane("left");
  latest!.togglePane("right");
  expect(latest!.layout.visible).toEqual({ left: false, center: true, right: false });
  expect(latest!.canHide("center")).toBe(false);
  latest!.togglePane("center");
  expect(latest!.layout.visible).toEqual({ left: false, center: true, right: false });
});

test("test_restores_from_storage_on_mount", () => {
  const saved: PaneLayout = {
    widths: { left: 300, right: 350 },
    visible: { left: false, center: true, right: true },
  };
  localStorage.setItem(PANE_LAYOUT_KEY, JSON.stringify(saved));
  mount(2000);
  expect(latest!.layout).toEqual(saved);
});

test("test_effective_shrinks_widths_without_hiding_when_narrow", () => {
  mount(600);
  expect(latest!.layout.visible).toEqual({ left: true, center: true, right: true });
  expect(latest!.effective.visible).toEqual({ left: true, center: true, right: true });
  expect(latest!.layout.widths).toEqual({ left: 260, right: 420 });
  expect(latest!.effective.widths).toEqual({ left: 160, right: 259 });
});

test("test_load_normalises_all_hidden_and_out_of_range_widths", () => {
  const allHidden = loadLayout(
    fakeStorage({
      [PANE_LAYOUT_KEY]: JSON.stringify({
        widths: { left: 300, right: 350 },
        visible: { left: false, center: false, right: false },
      }),
    }),
  );
  expect(allHidden.visible).toEqual({ left: false, center: true, right: false });
  expect(allHidden.widths).toEqual({ left: 300, right: 350 });

  const outOfRange = loadLayout(
    fakeStorage({
      [PANE_LAYOUT_KEY]: JSON.stringify({
        widths: { left: 10, right: 99999 },
        visible: { left: true, center: true, right: false },
      }),
    }),
  );
  expect(outOfRange.widths).toEqual(DEFAULT_LAYOUT.widths);
  expect(outOfRange.visible).toEqual({ left: true, center: true, right: false });
});

test("test_clamp_counts_two_handle_widths", () => {
  const input: PaneLayout = {
    widths: { left: 260, right: 420 },
    visible: { left: true, center: true, right: false },
  };
  // 260 + 280 + 2*4 = 548: fits at 548. One pixel less shrinks the sidebar and keeps it visible.
  expect(clampLayout(input, 548).widths.left).toBe(260);
  expect(clampLayout(input, 547).visible.left).toBe(true);
  expect(clampLayout(input, 547).widths.left).toBe(259);
});

test("test_setWidth_clamps_to_max_leaving_other_panes_minimums", () => {
  mount(1000);
  latest!.setWidth("left", 5000);
  // 1000 - 2*4 handles - 280 center - 240 right min = 472
  expect(latest!.layout.widths.left).toBe(472);
  latest!.setWidth("right", 5000);
  // 1000 - 8 - 280 - 160 left min = 552
  expect(latest!.layout.widths.right).toBe(552);
});

test("test_setWidth_max_ignores_hidden_panes", () => {
  mount(1000);
  latest!.togglePane("right");
  latest!.togglePane("center");
  latest!.setWidth("left", 5000);
  // Only left visible: 1000 - 8 handles.
  expect(latest!.layout.widths.left).toBe(992);
});

test("test_resizeBy_composes_before_rerender_and_reports_applied", () => {
  mount(2000);
  const hook = latest!; // stale reference: no re-render between calls
  expect(hook.resizeBy("left", 10)).toBe(10);
  expect(hook.resizeBy("left", 15)).toBe(15);
  expect(hook.resizeBy("left", -5)).toBe(-5);
  expect(stored().widths.left).toBe(280);
  // Clamped at the minimum: only the remaining distance is applied.
  expect(hook.resizeBy("left", -1000)).toBe(-120);
  expect(stored().widths.left).toBe(160);
  expect(hook.resizeBy("left", -10)).toBe(0);
});

test("test_narrow_width_keeps_pdf_visible_and_toggle_still_hides_it", () => {
  mount(600);
  expect(latest!.effective.visible.right).toBe(true);
  latest!.togglePane("right");
  expect(latest!.layout.visible.right).toBe(false);
  expect(latest!.effective.visible.right).toBe(false);
  latest!.togglePane("right");
  expect(latest!.effective.visible.right).toBe(true);
});

test("test_canHide_stays_true_when_the_window_is_narrow", () => {
  mount(300);
  expect(latest!.effective.visible).toEqual({ left: true, center: true, right: true });
  expect(latest!.canHide("center")).toBe(true);
  latest!.togglePane("center");
  expect(latest!.layout.visible.center).toBe(false);
});
