import { StateEffect, StateField, type Extension, type Transaction } from "@codemirror/state";
import { Decoration, EditorView, type DecorationSet } from "@codemirror/view";

export interface EditorSearchMatch {
  from: number;
  to: number;
}

export function findTextMatches(
  docText: string,
  query: string,
  caseSensitive: boolean
): EditorSearchMatch[] {
  if (!query) return [];
  const haystack = caseSensitive ? docText : docText.toLowerCase();
  const needle = caseSensitive ? query : query.toLowerCase();
  const matches: EditorSearchMatch[] = [];
  let pos = 0;
  while (pos <= haystack.length - needle.length) {
    const idx = haystack.indexOf(needle, pos);
    if (idx === -1) break;
    matches.push({ from: idx, to: idx + needle.length });
    pos = idx + needle.length;
  }
  return matches;
}

const setEditorSearchEffect = StateEffect.define<{
  matches: EditorSearchMatch[];
  activeIndex: number;
}>();

const matchDeco = Decoration.mark({ class: "lemma-editor-search-match" });
const activeMatchDeco = Decoration.mark({ class: "lemma-editor-search-match is-active" });

const editorSearchField = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(decorations: DecorationSet, tr: Transaction): DecorationSet {
    let next = tr.docChanged ? decorations.map(tr.changes) : decorations;
    for (const effect of tr.effects) {
      if (effect.is(setEditorSearchEffect)) {
        const { matches, activeIndex } = effect.value;
        if (matches.length === 0) {
          next = Decoration.none;
        } else {
          const docLen = tr.state.doc.length;
          const ranges = matches
            .filter((m) => m.from >= 0 && m.to <= docLen && m.from < m.to)
            .map((m, idx) => (idx === activeIndex ? activeMatchDeco : matchDeco).range(m.from, m.to));
          next = Decoration.set(ranges);
        }
      }
    }
    return next;
  },
  provide: (f: StateField<DecorationSet>) => EditorView.decorations.from(f),
});

export function editorSearchExtension(): Extension {
  return editorSearchField;
}

export function applyEditorSearch(
  view: EditorView,
  matches: EditorSearchMatch[],
  activeIndex: number,
  scrollToActive: boolean
): void {
  const active = matches[activeIndex];
  const effects: StateEffect<unknown>[] = [setEditorSearchEffect.of({ matches, activeIndex })];
  if (scrollToActive && active) {
    effects.push(EditorView.scrollIntoView(active.from, { y: "center" }));
  }
  view.dispatch({ effects });
}
