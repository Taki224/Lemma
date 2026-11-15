export type DiffLineType = "add" | "del" | "ctx" | "meta";

export interface DiffSegment {
  text: string;
  changed: boolean;
}

export interface DiffLine {
  type: DiffLineType;
  text: string;
  oldLine: number | null;
  newLine: number | null;
  segments?: DiffSegment[];
}

export interface DiffHunk {
  header: string;
  oldStart: number;
  oldCount: number;
  newStart: number;
  newCount: number;
  context: string;
  lines: DiffLine[];
}

export type DiffFileStatus = "modified" | "added" | "deleted" | "renamed";

export interface ParsedDiffFile {
  path: string;
  oldPath?: string;
  status: DiffFileStatus;
  additions: number;
  deletions: number;
  isBinary: boolean;
  hunks: DiffHunk[];
}

const HUNK_HEADER_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:\s?(.*))?$/;
const TOKEN_RE = /\s+|[\\{}[\]().,;:!?]+|[^\s\\{}[\]().,;:!?]+/g;

function stripDiffPrefix(rawPath: string): string {
  const trimmed = rawPath.trim();
  if (trimmed.startsWith("a/") || trimmed.startsWith("b/")) {
    return trimmed.slice(2);
  }
  return trimmed;
}

function tokenize(text: string): string[] {
  return text.match(TOKEN_RE) ?? [];
}

function mergeSegments(parts: DiffSegment[]): DiffSegment[] {
  const merged: DiffSegment[] = [];
  for (const part of parts) {
    if (!part.text) continue;
    const prev = merged[merged.length - 1];
    if (prev && prev.changed === part.changed) {
      prev.text += part.text;
    } else {
      merged.push({ text: part.text, changed: part.changed });
    }
  }
  return merged;
}

/**
 * Computes word/token-level inline diff segments for a paired removed/added line.
 * Returns null if the lines are too large or completely unrelated.
 */
export function computeInlineSegments(
  oldText: string,
  newText: string,
): { oldSegments: DiffSegment[]; newSegments: DiffSegment[] } | null {
  if (!oldText || !newText || oldText === newText) return null;

  const a = tokenize(oldText);
  const b = tokenize(newText);
  if (a.length === 0 || b.length === 0) return null;
  if (a.length * b.length > 25000) return null;

  const m = a.length;
  const n = b.length;
  const dp = new Uint16Array((m + 1) * (n + 1));
  const cols = n + 1;

  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      if (a[i] === b[j]) {
        dp[i * cols + j] = dp[(i + 1) * cols + (j + 1)] + 1;
      } else {
        const down = dp[(i + 1) * cols + j];
        const right = dp[i * cols + (j + 1)];
        dp[i * cols + j] = down >= right ? down : right;
      }
    }
  }

  const oldParts: DiffSegment[] = [];
  const newParts: DiffSegment[] = [];
  let i = 0;
  let j = 0;
  let commonChars = 0;

  while (i < m && j < n) {
    if (a[i] === b[j]) {
      commonChars += a[i].length;
      oldParts.push({ text: a[i], changed: false });
      newParts.push({ text: b[j], changed: false });
      i++;
      j++;
    } else if (dp[(i + 1) * cols + j] >= dp[i * cols + (j + 1)]) {
      oldParts.push({ text: a[i], changed: true });
      i++;
    } else {
      newParts.push({ text: b[j], changed: true });
      j++;
    }
  }
  while (i < m) {
    oldParts.push({ text: a[i], changed: true });
    i++;
  }
  while (j < n) {
    newParts.push({ text: b[j], changed: true });
    j++;
  }

  // Skip noisy inline marks if the two lines share almost nothing.
  const maxLen = Math.max(oldText.length, newText.length);
  if (maxLen > 0 && commonChars / maxLen < 0.2) {
    return null;
  }

  return {
    oldSegments: mergeSegments(oldParts),
    newSegments: mergeSegments(newParts),
  };
}

function annotateHunkInlineDiffs(lines: DiffLine[]): void {
  let idx = 0;
  while (idx < lines.length) {
    if (lines[idx].type !== "del") {
      idx++;
      continue;
    }
    const delStart = idx;
    while (idx < lines.length && lines[idx].type === "del") idx++;
    const delEnd = idx;

    const addStart = idx;
    while (idx < lines.length && lines[idx].type === "add") idx++;
    const addEnd = idx;

    const delCount = delEnd - delStart;
    const addCount = addEnd - addStart;
    if (delCount > 0 && delCount === addCount) {
      for (let k = 0; k < delCount; k++) {
        const delLine = lines[delStart + k];
        const addLine = lines[addStart + k];
        const res = computeInlineSegments(delLine.text, addLine.text);
        if (res) {
          delLine.segments = res.oldSegments;
          addLine.segments = res.newSegments;
        }
      }
    }
  }
}

export function parseUnifiedDiff(raw: string): ParsedDiffFile[] {
  if (!raw) return [];
  const rawLines = raw.split(/\r?\n/);
  const files: ParsedDiffFile[] = [];

  let currentFile: ParsedDiffFile | null = null;
  let currentHunk: DiffHunk | null = null;
  let oldLine = 0;
  let newLine = 0;

  const ensureFile = (pathHint = "Changes"): ParsedDiffFile => {
    if (!currentFile) {
      currentFile = {
        path: pathHint,
        status: "modified",
        additions: 0,
        deletions: 0,
        isBinary: false,
        hunks: [],
      };
      files.push(currentFile);
    }
    return currentFile;
  };

  for (let i = 0; i < rawLines.length; i++) {
    const line = rawLines[i];

    if (line.startsWith("diff --git ")) {
      const rest = line.slice("diff --git ".length);
      const match = rest.match(/^a\/(.+?)\s+b\/(.+)$/);
      const oldPath = match ? match[1] : undefined;
      const newPath = match ? match[2] : stripDiffPrefix(rest.split(/\s+/)[1] || rest);
      currentFile = {
        path: newPath || oldPath || "file",
        oldPath: oldPath && newPath && oldPath !== newPath ? oldPath : undefined,
        status: "modified",
        additions: 0,
        deletions: 0,
        isBinary: false,
        hunks: [],
      };
      files.push(currentFile);
      currentHunk = null;
      continue;
    }

    if (currentFile && !currentHunk) {
      if (line.startsWith("new file mode")) {
        currentFile.status = "added";
        continue;
      }
      if (line.startsWith("deleted file mode")) {
        currentFile.status = "deleted";
        continue;
      }
      if (line.startsWith("rename from ")) {
        currentFile.status = "renamed";
        currentFile.oldPath = line.slice("rename from ".length).trim();
        continue;
      }
      if (line.startsWith("rename to ")) {
        currentFile.status = "renamed";
        currentFile.path = line.slice("rename to ".length).trim();
        continue;
      }
      if (line.startsWith("Binary files ") && line.endsWith(" differ")) {
        currentFile.isBinary = true;
        continue;
      }
      if (line.startsWith("index ") || line.startsWith("similarity index") || line.startsWith("old mode") || line.startsWith("new mode")) {
        continue;
      }
    }

    if (line.startsWith("--- ")) {
      const target = line.slice(4).trim();
      const file = ensureFile(target === "/dev/null" ? "file" : stripDiffPrefix(target));
      if (target === "/dev/null") {
        file.status = "added";
      } else if (!file.oldPath) {
        const clean = stripDiffPrefix(target);
        if (clean && clean !== file.path) file.oldPath = clean;
      }
      continue;
    }

    if (line.startsWith("+++ ")) {
      const target = line.slice(4).trim();
      const file = ensureFile(target === "/dev/null" ? "file" : stripDiffPrefix(target));
      if (target === "/dev/null") {
        file.status = "deleted";
      } else {
        file.path = stripDiffPrefix(target);
      }
      continue;
    }

    const hunkMatch = line.match(HUNK_HEADER_RE);
    if (hunkMatch) {
      const file = ensureFile();
      const oldStart = Number(hunkMatch[1]);
      const oldCount = hunkMatch[2] !== undefined ? Number(hunkMatch[2]) : 1;
      const newStart = Number(hunkMatch[3]);
      const newCount = hunkMatch[4] !== undefined ? Number(hunkMatch[4]) : 1;
      const context = (hunkMatch[5] || "").trim();

      currentHunk = {
        header: line,
        oldStart,
        oldCount,
        newStart,
        newCount,
        context,
        lines: [],
      };
      file.hunks.push(currentHunk);
      oldLine = oldStart;
      newLine = newStart;
      continue;
    }

    if (currentHunk && currentFile) {
      // Ignore trailing empty string from final newline split
      if (i === rawLines.length - 1 && line === "") {
        continue;
      }
      if (line.startsWith("+")) {
        currentFile.additions++;
        currentHunk.lines.push({
          type: "add",
          text: line.slice(1),
          oldLine: null,
          newLine: newLine++,
        });
      } else if (line.startsWith("-")) {
        currentFile.deletions++;
        currentHunk.lines.push({
          type: "del",
          text: line.slice(1),
          oldLine: oldLine++,
          newLine: null,
        });
      } else if (line.startsWith("\\")) {
        currentHunk.lines.push({
          type: "meta",
          text: line,
          oldLine: null,
          newLine: null,
        });
      } else {
        currentHunk.lines.push({
          type: "ctx",
          text: line.startsWith(" ") ? line.slice(1) : line,
          oldLine: oldLine++,
          newLine: newLine++,
        });
      }
    }
  }

  for (const file of files) {
    for (const hunk of file.hunks) {
      annotateHunkInlineDiffs(hunk.lines);
    }
  }

  return files;
}
