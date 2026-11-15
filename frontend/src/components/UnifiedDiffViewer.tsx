import React, { useMemo, useState } from "react";
import { parseUnifiedDiff } from "../utils/unifiedDiff";
import type { DiffHunk, DiffLine, ParsedDiffFile } from "../utils/unifiedDiff";

export interface UnifiedDiffViewerProps {
  unifiedDiff: string;
}

function formatHunkRange(hunk: DiffHunk): string {
  if (hunk.newCount === 0) {
    const end = hunk.oldStart + Math.max(hunk.oldCount - 1, 0);
    return hunk.oldCount <= 1 ? `Removed at line ${hunk.oldStart}` : `Removed lines ${hunk.oldStart}–${end}`;
  }
  const end = hunk.newStart + Math.max(hunk.newCount - 1, 0);
  return hunk.newCount <= 1 ? `Line ${hunk.newStart}` : `Lines ${hunk.newStart}–${end}`;
}

function renderLineContent(line: DiffLine): React.ReactNode {
  if (!line.text) return "\u00A0";
  if (!line.segments) return line.text;
  const markClass = line.type === "add" ? "lemma-diff-word-add" : "lemma-diff-word-del";
  return line.segments.map((seg, idx) =>
    seg.changed ? (
      <mark key={idx} className={markClass}>
        {seg.text}
      </mark>
    ) : (
      <React.Fragment key={idx}>{seg.text}</React.Fragment>
    ),
  );
}

function DiffFileSection({
  file,
  defaultCollapsed,
}: {
  file: ParsedDiffFile;
  defaultCollapsed: boolean;
}) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  const title = file.oldPath ? `${file.oldPath} → ${file.path}` : file.path;

  return (
    <section className="lemma-diff-file" aria-label={`Diff for ${file.path}`}>
      <button
        type="button"
        className="lemma-diff-file-header"
        aria-expanded={!collapsed}
        onClick={() => setCollapsed((c) => !c)}
      >
        <span className="lemma-diff-file-chevron" aria-hidden="true">
          {collapsed ? "▸" : "▾"}
        </span>
        <span className="lemma-diff-file-name" title={title}>
          {title}
        </span>
        {file.status !== "modified" ? (
          <span className={`lemma-diff-status-badge is-${file.status}`}>{file.status}</span>
        ) : null}
        <span className="lemma-diff-file-stats">
          {file.additions > 0 ? <span className="lemma-diff-stat-add">+{file.additions}</span> : null}
          {file.deletions > 0 ? <span className="lemma-diff-stat-del">−{file.deletions}</span> : null}
        </span>
      </button>

      {!collapsed ? (
        file.isBinary ? (
          <div className="lemma-diff-binary-note">Binary file changed</div>
        ) : file.hunks.length === 0 ? (
          <div className="lemma-diff-binary-note">No textual changes</div>
        ) : (
          <div className="lemma-diff-hunks">
            {file.hunks.map((hunk, hIdx) => (
              <div key={hIdx} className="lemma-diff-hunk">
                <div className="lemma-diff-hunk-header">
                  <span className="lemma-diff-hunk-range">{formatHunkRange(hunk)}</span>
                  {hunk.context ? (
                    <span className="lemma-diff-hunk-context">{hunk.context}</span>
                  ) : null}
                </div>
                <div className="lemma-diff-lines" role="table" aria-label={formatHunkRange(hunk)}>
                  {hunk.lines.map((line, lIdx) => {
                    const sign = line.type === "add" ? "+" : line.type === "del" ? "−" : " ";
                    return (
                      <div
                        key={lIdx}
                        role="row"
                        className={`lemma-diff-line is-${line.type}`}
                      >
                        <span className="lemma-diff-ln" aria-hidden="true">
                          {line.oldLine ?? ""}
                        </span>
                        <span className="lemma-diff-ln" aria-hidden="true">
                          {line.newLine ?? ""}
                        </span>
                        <span className="lemma-diff-sign" aria-hidden="true">
                          {sign}
                        </span>
                        <span className="lemma-diff-text">{renderLineContent(line)}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        )
      ) : null}
    </section>
  );
}

export function UnifiedDiffViewer({ unifiedDiff }: UnifiedDiffViewerProps) {
  const files = useMemo(() => parseUnifiedDiff(unifiedDiff), [unifiedDiff]);

  if (files.length === 0) {
    return <pre className="lemma-timeline-diff-code">{unifiedDiff}</pre>;
  }

  return (
    <div className="lemma-diff-viewer">
      {files.map((file, idx) => (
        <DiffFileSection
          key={`${file.path}-${idx}`}
          file={file}
          defaultCollapsed={false}
        />
      ))}
    </div>
  );
}
