import { test, expect } from "vitest";
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { cancelFlash, flashLine, flashLineExtension, flashLineField } from "../components/flashLine";

const DOC = ["alpha", "beta", "gamma"].join("\n");

function makeView(durationMs?: number) {
  const state = EditorState.create({ doc: DOC, extensions: [flashLineExtension(durationMs)] });
  return new EditorView({ state });
}

function decorations(view: EditorView): Array<{ from: number; cls: string }> {
  const set: any = view.state.field(flashLineField).decorations;
  return (set.items as any[]).map((r) => ({ from: r.from, cls: r.value.class }));
}

// The harness has no fake timers: capture timers instead of waiting.
function withCapturedTimers(run: (pending: Map<number, { fn: () => void; ms?: number }>, cleared: number[]) => void) {
  const realSet = globalThis.setTimeout;
  const realClear = globalThis.clearTimeout;
  const pending = new Map<number, { fn: () => void; ms?: number }>();
  const cleared: number[] = [];
  let nextId = 5000;
  globalThis.setTimeout = ((fn: () => void, ms?: number) => {
    const id = nextId++;
    pending.set(id, { fn, ms });
    return id as any;
  }) as any;
  globalThis.clearTimeout = ((id: any) => {
    cleared.push(id);
    pending.delete(id);
  }) as any;
  try {
    run(pending, cleared);
  } finally {
    globalThis.setTimeout = realSet;
    globalThis.clearTimeout = realClear;
  }
}

test("test_flash_adds_line_decoration", () => {
  withCapturedTimers(() => {
    const view = makeView();
    flashLine(view, 2);
    expect(decorations(view)).toEqual([{ from: 6, cls: "lemma-flash-line" }]);
  });
});

test("test_flash_clears_after_duration", () => {
  withCapturedTimers((pending) => {
    const view = makeView(1200);
    flashLine(view, 1);
    const timers = [...pending.values()];
    expect(timers.length).toBe(1);
    expect(timers[0].ms).toBe(1200);
    expect(decorations(view).length).toBe(1);
    timers[0].fn();
    expect(decorations(view)).toEqual([]);
  });
});

test("test_line_clamped_to_doc_bounds", () => {
  withCapturedTimers(() => {
    const view = makeView();
    flashLine(view, 999);
    expect(decorations(view)).toEqual([{ from: 11, cls: "lemma-flash-line" }]);
    flashLine(view, -5);
    expect(decorations(view)).toEqual([{ from: 0, cls: "lemma-flash-line" }]);
    flashLine(view, 0);
    // Same line re-flashed while still active: class alternates to restart the CSS animation.
    expect(decorations(view)).toEqual([{ from: 0, cls: "lemma-flash-line lemma-flash-line-alt" }]);
  });
});

test("test_second_flash_replaces_first", () => {
  withCapturedTimers((pending, cleared) => {
    const view = makeView();
    flashLine(view, 1);
    const firstId = [...pending.keys()][0];
    flashLine(view, 3);
    expect(cleared).toContain(firstId);
    expect(decorations(view)).toEqual([{ from: 11, cls: "lemma-flash-line" }]);
    expect(pending.size).toBe(1);
  });
});

test("test_flash_decoration_follows_doc_changes", () => {
  withCapturedTimers(() => {
    const view = makeView();
    flashLine(view, 2);
    expect(decorations(view)).toEqual([{ from: 6, cls: "lemma-flash-line" }]);
    // Typing above the flashed line shifts it: the decoration must move with it.
    view.dispatch({ changes: { from: 0, to: 0, insert: "xx" } });
    expect(decorations(view)).toEqual([{ from: 8, cls: "lemma-flash-line" }]);
  });
});

test("test_same_line_reflash_alternates_class_to_restart_animation", () => {
  withCapturedTimers(() => {
    const view = makeView();
    flashLine(view, 2);
    const first = decorations(view)[0].cls;
    flashLine(view, 2);
    const second = decorations(view)[0].cls;
    expect(second).not.toBe(first);
    expect(second.startsWith("lemma-flash-line")).toBe(true);
    flashLine(view, 2);
    expect(decorations(view)[0].cls).toBe(first);
  });
});

test("test_cancel_flash_clears_pending_timer", () => {
  withCapturedTimers((pending, cleared) => {
    const view = makeView();
    flashLine(view, 1);
    const id = [...pending.keys()][0];
    cancelFlash(view);
    expect(cleared).toContain(id);
    expect(pending.size).toBe(0);
    // Idempotent / safe when nothing is pending.
    cancelFlash(view);
  });
});
