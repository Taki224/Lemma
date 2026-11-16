import { test, expect } from "vitest";
import { computeMatchRects, findPdfMatches } from "../components/pdfSearch";

test("findPdfMatches finds matches across pages and within single items", () => {
  const matches = findPdfMatches(
    [
      [{ str: "Introduction to LaTeX", hasEOL: true }],
      [{ str: "More LaTeX examples" }],
    ],
    "latex",
    false
  );
  expect(matches).toHaveLength(2);
  expect(matches[0].pageNum).toBe(1);
  expect(matches[1].pageNum).toBe(2);
});

test("findPdfMatches matches phrases spanning adjacent items and EOL", () => {
  const matches = findPdfMatches(
    [
      [
        { str: "Hello", hasEOL: true },
        { str: "world today", hasEOL: false },
      ],
    ],
    "Hello world",
    true
  );
  expect(matches).toHaveLength(1);
  expect(matches[0].spans).toEqual([
    { itemIndex: 0, fracStart: 0, fracEnd: 1 },
    { itemIndex: 1, fracStart: 0, fracEnd: 5 / 11 },
  ]);
});

test("computeMatchRects converts PDF coordinates using viewport.convertToViewportRectangle", () => {
  const items = [
    { str: "HelloWorld", transform: [1, 0, 0, 1, 100, 700], width: 100, height: 10 },
  ];
  const viewport = {
    convertToViewportRectangle: ([x1, y1, x2, y2]: [number, number, number, number]) => [
      x1,
      800 - y2,
      x2,
      800 - y1,
    ],
  };
  const rects = computeMatchRects(
    items,
    [{ itemIndex: 0, fracStart: 0.5, fracEnd: 1.0 }],
    viewport
  );
  expect(rects).toHaveLength(1);
  expect(rects[0].left).toBe(150);
  expect(rects[0].width).toBe(50);
  expect(rects[0].top).toBe(91);
  expect(rects[0].height).toBe(11);
});
