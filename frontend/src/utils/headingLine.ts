// Limitation: only matches sectioning commands at the start of a line (after whitespace). It does not skip
// verbatim-like blocks or \iffalse ... \fi, so a "\section" inside those is still treated as a heading.
const HEADING_RE = /^\s*\\(?:part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?(?![A-Za-z@])/;

// Same search over a line accessor (1-based `getLine(n)`, `lineCount` lines), so callers with a line-indexed document
// (e.g. CodeMirror) can scan upward without materialising the whole text. Lines are read lazily, upward from `line`.
export function findHeadingLineAtOrAboveBy(
  getLine: (n: number) => string,
  lineCount: number,
  line: number,
): number {
  const start = Math.min(Math.max(1, Math.floor(line)), lineCount);
  let nearestNonEmpty = 0;
  for (let n = start; n >= 1; n--) {
    const text = getLine(n);
    if (HEADING_RE.test(text)) return n;
    if (nearestNonEmpty === 0 && text.trim() !== "") nearestNonEmpty = n;
  }
  return nearestNonEmpty || start;
}

// 1-based line of the closest sectioning heading at or above `line`; if none, the nearest non-empty line at or above it.
export function findHeadingLineAtOrAbove(docText: string, line: number): number {
  const lines = docText.split("\n");
  return findHeadingLineAtOrAboveBy((n) => lines[n - 1], lines.length, line);
}
