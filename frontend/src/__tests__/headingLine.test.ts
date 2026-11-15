import { test, expect } from "vitest";
import { findHeadingLineAtOrAbove, findHeadingLineAtOrAboveBy } from "../utils/headingLine";

const doc = (...lines: string[]) => lines.join("\n");

test("returns the line itself when it is a heading", () => {
  expect(findHeadingLineAtOrAbove(doc("a", "\\section{X}", "b"), 2)).toBe(2);
});

test("returns the closest heading above", () => {
  const d = doc("\\section{A}", "text", "\\subsection{B}", "text", "text");
  expect(findHeadingLineAtOrAbove(d, 5)).toBe(3);
});

test("recognizes all levels and starred variants", () => {
  for (const cmd of ["part", "chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph"]) {
    expect(findHeadingLineAtOrAbove(doc("x", `\\${cmd}{T}`, "y"), 3)).toBe(2);
    expect(findHeadingLineAtOrAbove(doc("x", `\\${cmd}*{T}`, "y"), 3)).toBe(2);
  }
});

test("allows leading whitespace and optional short title", () => {
  expect(findHeadingLineAtOrAbove(doc("  \\section[Short]{Long}", "y"), 2)).toBe(1);
});

test("ignores commented headings", () => {
  const d = doc("\\section{Real}", "% \\section{Old}", "text");
  expect(findHeadingLineAtOrAbove(d, 3)).toBe(1);
});

test("does not match longer commands like \\sectionmark", () => {
  const d = doc("intro", "\\sectionmark{x}", "");
  expect(findHeadingLineAtOrAbove(d, 3)).toBe(2);
});

test("falls back to nearest preceding non-empty line, never empty", () => {
  const d = doc("text one", "", "  ", "", "");
  expect(findHeadingLineAtOrAbove(d, 5)).toBe(1);
});

test("fallback uses the line itself when non-empty", () => {
  expect(findHeadingLineAtOrAbove(doc("a", "b", "c"), 2)).toBe(2);
});

test("clamps line beyond doc end", () => {
  expect(findHeadingLineAtOrAbove(doc("\\section{A}", "x", ""), 99)).toBe(1);
});

test("returns the clamped line when the document has no non-empty line", () => {
  expect(findHeadingLineAtOrAbove("\n\n", 2)).toBe(2);
});

test("accessor variant scans upward lazily and matches the text helper", () => {
  const lines = ["\\section{A}", "x", "y", "z", "w"];
  const read: number[] = [];
  const got = findHeadingLineAtOrAboveBy((n) => (read.push(n), lines[n - 1]), lines.length, 3);
  expect(got).toBe(1);
  expect(read).toEqual([3, 2, 1]); // never touches lines below the target
  expect(got).toBe(findHeadingLineAtOrAbove(lines.join("\n"), 3));
});

test("accessor variant falls back to nearest non-empty line", () => {
  const lines = ["a", "", "  ", "b", ""];
  expect(findHeadingLineAtOrAboveBy((n) => lines[n - 1], lines.length, 3)).toBe(1);
  expect(findHeadingLineAtOrAboveBy((n) => lines[n - 1], lines.length, 5)).toBe(4);
});
