import { StateEffect, StateField, type Extension, type Transaction } from "@codemirror/state";
import { Decoration, EditorView, type DecorationSet } from "@codemirror/view";

const DEFAULT_DURATION_MS = 1200;

const setFlash = StateEffect.define<{ line: number }>();
const clearFlash = StateEffect.define<null>();

interface FlashState {
  decorations: DecorationSet;
  durationMs: number;
  // Line currently flashing (null when idle) and which animation variant is applied.
  line: number | null;
  alt: boolean;
}

const lineDeco = Decoration.line({ class: "lemma-flash-line" });
// A different class name restarts the CSS animation when the same line is re-flashed while still active.
const lineDecoAlt = Decoration.line({ class: "lemma-flash-line lemma-flash-line-alt" });

export const flashLineField = StateField.define<FlashState>({
  create: () => ({ decorations: Decoration.none, durationMs: DEFAULT_DURATION_MS, line: null, alt: false }),
  update(value: FlashState, tr: Transaction): FlashState {
    let next = value;
    if (tr.docChanged) {
      next = { ...next, decorations: next.decorations.map(tr.changes) };
    }
    for (const effect of tr.effects) {
      if (effect.is(setFlash)) {
        const line = effect.value.line;
        const alt = next.line === line ? !next.alt : false;
        const pos = tr.state.doc.line(line).from;
        next = { ...next, line, alt, decorations: Decoration.set([(alt ? lineDecoAlt : lineDeco).range(pos)]) };
      } else if (effect.is(clearFlash)) {
        next = { ...next, line: null, alt: false, decorations: Decoration.none };
      }
    }
    return next;
  },
  provide: (f: StateField<FlashState>) => EditorView.decorations.from(f, (v: FlashState) => v.decorations),
});

const timers = new WeakMap<object, ReturnType<typeof setTimeout>>();

export function flashLineExtension(durationMs: number = DEFAULT_DURATION_MS): Extension {
  return flashLineField.init(() => ({
    decorations: Decoration.none,
    durationMs,
    line: null,
    alt: false,
  }));
}

// Stop a pending clear timer (e.g. when the view is destroyed) so it cannot dispatch on a dead view.
export function cancelFlash(view: EditorView): void {
  const pending = timers.get(view);
  if (pending === undefined) return;
  clearTimeout(pending);
  timers.delete(view);
}

export function flashLine(view: EditorView, line: number): void {
  const doc = view.state.doc;
  const clamped = Math.min(Math.max(Math.trunc(line) || 1, 1), doc.lines);
  const durationMs = view.state.field(flashLineField, false)?.durationMs ?? DEFAULT_DURATION_MS;

  cancelFlash(view);

  view.dispatch({ effects: setFlash.of({ line: clamped }) });
  timers.set(
    view,
    setTimeout(() => {
      timers.delete(view);
      view.dispatch({ effects: clearFlash.of(null) });
    }, durationMs)
  );
}
