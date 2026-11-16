import { test, expect } from "vitest";
import { findTextMatches } from "../components/editorSearch";

test("findTextMatches finds case-insensitive matches by default", () => {
  const matches = findTextMatches("Hello world, hello again", "hello", false);
  expect(matches).toEqual([
    { from: 0, to: 5 },
    { from: 13, to: 18 },
  ]);
});

test("findTextMatches respects caseSensitive=true", () => {
  const matches = findTextMatches("Hello world, hello again", "hello", true);
  expect(matches).toEqual([{ from: 13, to: 18 }]);
});

test("findTextMatches returns empty array for empty query or no matches", () => {
  expect(findTextMatches("abc", "", false)).toEqual([]);
  expect(findTextMatches("abc", "xyz", false)).toEqual([]);
});
