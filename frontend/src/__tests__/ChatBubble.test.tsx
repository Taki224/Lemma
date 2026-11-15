import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { ChatBubble } from "../components/ChatBubble";

beforeEach(() => {
  (window as any).innerWidth = 1000;
  (window as any).innerHeight = 800;
});

// The fake renderer has no pointer events: invoke the element's React handlers directly.
function pointer(kind: "Down" | "Move" | "Up" | "Cancel", clientX: number, clientY: number, button?: number) {
  const el = screen.getByRole("button");
  el.props[`onPointer${kind}`]({
    type: `pointer${kind.toLowerCase()}`,
    pointerId: 3,
    clientX,
    clientY,
    button,
    target: el,
    currentTarget: {
      setPointerCapture() {},
      releasePointerCapture() {},
      hasPointerCapture: () => true,
    },
    preventDefault() {},
    stopPropagation() {},
  });
}

// The bubble is bounded by its container (the chat layer), not the window.
function makeContainer(rect: { left: number; top: number; width: number; height: number }) {
  const box = { ...rect };
  return {
    box,
    ref: {
      current: {
        getBoundingClientRect: () => ({
          left: box.left,
          top: box.top,
          width: box.width,
          height: box.height,
          right: box.left + box.width,
          bottom: box.top + box.height,
        }),
      },
    } as any,
  };
}

function setup(props: Partial<React.ComponentProps<typeof ChatBubble>> = {}) {
  const onExpand = vi.fn();
  const onCornerChange = vi.fn();
  render(
    <ChatBubble corner="br" pendingCount={0} onExpand={onExpand} onCornerChange={onCornerChange} {...props} />
  );
  return { onExpand, onCornerChange };
}

describe("ChatBubble", () => {
  it("test_click_expands", () => {
    const { onExpand, onCornerChange } = setup();
    pointer("Down", 900, 700);
    pointer("Move", 902, 701);
    pointer("Up", 902, 701);

    expect(onExpand).toHaveBeenCalledTimes(1);
    expect(onCornerChange).toHaveBeenCalledTimes(0);
  });

  it("test_drag_beyond_threshold_does_not_expand_and_snaps", () => {
    const { onExpand, onCornerChange } = setup();
    pointer("Down", 900, 700);
    pointer("Move", 500, 600);
    pointer("Move", 100, 100);
    pointer("Up", 100, 100);

    expect(onExpand).toHaveBeenCalledTimes(0);
    expect(onCornerChange).toHaveBeenCalledTimes(1);
    expect(onCornerChange).toHaveBeenCalledWith("tl");
  });

  it("test_badge_shows_pending_count", () => {
    setup({ pendingCount: 3 });
    expect(screen.getByText("3")).toBeDefined();
    expect(String(screen.getByText("3").props.className)).toContain("lemma-chat-bubble-badge");
  });

  it("test_no_badge_when_zero_pending", () => {
    setup({ pendingCount: 0 });
    expect(screen.queryByText("0")).toBeNull();
  });

  it("test_aria_label_includes_pending", () => {
    setup({ pendingCount: 2 });
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe(
      "Open AI assistant, 2 suggestions waiting"
    );
  });

  it("test_aria_label_without_pending", () => {
    setup();
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe("Open AI assistant");
  });

  it("test_keyboard_expands", () => {
    const { onExpand } = setup();
    const el = screen.getByRole("button");
    fireEvent.keyDown(el, { key: "Enter" });
    fireEvent.keyDown(el, { key: " " });
    fireEvent.keyDown(el, { key: "a" });

    expect(onExpand).toHaveBeenCalledTimes(2);
  });

  it("test_corner_class_reflects_prop", () => {
    setup({ corner: "tl" });
    const cls = String(screen.getByRole("button").props.className);
    expect(cls).toContain("lemma-chat-bubble");
    expect(cls).toContain("lemma-chat-bubble-tl");
  });

  it("test_no_events_after_pointerup", () => {
    const { onExpand, onCornerChange } = setup();
    pointer("Down", 900, 700);
    pointer("Up", 900, 700);
    pointer("Move", 100, 100);
    pointer("Up", 100, 100);

    expect(onExpand).toHaveBeenCalledTimes(1);
    expect(onCornerChange).toHaveBeenCalledTimes(0);
  });

  it("test_synthetic_click_expands_once", () => {
    const { onExpand } = setup();
    fireEvent.click(screen.getByRole("button"));
    expect(onExpand).toHaveBeenCalledTimes(1);
  });

  it("test_click_after_pointer_sequence_does_not_double_fire", () => {
    const { onExpand, onCornerChange } = setup();
    pointer("Down", 900, 700);
    pointer("Up", 900, 700);
    fireEvent.click(screen.getByRole("button")); // browser-generated click after pointerup
    expect(onExpand).toHaveBeenCalledTimes(1);

    // A drag's trailing click must not expand either.
    pointer("Down", 900, 700);
    pointer("Move", 100, 100);
    pointer("Up", 100, 100);
    fireEvent.click(screen.getByRole("button"));
    expect(onExpand).toHaveBeenCalledTimes(1);
    expect(onCornerChange).toHaveBeenCalledTimes(1);
  });

  it("test_non_primary_pointerdown_is_ignored", () => {
    const { onExpand, onCornerChange } = setup();
    pointer("Down", 900, 700, 2);
    pointer("Move", 100, 100);
    pointer("Up", 100, 100);
    expect(onExpand).toHaveBeenCalledTimes(0);
    expect(onCornerChange).toHaveBeenCalledTimes(0);
  });

  it("test_drag_offset_uses_css_custom_properties", () => {
    setup();
    pointer("Down", 900, 700);
    pointer("Move", 850, 650);
    const style = screen.getByRole("button").props.style;
    expect(style["--bubble-dx"]).toBe("-50px");
    expect(style["--bubble-dy"]).toBe("-50px");
    expect(style.transform).toBe(undefined);
  });

  it("test_release_keeps_drop_point_relative_to_new_corner_then_clears", () => {
    // br -> tl: anchors differ by (1000-16-48-16, 800-16-48-16) = (920, 720).
    const frames: Array<() => void> = [];
    (globalThis as any).requestAnimationFrame = (cb: () => void) => frames.push(cb);
    try {
      setup();
      pointer("Down", 900, 700);
      pointer("Move", 100, 100);
      pointer("Up", 100, 100);
      const style = screen.getByRole("button").props.style;
      // drag (-800,-600) + (from - to) (920,720)
      expect(style["--bubble-dx"]).toBe("120px");
      expect(style["--bubble-dy"]).toBe("120px");
      frames.forEach((f) => f());
      expect(screen.getByRole("button").props.style).toBe(undefined);
    } finally {
      delete (globalThis as any).requestAnimationFrame;
    }
  });

  it("test_snap_uses_container_rect_not_window", () => {
    // Window is 1000x800, but the layer is a 400x300 box at (300, 50).
    const c = makeContainer({ left: 300, top: 50, width: 400, height: 300 });
    const { onCornerChange } = setup({ containerRef: c.ref, corner: "br" });
    // Start at the br bubble (~ x 660, y 310), drop at layer-local (100, 100): top-left of the layer.
    pointer("Down", 660, 310);
    pointer("Move", 400, 150);
    pointer("Up", 400, 150);
    expect(onCornerChange).toHaveBeenCalledWith("tl");

    // A drop that is the window's top-left half but the layer's bottom-right half snaps br.
    cleanup();
    const second = makeContainer({ left: 0, top: 0, width: 300, height: 200 });
    const b = setup({ containerRef: second.ref, corner: "tl" });
    pointer("Down", 40, 40);
    pointer("Move", 250, 160);
    pointer("Up", 250, 160);
    expect(b.onCornerChange).toHaveBeenCalledWith("br");
  });

  it("test_drag_is_clamped_inside_container", () => {
    const c = makeContainer({ left: 100, top: 100, width: 400, height: 300 });
    setup({ containerRef: c.ref, corner: "br" });
    pointer("Down", 450, 350);
    pointer("Move", 5000, 5000); // far beyond the layer: stays at the br anchor
    expect(screen.getByRole("button").props.style["--bubble-dx"]).toBe("0px");
    expect(screen.getByRole("button").props.style["--bubble-dy"]).toBe("0px");
    pointer("Move", -5000, -5000);
    // br origin (400-16-48, 300-16-48) = (336, 236); clamp to margin 16 => delta (-320, -220)
    expect(screen.getByRole("button").props.style["--bubble-dx"]).toBe("-320px");
    expect(screen.getByRole("button").props.style["--bubble-dy"]).toBe("-220px");
  });

  it("test_resize_reclamps_active_drag", () => {
    const c = makeContainer({ left: 0, top: 0, width: 800, height: 600 });
    let notify: () => void = () => {};
    (globalThis as any).ResizeObserver = class {
      constructor(cb: () => void) {
        notify = cb;
      }
      observe() {}
      disconnect() {}
      unobserve() {}
    };
    try {
      setup({ containerRef: c.ref, corner: "tl" });
      pointer("Down", 20, 20);
      pointer("Move", 700, 500);
      expect(screen.getByRole("button").props.style["--bubble-dx"]).toBe("680px");
      // Layer shrinks while dragging: bubble must stay inside it. tl origin 16; max x = 300-16-48 = 236.
      c.box.width = 300;
      c.box.height = 200;
      notify();
      expect(screen.getByRole("button").props.style["--bubble-dx"]).toBe("220px");
      expect(screen.getByRole("button").props.style["--bubble-dy"]).toBe("120px");
    } finally {
      delete (globalThis as any).ResizeObserver;
    }
  });

  it("test_resize_observer_connects_once_and_uses_latest_corner", () => {
    const c = makeContainer({ left: 0, top: 0, width: 800, height: 600 });
    let notify: () => void = () => {};
    let created = 0;
    (globalThis as any).ResizeObserver = class {
      constructor(cb: () => void) {
        created++;
        notify = cb;
      }
      observe() {}
      disconnect() {}
      unobserve() {}
    };
    try {
      const noop = () => {};
      const view = (corner: "tl" | "br", pendingCount: number) => (
        <ChatBubble containerRef={c.ref} corner={corner} pendingCount={pendingCount} onExpand={noop} onCornerChange={noop} />
      );
      const r = render(view("tl", 0));
      pointer("Down", 20, 20);
      pointer("Move", 700, 500);
      expect(screen.getByRole("button").props.style["--bubble-dx"]).toBe("680px");
      r.rerender(view("tl", 1));
      r.rerender(view("br", 1));
      expect(created).toBe(1);
      // The long-lived observer must clamp against the corner of the latest render (br), not the first (tl).
      notify();
      expect(screen.getByRole("button").props.style["--bubble-dx"]).toBe("0px");
    } finally {
      delete (globalThis as any).ResizeObserver;
    }
  });
});
