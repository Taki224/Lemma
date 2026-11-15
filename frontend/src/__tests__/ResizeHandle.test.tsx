import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ResizeHandle } from "../components/ResizeHandle";

// The fake renderer has no pointer events: invoke the element's React handlers directly.
function pointer(kind: "Down" | "Move" | "Up" | "Cancel", clientX: number, button?: number, clientY = 0) {
  const el = screen.getByRole("separator");
  const handler = el.props[`onPointer${kind}`];
  const captured: Array<[string, number]> = [];
  handler({
    type: `pointer${kind.toLowerCase()}`,
    pointerId: 7,
    clientX,
    clientY,
    button,
    target: el,
    currentTarget: {
      setPointerCapture: (id: number) => captured.push(["set", id]),
      releasePointerCapture: (id: number) => captured.push(["release", id]),
      hasPointerCapture: () => true,
    },
    preventDefault() {},
    stopPropagation() {},
  });
  return captured;
}

function setup(props: Partial<React.ComponentProps<typeof ResizeHandle>> = {}) {
  const onDrag = vi.fn();
  const onToggleCollapse = vi.fn();
  render(
    <ResizeHandle
      onDrag={onDrag}
      onToggleCollapse={onToggleCollapse}
      ariaLabel="Resize editor pane"
      {...props}
    />
  );
  return { onDrag, onToggleCollapse };
}

describe("ResizeHandle", () => {
  it("test_has_separator_role_and_label", () => {
    setup();
    const el = screen.getByRole("separator", { name: "Resize editor pane" });
    expect(el.getAttribute("aria-label")).toBe("Resize editor pane");
    expect(el.getAttribute("aria-orientation")).toBe("vertical");
    expect(el.props.tabIndex).toBe(0);
    expect(String(el.props.className)).toContain("lemma-resize-handle");
  });

  it("test_drag_emits_deltas", () => {
    const { onDrag } = setup();
    const captured = pointer("Down", 100);
    expect(captured).toEqual([["set", 7]]);
    expect(onDrag).toHaveBeenCalledTimes(0);

    pointer("Move", 110);
    pointer("Move", 125);
    pointer("Move", 120);

    expect(onDrag.mock.calls.map((c: any[]) => c[0])).toEqual([10, 15, -5]);
  });

  it("test_keyboard_arrows_emit_step", () => {
    const { onDrag } = setup();
    const el = screen.getByRole("separator");
    fireEvent.keyDown(el, { key: "ArrowLeft" });
    fireEvent.keyDown(el, { key: "ArrowRight" });
    fireEvent.keyDown(el, { key: "ArrowUp" });

    expect(onDrag.mock.calls.map((c: any[]) => c[0])).toEqual([-16, 16]);
  });

  it("test_horizontal_home_and_enter_reset_like_dblclick", () => {
    const { onToggleCollapse, onDrag } = setup({ orientation: "horizontal" });
    const el = screen.getByRole("separator");
    fireEvent.keyDown(el, { key: "Home" });
    fireEvent.keyDown(el, { key: "Enter" });
    expect(onToggleCollapse).toHaveBeenCalledTimes(2);
    expect(onDrag).toHaveBeenCalledTimes(0);
  });

  it("test_vertical_home_and_enter_do_nothing", () => {
    const { onToggleCollapse } = setup();
    const el = screen.getByRole("separator");
    fireEvent.keyDown(el, { key: "Home" });
    fireEvent.keyDown(el, { key: "Enter" });
    expect(onToggleCollapse).toHaveBeenCalledTimes(0);
  });

  it("test_dblclick_toggles_collapse", () => {
    const { onToggleCollapse, onDrag } = setup();
    fireEvent.doubleClick(screen.getByRole("separator"));

    expect(onToggleCollapse).toHaveBeenCalledTimes(1);
    expect(onDrag).toHaveBeenCalledTimes(0);
  });

  it("test_no_events_after_pointerup", () => {
    const { onDrag } = setup();
    pointer("Down", 100);
    pointer("Move", 104);
    const captured = pointer("Up", 104);
    expect(captured).toEqual([["release", 7]]);

    pointer("Move", 150);
    expect(onDrag.mock.calls.map((c: any[]) => c[0])).toEqual([4]);

    // A new drag starts from its own origin, not from the previous one.
    pointer("Down", 300);
    pointer("Move", 302);
    expect(onDrag.mock.calls.map((c: any[]) => c[0])).toEqual([4, 2]);

    pointer("Cancel", 302);
    pointer("Move", 400);
    expect(onDrag).toHaveBeenCalledTimes(2);
  });

  it("test_disabled_emits_nothing", () => {
    const { onDrag, onToggleCollapse } = setup({ disabled: true });
    const el = screen.getByRole("separator");
    expect(el.getAttribute("aria-disabled")).toBe("true");
    expect(el.props.tabIndex).toBe(-1);

    pointer("Down", 100);
    pointer("Move", 120);
    fireEvent.keyDown(el, { key: "ArrowRight" });
    fireEvent.doubleClick(el);

    expect(onDrag).toHaveBeenCalledTimes(0);
    expect(onToggleCollapse).toHaveBeenCalledTimes(0);
  });

  it("test_clamped_drag_does_not_drift", () => {
    // Pane is stuck: onDrag reports that nothing was applied.
    const onDrag = vi.fn(() => 0);
    setup({ onDrag });
    pointer("Down", 100);
    pointer("Move", 150); // pushes 50px into the clamp
    onDrag.mockImplementation((d: number) => d); // reversing is accepted again
    pointer("Move", 140);
    // Origin stayed at 100, so the first applied delta after reversing is 140-100, not -10.
    expect(onDrag.mock.calls.map((c: any[]) => c[0])).toEqual([50, 40]);
  });

  it("test_partial_clamp_advances_origin_by_applied_only", () => {
    const onDrag = vi.fn((d: number) => Math.min(d, 20));
    setup({ onDrag });
    pointer("Down", 100);
    pointer("Move", 150); // applied 20 -> origin 120
    pointer("Move", 130);
    expect(onDrag.mock.calls.map((c: any[]) => c[0])).toEqual([50, 10]);
  });

  it("test_non_primary_button_does_not_start_drag", () => {
    const { onDrag } = setup();
    pointer("Down", 100, 2);
    pointer("Move", 120);
    expect(onDrag).toHaveBeenCalledTimes(0);
  });

  it("test_exposes_aria_value_attributes", () => {
    setup({ width: { now: 260, min: 160, max: 500 } });
    const el = screen.getByRole("separator");
    expect(el.getAttribute("aria-valuenow")).toBe("260");
    expect(el.getAttribute("aria-valuemin")).toBe("160");
    expect(el.getAttribute("aria-valuemax")).toBe("500");
  });

  it("test_horizontal_orientation_attrs_and_class", () => {
    setup({ orientation: "horizontal", height: { now: 260, min: 160, max: 600 } });
    const el = screen.getByRole("separator");
    expect(el.getAttribute("aria-orientation")).toBe("horizontal");
    expect(String(el.props.className)).toContain("lemma-resize-handle-horizontal");
    expect(el.getAttribute("aria-valuenow")).toBe("260");
    expect(el.getAttribute("aria-valuemin")).toBe("160");
    expect(el.getAttribute("aria-valuemax")).toBe("600");
  });

  it("test_horizontal_drag_emits_pointer_y_deltas", () => {
    const { onDrag } = setup({ orientation: "horizontal" });
    pointer("Down", 0, 0, 200);
    pointer("Move", 50, 0, 180); // up, X change ignored
    pointer("Move", 50, 0, 190); // down
    expect(onDrag.mock.calls.map((c: any[]) => c[0])).toEqual([-20, 10]);
  });

  it("test_horizontal_keyboard_up_down_emit_step", () => {
    const { onDrag } = setup({ orientation: "horizontal" });
    const el = screen.getByRole("separator");
    fireEvent.keyDown(el, { key: "ArrowUp" });
    fireEvent.keyDown(el, { key: "ArrowDown" });
    fireEvent.keyDown(el, { key: "ArrowLeft" });
    expect(onDrag.mock.calls.map((c: any[]) => c[0])).toEqual([-16, 16]);
  });

  it("test_horizontal_dblclick_toggles", () => {
    const { onToggleCollapse } = setup({ orientation: "horizontal" });
    fireEvent.doubleClick(screen.getByRole("separator"));
    expect(onToggleCollapse).toHaveBeenCalledTimes(1);
  });
});
