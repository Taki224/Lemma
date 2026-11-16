export interface PdfTextItemWithGeometry {
  str?: string;
  hasEOL?: boolean;
  transform?: number[];
  width?: number;
  height?: number;
}

export interface PdfMatchSpan {
  itemIndex: number;
  fracStart: number;
  fracEnd: number;
}

export interface PdfSearchMatch {
  pageNum: number;
  spans: PdfMatchSpan[];
}

export interface PdfHighlightRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface PdfViewportLike {
  convertToViewportRectangle?: (rect: [number, number, number, number]) => number[];
}

export function findPdfMatches(
  itemsByPage: PdfTextItemWithGeometry[][],
  query: string,
  caseSensitive: boolean
): PdfSearchMatch[] {
  if (!query) return [];
  const needle = caseSensitive ? query : query.toLowerCase();
  const matches: PdfSearchMatch[] = [];

  for (let pageIdx = 0; pageIdx < itemsByPage.length; pageIdx++) {
    const items = itemsByPage[pageIdx];
    const spans: Array<{ itemIndex: number; start: number; end: number }> = [];
    let pageText = "";

    for (let i = 0; i < items.length; i++) {
      const str = typeof items[i]?.str === "string" ? items[i].str! : "";
      if (str.length > 0) {
        const start = pageText.length;
        pageText += str;
        spans.push({ itemIndex: i, start, end: pageText.length });
      }
      if (items[i]?.hasEOL && i + 1 < items.length) {
        const nextStr = typeof items[i + 1]?.str === "string" ? items[i + 1].str! : "";
        if (str.length > 0 && !/\s$/.test(str) && nextStr.length > 0 && !/^\s/.test(nextStr)) {
          pageText += " ";
        }
      }
    }

    const haystack = caseSensitive ? pageText : pageText.toLowerCase();
    let pos = 0;
    while (pos <= haystack.length - needle.length) {
      const idx = haystack.indexOf(needle, pos);
      if (idx === -1) break;
      const matchStart = idx;
      const matchEnd = idx + needle.length;
      const matchSpans: PdfMatchSpan[] = [];

      for (const s of spans) {
        if (s.end <= matchStart || s.start >= matchEnd) continue;
        const itemLen = s.end - s.start;
        const localStart = Math.max(0, matchStart - s.start);
        const localEnd = Math.min(itemLen, matchEnd - s.start);
        matchSpans.push({
          itemIndex: s.itemIndex,
          fracStart: localStart / itemLen,
          fracEnd: localEnd / itemLen,
        });
      }

      if (matchSpans.length > 0) {
        matches.push({ pageNum: pageIdx + 1, spans: matchSpans });
      }
      pos = matchEnd;
    }
  }

  return matches;
}

export function computeMatchRects(
  items: PdfTextItemWithGeometry[],
  spans: PdfMatchSpan[],
  viewport: PdfViewportLike | null
): PdfHighlightRect[] {
  if (!viewport || typeof viewport.convertToViewportRectangle !== "function") {
    return [];
  }
  const rects: PdfHighlightRect[] = [];
  for (const span of spans) {
    const item = items[span.itemIndex];
    if (!item || !Array.isArray(item.transform) || item.transform.length < 6) continue;
    const [, , c, d, tx, ty] = item.transform;
    const itemWidth = typeof item.width === "number" ? item.width : 0;
    const itemHeight =
      typeof item.height === "number" && item.height > 0 ? item.height : Math.hypot(c, d) || 12;
    const x1 = tx + itemWidth * span.fracStart;
    const x2 = tx + itemWidth * span.fracEnd;
    const y1 = ty - itemHeight * 0.2;
    const y2 = ty + itemHeight * 0.9;
    const [vx1, vy1, vx2, vy2] = viewport.convertToViewportRectangle([x1, y1, x2, y2]);
    rects.push({
      left: Math.min(vx1, vx2),
      top: Math.min(vy1, vy2),
      width: Math.max(Math.abs(vx2 - vx1), 4),
      height: Math.max(Math.abs(vy2 - vy1), 10),
    });
  }
  return rects;
}
