import {
  autocompletion,
  type Completion,
  type CompletionContext,
  type CompletionResult,
  type CompletionSource,
} from "@codemirror/autocomplete";
import type { Extension } from "@codemirror/state";
import { formatBibDetail, type BibEntry } from "../utils/bibParser";

const CITE_BEFORE_CURSOR_RE =
  /\\[a-zA-Z]*cite[a-zA-Z]*\*?(?:\s*\[[^\]]*\])*\{([^}]*)$/i;

export interface CitationMatch {
  query: string;
  fromOffset: number;
  alreadyCited: Set<string>;
}

/**
 * Strip unescaped `%` comment from a single line prefix up to the cursor.
 */
function hasUnescapedCommentBefore(lineBefore: string, index: number): boolean {
  for (let i = 0; i < index; i++) {
    if (lineBefore[i] === "%" && (i === 0 || lineBefore[i - 1] !== "\\")) {
      return true;
    }
  }
  return false;
}

/**
 * Detect whether the cursor is inside a LaTeX `\cite{...}`-style argument
 * and return the current key query, its start offset within `lineBeforeCursor`,
 * and any keys already entered earlier in the same `{...}` list.
 */
export function matchCitationContext(lineBeforeCursor: string): CitationMatch | null {
  const match = CITE_BEFORE_CURSOR_RE.exec(lineBeforeCursor);
  if (!match) return null;

  if (hasUnescapedCommentBefore(lineBeforeCursor, match.index)) {
    return null;
  }

  const insideBraces = match[1];
  const braceContentStart = lineBeforeCursor.length - insideBraces.length;
  const lastComma = insideBraces.lastIndexOf(",");

  const alreadyCited = new Set<string>();
  if (lastComma !== -1) {
    const priorKeys = insideBraces.slice(0, lastComma).split(",");
    for (const raw of priorKeys) {
      const k = raw.trim();
      if (k) alreadyCited.add(k);
    }
  }

  const afterComma = lastComma === -1 ? insideBraces : insideBraces.slice(lastComma + 1);
  const leadingWs = afterComma.length - afterComma.trimStart().length;
  const fromOffset =
    braceContentStart + (lastComma === -1 ? 0 : lastComma + 1) + leadingWs;
  const query = afterComma.slice(leadingWs);

  return { query, fromOffset, alreadyCited };
}

function rankEntry(entry: BibEntry, tokens: string[]): number {
  if (tokens.length === 0) return 0;
  const keyLower = entry.key.toLowerCase();
  const haystack = `${keyLower} ${entry.title} ${entry.author} ${entry.year}`.toLowerCase();
  for (const tok of tokens) {
    if (!haystack.includes(tok)) return -1;
  }
  if (tokens.length === 1) {
    if (keyLower.startsWith(tokens[0])) return 0;
    if (keyLower.includes(tokens[0])) return 1;
  }
  return 2;
}

/**
 * Create a CodeMirror `CompletionSource` that suggests BibTeX keys inside `\cite{...}`.
 */
export function createCitationCompletionSource(
  getEntries: () => BibEntry[],
  isDisabled?: () => boolean
): CompletionSource {
  return (context: CompletionContext): CompletionResult | null => {
    if (isDisabled?.()) return null;

    const line = context.state.doc.lineAt(context.pos);
    const lineBefore = line.text.slice(0, context.pos - line.from);
    const citeMatch = matchCitationContext(lineBefore);
    if (!citeMatch) return null;

    const entries = getEntries();
    if (entries.length === 0) return null;

    const tokens = citeMatch.query
      .trim()
      .toLowerCase()
      .split(/\s+/)
      .filter(Boolean);

    const ranked: Array<{ rank: number; completion: Completion }> = [];
    for (const entry of entries) {
      if (citeMatch.alreadyCited.has(entry.key)) continue;
      const rank = rankEntry(entry, tokens);
      if (rank === -1) continue;
      ranked.push({
        rank,
        completion: {
          label: entry.key,
          detail: formatBibDetail(entry),
          type: "text",
          apply: entry.key,
        },
      });
    }

    if (ranked.length === 0) return null;

    ranked.sort((a, b) => a.rank - b.rank);

    return {
      from: line.from + citeMatch.fromOffset,
      options: ranked.map((r) => r.completion),
      filter: false,
      validFor: /^[a-zA-Z0-9_.:/\s-]*$/,
    };
  };
}

/**
 * CodeMirror extension enabling `\cite{...}` BibTeX reference dropdowns.
 */
export function citationAutocompleteExtension(
  getEntries: () => BibEntry[],
  isDisabled?: () => boolean
): Extension {
  return autocompletion({
    override: [createCitationCompletionSource(getEntries, isDisabled)],
    activateOnTyping: true,
    maxRenderedOptions: 50,
    icons: false,
  });
}
