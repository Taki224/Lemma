import { describe, expect, it } from "vitest";
import { computeInlineSegments, parseUnifiedDiff } from "../utils/unifiedDiff";

describe("parseUnifiedDiff", () => {
  it("parses git diff with hunk headers, line numbers, and stats", () => {
    const raw = [
      "diff --git a/main.tex b/main.tex",
      "index de5bd9b..513cfbf 100644",
      "--- a/main.tex",
      "+++ b/main.tex",
      "@@ -13,3 +13,4 @@ \\section{Methodology}",
      " Lorem ipsum dolor sit amet.",
      "-Old sentence here.",
      "+New sentence here.",
      "+Added second line.",
    ].join("\n");

    const files = parseUnifiedDiff(raw);
    expect(files).toHaveLength(1);
    const file = files[0];
    expect(file.path).toBe("main.tex");
    expect(file.status).toBe("modified");
    expect(file.additions).toBe(2);
    expect(file.deletions).toBe(1);
    expect(file.hunks).toHaveLength(1);

    const hunk = file.hunks[0];
    expect(hunk.oldStart).toBe(13);
    expect(hunk.newStart).toBe(13);
    expect(hunk.context).toBe("\\section{Methodology}");
    expect(hunk.lines).toHaveLength(4);

    expect(hunk.lines[0]).toMatchObject({ type: "ctx", oldLine: 13, newLine: 13, text: "Lorem ipsum dolor sit amet." });
    expect(hunk.lines[1]).toMatchObject({ type: "del", oldLine: 14, newLine: null, text: "Old sentence here." });
    expect(hunk.lines[2]).toMatchObject({ type: "add", oldLine: null, newLine: 14, text: "New sentence here." });
    expect(hunk.lines[3]).toMatchObject({ type: "add", oldLine: null, newLine: 15, text: "Added second line." });
  });

  it("handles added, deleted, and binary files", () => {
    const raw = [
      "diff --git a/new.tex b/new.tex",
      "new file mode 100644",
      "index 0000000..1111111",
      "--- /dev/null",
      "+++ b/new.tex",
      "@@ -0,0 +1,1 @@",
      "+Hello",
      "diff --git a/fig.pdf b/fig.pdf",
      "Binary files a/fig.pdf and b/fig.pdf differ",
    ].join("\n");

    const files = parseUnifiedDiff(raw);
    expect(files).toHaveLength(2);
    expect(files[0].status).toBe("added");
    expect(files[0].path).toBe("new.tex");
    expect(files[0].additions).toBe(1);
    expect(files[1].isBinary).toBe(true);
    expect(files[1].path).toBe("fig.pdf");
  });

  it("returns empty array for plain non-diff strings", () => {
    expect(parseUnifiedDiff("diff-of-sha-003-newest")).toEqual([]);
  });
});

describe("computeInlineSegments", () => {
  it("highlights changed words between paired lines", () => {
    const res = computeInlineSegments("The quick brown fox", "The fast brown fox");
    expect(res).not.toBeNull();
    expect(res!.oldSegments).toEqual([
      { text: "The ", changed: false },
      { text: "quick", changed: true },
      { text: " brown fox", changed: false },
    ]);
    expect(res!.newSegments).toEqual([
      { text: "The ", changed: false },
      { text: "fast", changed: true },
      { text: " brown fox", changed: false },
    ]);
  });

  it("returns null when lines are completely unrelated", () => {
    expect(computeInlineSegments("aaaaaa bbbbbb", "zzzzzz yyyyyy")).toBeNull();
  });
});
