import React, { useState } from "react";
import katex from "katex";
import "katex/dist/katex.min.css";

export interface SanitizedMarkdownProps {
  content: string;
}

const SCRIPT_BLOCK_RE = /<script\b[^>]*>[\s\S]*?<\/script>/gi;
const SCRIPT_TAG_RE = /<script\b[^>]*\/?>/gi;
const IFRAME_BLOCK_RE = /<iframe\b[^>]*>[\s\S]*?<\/iframe>/gi;
const IFRAME_TAG_RE = /<iframe\b[^>]*\/?>/gi;
const IMG_TAG_RE = /<img\b[^>]*\/?>/gi;
const MD_IMG_RE = /!\[([^\]]*)\]\(([^)]+)\)/g;

const MD_LINK_PREFIX_RE =
  /^\[([^\]]+)\]\(((?:https?:\/\/[^\s()]+|\((?:https?:\/\/[^\s()]+)\))+)\)/i;
const BARE_URL_PREFIX_RE = /^(https?:\/\/[^\s)<>"']+)/i;

const HR_RE = /^\s{0,3}(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$/;
const HEADING_RE = /^\s{0,3}(#{1,6})(?:\s+(.*?))?\s*$/;
const BLOCKQUOTE_RE = /^\s{0,3}>\s?(.*)$/;
const UNORDERED_LIST_RE = /^(\s*)([-*+])\s+(.*)$/;
const ORDERED_LIST_RE = /^(\s*)(\d+)\.\s+(.*)$/;
const TABLE_SEP_RE = /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$/;

type Alignment = "left" | "center" | "right" | null;

interface ListItemNode {
  blocks: BlockNode[];
}

type BlockNode =
  | { type: "heading"; level: 1 | 2 | 3 | 4 | 5 | 6; text: string }
  | { type: "hr" }
  | { type: "code"; text: string }
  | { type: "display_math"; tex: string }
  | { type: "blockquote"; blocks: BlockNode[] }
  | { type: "list"; ordered: boolean; start: number; items: ListItemNode[] }
  | { type: "table"; alignments: Alignment[]; headers: string[]; rows: string[][] }
  | { type: "paragraph"; text: string };

function sanitizeMarkdownSource(raw: string): string {
  return raw
    .replace(/\r\n?/g, "\n")
    .replace(SCRIPT_BLOCK_RE, "")
    .replace(SCRIPT_TAG_RE, "")
    .replace(IFRAME_BLOCK_RE, "")
    .replace(IFRAME_TAG_RE, "")
    .replace(IMG_TAG_RE, "")
    .replace(MD_IMG_RE, "[Remote image blocked: $1]");
}

function renderKatex(tex: string, display: boolean, key: string): React.ReactNode {
  try {
    const html = katex.renderToString(tex.trim(), {
      displayMode: display,
      throwOnError: false,
      errorColor: "currentColor",
      strict: "ignore",
    });
    return (
      <span
        key={key}
        className={display ? "lemma-math-display" : "lemma-math-inline"}
        dangerouslySetInnerHTML={{ __html: html }}
      />
    );
  } catch {
    return tex;
  }
}

function findClosingInlineMathDollar(text: string, openIdx: number): number {
  for (let i = openIdx + 1; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === "\n") return -1;
    if (ch === "\\") {
      i += 1;
      continue;
    }
    if (ch === "$") {
      if (text[i - 1] && /\s/.test(text[i - 1])) return -1;
      if (text[i + 1] === "$") return -1;
      return i;
    }
  }
  return -1;
}

function findClosingBackticks(text: string, startAfterOpen: number, tickLen: number): number {
  let i = startAfterOpen;
  while (i < text.length) {
    if (text[i] === "`") {
      let run = 0;
      while (i + run < text.length && text[i + run] === "`") run += 1;
      if (run === tickLen) return i;
      i += run;
    } else {
      i += 1;
    }
  }
  return -1;
}

function findClosingDelimiter(text: string, start: number, delim: string): number {
  let i = start;
  while (i < text.length) {
    if (text[i] === "`") {
      let tickLen = 0;
      while (i + tickLen < text.length && text[i + tickLen] === "`") tickLen += 1;
      const closeTicks = findClosingBackticks(text, i + tickLen, tickLen);
      if (closeTicks !== -1) {
        i = closeTicks + tickLen;
        continue;
      }
      i += tickLen;
      continue;
    }
    if (text.startsWith("\\(", i)) {
      const end = text.indexOf("\\)", i + 2);
      if (end !== -1) {
        i = end + 2;
        continue;
      }
    }
    if (text[i] === "$" && text[i - 1] !== "\\" && text[i + 1] !== "$" && text[i + 1] && !/\s/.test(text[i + 1])) {
      const end = findClosingInlineMathDollar(text, i);
      if (end !== -1) {
        i = end + 1;
        continue;
      }
    }
    if (delim === "*") {
      if (text[i] === "*" && text[i + 1] !== "*" && text[i - 1] !== "*") {
        return i;
      }
    } else if (text.startsWith(delim, i)) {
      return i;
    }
    i += 1;
  }
  return -1;
}

function renderInline(
  text: string,
  keyPrefix: string,
  onOpenExternalUrl: (url: string) => void
): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  let plain = "";
  let i = 0;
  let seq = 0;

  const flushPlain = () => {
    if (plain) {
      nodes.push(plain);
      plain = "";
    }
  };

  while (i < text.length) {
    // 1. Display math inline: \[...\] or $$...$$
    if (text.startsWith("\\[", i)) {
      const end = text.indexOf("\\]", i + 2);
      if (end !== -1) {
        flushPlain();
        nodes.push(renderKatex(text.slice(i + 2, end), true, `${keyPrefix}-dm${seq++}`));
        i = end + 2;
        continue;
      }
    }
    if (text.startsWith("$$", i)) {
      const end = text.indexOf("$$", i + 2);
      if (end !== -1) {
        flushPlain();
        nodes.push(renderKatex(text.slice(i + 2, end), true, `${keyPrefix}-dm${seq++}`));
        i = end + 2;
        continue;
      }
    }

    // 2. Inline math: \(...\) or $...$
    if (text.startsWith("\\(", i)) {
      const end = text.indexOf("\\)", i + 2);
      if (end !== -1) {
        flushPlain();
        nodes.push(renderKatex(text.slice(i + 2, end), false, `${keyPrefix}-m${seq++}`));
        i = end + 2;
        continue;
      }
    }
    if (
      text[i] === "$" &&
      text[i - 1] !== "\\" &&
      text[i + 1] !== undefined &&
      text[i + 1] !== "$" &&
      !/\s/.test(text[i + 1])
    ) {
      const end = findClosingInlineMathDollar(text, i);
      if (end !== -1 && end > i + 1) {
        flushPlain();
        nodes.push(renderKatex(text.slice(i + 1, end), false, `${keyPrefix}-m${seq++}`));
        i = end + 1;
        continue;
      }
    }

    // 3. Inline code (CommonMark multi-backtick spans)
    if (text[i] === "`") {
      let tickLen = 0;
      while (i + tickLen < text.length && text[i + tickLen] === "`") tickLen += 1;
      const closePos = findClosingBackticks(text, i + tickLen, tickLen);
      if (closePos !== -1) {
        flushPlain();
        let code = text.slice(i + tickLen, closePos);
        if (code.length >= 2 && code.startsWith(" ") && code.endsWith(" ") && code.trim().length > 0) {
          code = code.slice(1, -1);
        }
        nodes.push(
          <code key={`${keyPrefix}-c${seq++}`} className="lemma-inline-code">
            {code}
          </code>
        );
        i = closePos + tickLen;
        continue;
      }
      plain += text.slice(i, i + tickLen);
      i += tickLen;
      continue;
    }

    // 4. Markdown links [label](https://...)
    if (text[i] === "[") {
      const m = MD_LINK_PREFIX_RE.exec(text.slice(i));
      if (m) {
        flushPlain();
        const label = m[1];
        const url = m[2];
        nodes.push(
          <button
            key={`${keyPrefix}-u${seq++}`}
            type="button"
            className="lemma-external-url-btn"
            aria-label={`External URL ${url}`}
            onClick={() => onOpenExternalUrl(url)}
          >
            {label ? `${label} (${url})` : url}
          </button>
        );
        i += m[0].length;
        continue;
      }
    }

    // 5. Bare external URLs
    if (text[i] === "h" || text[i] === "H") {
      const m = BARE_URL_PREFIX_RE.exec(text.slice(i));
      if (m) {
        flushPlain();
        const url = m[1];
        nodes.push(
          <button
            key={`${keyPrefix}-u${seq++}`}
            type="button"
            className="lemma-external-url-btn"
            aria-label={`External URL ${url}`}
            onClick={() => onOpenExternalUrl(url)}
          >
            {url}
          </button>
        );
        i += m[0].length;
        continue;
      }
    }

    // 6. Bold **...**
    if (text.startsWith("**", i)) {
      const end = findClosingDelimiter(text, i + 2, "**");
      if (end !== -1 && end > i + 2) {
        flushPlain();
        const k = `${keyPrefix}-b${seq++}`;
        nodes.push(
          <strong key={k}>
            {renderInline(text.slice(i + 2, end), k, onOpenExternalUrl)}
          </strong>
        );
        i = end + 2;
        continue;
      }
    }

    // 7. Strikethrough ~~...~~
    if (text.startsWith("~~", i)) {
      const end = findClosingDelimiter(text, i + 2, "~~");
      if (end !== -1 && end > i + 2) {
        flushPlain();
        const k = `${keyPrefix}-d${seq++}`;
        nodes.push(
          <del key={k}>
            {renderInline(text.slice(i + 2, end), k, onOpenExternalUrl)}
          </del>
        );
        i = end + 2;
        continue;
      }
    }

    // 8. Italic *...* (underscores stay literal for LaTeX keys/subscripts)
    if (text[i] === "*" && text[i + 1] !== "*") {
      const end = findClosingDelimiter(text, i + 1, "*");
      if (end !== -1 && end > i + 1) {
        flushPlain();
        const k = `${keyPrefix}-e${seq++}`;
        nodes.push(
          <em key={k}>
            {renderInline(text.slice(i + 1, end), k, onOpenExternalUrl)}
          </em>
        );
        i = end + 1;
        continue;
      }
    }

    plain += text[i];
    i += 1;
  }

  flushPlain();
  return nodes;
}

function splitTableRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  const cells: string[] = [];
  let current = "";
  for (let i = 0; i < trimmed.length; i += 1) {
    if (trimmed[i] === "\\" && trimmed[i + 1] === "|") {
      current += "|";
      i += 1;
    } else if (trimmed[i] === "|") {
      cells.push(current.trim());
      current = "";
    } else {
      current += trimmed[i];
    }
  }
  cells.push(current.trim());
  return cells;
}

function parseTableAlignments(sepLine: string): Alignment[] {
  return splitTableRow(sepLine).map((cell) => {
    const left = cell.startsWith(":");
    const right = cell.endsWith(":");
    if (left && right) return "center";
    if (right) return "right";
    if (left) return "left";
    return null;
  });
}

function matchListMarker(line: string): { indent: number; ordered: boolean; start: number; text: string } | null {
  if (HR_RE.test(line)) return null;
  const ul = UNORDERED_LIST_RE.exec(line);
  if (ul) {
    return { indent: ul[1].length, ordered: false, start: 1, text: ul[3] };
  }
  const ol = ORDERED_LIST_RE.exec(line);
  if (ol) {
    return { indent: ol[1].length, ordered: true, start: Number(ol[2]), text: ol[3] };
  }
  return null;
}

function isBlockStarter(line: string): boolean {
  if (HR_RE.test(line)) return true;
  if (HEADING_RE.test(line)) return true;
  if (BLOCKQUOTE_RE.test(line)) return true;
  if (/^\s{0,3}(`{3,}|~{3,})/.test(line)) return true;
  if (matchListMarker(line) !== null) return true;
  const trimmed = line.trim();
  if (trimmed.startsWith("\\[") || trimmed.startsWith("$$")) return true;
  return false;
}

function parseBlocks(lines: string[]): BlockNode[] {
  const blocks: BlockNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // 1. Blank line
    if (line.trim() === "") {
      i += 1;
      continue;
    }

    // 2. Fenced code block
    const fenceMatch = line.match(/^\s{0,3}(`{3,}|~{3,})(.*)$/);
    if (fenceMatch && !(fenceMatch[1].startsWith("`") && fenceMatch[2].includes("`"))) {
      const fenceChar = fenceMatch[1][0];
      const fenceLen = fenceMatch[1].length;
      const closeRe = new RegExp(`^\\s{0,3}${fenceChar}{${fenceLen},}\\s*$`);
      const codeLines: string[] = [];
      i += 1;
      while (i < lines.length && !closeRe.test(lines[i])) {
        codeLines.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1;
      blocks.push({ type: "code", text: codeLines.join("\n") });
      continue;
    }

    // 3. Multi-line or standalone display math block (\[...\] or $$...$$)
    const trimmed = line.trim();
    if (trimmed.startsWith("\\[")) {
      const closeIdx = line.indexOf("\\]", line.indexOf("\\[") + 2);
      if (closeIdx !== -1 && line.slice(closeIdx + 2).trim() === "") {
        blocks.push({
          type: "display_math",
          tex: line.slice(line.indexOf("\\[") + 2, closeIdx),
        });
        i += 1;
        continue;
      }
      if (closeIdx === -1) {
        const mathLines = [line.slice(line.indexOf("\\[") + 2)];
        let j = i + 1;
        let closed = false;
        while (j < lines.length) {
          const endIdx = lines[j].indexOf("\\]");
          if (endIdx !== -1) {
            mathLines.push(lines[j].slice(0, endIdx));
            closed = true;
            j += 1;
            break;
          }
          mathLines.push(lines[j]);
          j += 1;
        }
        if (closed) {
          blocks.push({ type: "display_math", tex: mathLines.join("\n") });
          i = j;
          continue;
        }
      }
    }

    if (trimmed.startsWith("$$")) {
      const openPos = line.indexOf("$$");
      const closeIdx = line.indexOf("$$", openPos + 2);
      if (closeIdx !== -1 && line.slice(closeIdx + 2).trim() === "") {
        blocks.push({
          type: "display_math",
          tex: line.slice(openPos + 2, closeIdx),
        });
        i += 1;
        continue;
      }
      if (closeIdx === -1) {
        const mathLines = [line.slice(openPos + 2)];
        let j = i + 1;
        let closed = false;
        while (j < lines.length) {
          const endIdx = lines[j].indexOf("$$");
          if (endIdx !== -1) {
            mathLines.push(lines[j].slice(0, endIdx));
            closed = true;
            j += 1;
            break;
          }
          mathLines.push(lines[j]);
          j += 1;
        }
        if (closed) {
          blocks.push({ type: "display_math", tex: mathLines.join("\n") });
          i = j;
          continue;
        }
      }
    }

    // 4. Horizontal rule
    if (HR_RE.test(line)) {
      blocks.push({ type: "hr" });
      i += 1;
      continue;
    }

    // 5. Heading
    const headingMatch = HEADING_RE.exec(line);
    if (headingMatch) {
      const level = headingMatch[1].length as 1 | 2 | 3 | 4 | 5 | 6;
      const text = (headingMatch[2] || "").replace(/\s+#+\s*$/, "");
      blocks.push({ type: "heading", level, text });
      i += 1;
      continue;
    }

    // 6. Blockquote
    const quoteMatch = BLOCKQUOTE_RE.exec(line);
    if (quoteMatch) {
      const quoteLines: string[] = [quoteMatch[1]];
      i += 1;
      while (i < lines.length) {
        const nextQuote = BLOCKQUOTE_RE.exec(lines[i]);
        if (nextQuote) {
          quoteLines.push(nextQuote[1]);
          i += 1;
        } else {
          break;
        }
      }
      blocks.push({ type: "blockquote", blocks: parseBlocks(quoteLines) });
      continue;
    }

    // 7. Table
    if (
      i + 1 < lines.length &&
      line.includes("|") &&
      TABLE_SEP_RE.test(lines[i + 1]) &&
      lines[i + 1].includes("|")
    ) {
      const headers = splitTableRow(line);
      const alignments = parseTableAlignments(lines[i + 1]);
      const rows: string[][] = [];
      i += 2;
      while (
        i < lines.length &&
        lines[i].trim() !== "" &&
        lines[i].includes("|") &&
        !isBlockStarter(lines[i])
      ) {
        rows.push(splitTableRow(lines[i]));
        i += 1;
      }
      blocks.push({ type: "table", alignments, headers, rows });
      continue;
    }

    // 8. List (ordered or unordered, with nested sublists)
    const firstMarker = matchListMarker(line);
    if (firstMarker) {
      const baseIndent = firstMarker.indent;
      const ordered = firstMarker.ordered;
      const start = firstMarker.start;
      const items: ListItemNode[] = [];

      while (i < lines.length) {
        const marker = matchListMarker(lines[i]);
        if (!marker || marker.ordered !== ordered || Math.abs(marker.indent - baseIndent) > 1) {
          break;
        }
        const itemLines: string[] = [marker.text];
        i += 1;

        while (i < lines.length) {
          if (lines[i].trim() === "") {
            let k = i + 1;
            while (k < lines.length && lines[k].trim() === "") k += 1;
            if (k >= lines.length) {
              i = k;
              break;
            }
            const nextMarker = matchListMarker(lines[k]);
            if (nextMarker && nextMarker.ordered === ordered && Math.abs(nextMarker.indent - baseIndent) <= 1) {
              i = k;
              break;
            }
            const nextIndent = (lines[k].match(/^(\s*)/)?.[1] ?? "").length;
            if (nextIndent > baseIndent) {
              itemLines.push("");
              i += 1;
              continue;
            }
            i = k;
            break;
          }

          const nextMarker = matchListMarker(lines[i]);
          if (nextMarker) {
            const isSiblingOrOuter =
              nextMarker.indent <= baseIndent ||
              (nextMarker.ordered === ordered && nextMarker.indent <= baseIndent + 1);
            if (isSiblingOrOuter) {
              break;
            }
            itemLines.push(lines[i]);
            i += 1;
            continue;
          }

          const lineIndent = (lines[i].match(/^(\s*)/)?.[1] ?? "").length;
          if (lineIndent <= baseIndent && isBlockStarter(lines[i])) {
            break;
          }
          itemLines.push(lines[i]);
          i += 1;
        }

        items.push({ blocks: parseBlocks(itemLines) });
      }

      blocks.push({ type: "list", ordered, start, items });
      continue;
    }

    // 9. Paragraph
    const paraLines: string[] = [line];
    i += 1;
    while (i < lines.length && lines[i].trim() !== "" && !isBlockStarter(lines[i])) {
      if (
        i + 1 < lines.length &&
        lines[i].includes("|") &&
        TABLE_SEP_RE.test(lines[i + 1]) &&
        lines[i + 1].includes("|")
      ) {
        break;
      }
      paraLines.push(lines[i]);
      i += 1;
    }
    blocks.push({ type: "paragraph", text: paraLines.join("\n") });
  }

  return blocks;
}

function renderBlock(
  block: BlockNode,
  key: string,
  onOpenExternalUrl: (url: string) => void
): React.ReactNode {
  switch (block.type) {
    case "heading": {
      const Tag = `h${block.level}` as "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
      return (
        <Tag key={key} className={`lemma-md-heading lemma-md-h${block.level}`}>
          {renderInline(block.text, key, onOpenExternalUrl)}
        </Tag>
      );
    }
    case "hr":
      return <hr key={key} className="lemma-md-hr" />;
    case "code":
      return (
        <pre key={key} className="lemma-chat-code">
          <code>{block.text}</code>
        </pre>
      );
    case "display_math":
      return renderKatex(block.tex, true, key);
    case "blockquote":
      return (
        <blockquote key={key} className="lemma-md-blockquote">
          {block.blocks.map((child, idx) => renderBlock(child, `${key}-q${idx}`, onOpenExternalUrl))}
        </blockquote>
      );
    case "list": {
      const ListTag = block.ordered ? "ol" : "ul";
      return (
        <ListTag
          key={key}
          className={block.ordered ? "lemma-md-ol" : "lemma-md-ul"}
          start={block.ordered && block.start !== 1 ? block.start : undefined}
        >
          {block.items.map((item, itemIdx) => (
            <li key={`${key}-i${itemIdx}`} className="lemma-md-li">
              {item.blocks.map((child, childIdx) =>
                child.type === "paragraph" && item.blocks.length === 1 ? (
                  <span key={`${key}-i${itemIdx}-p${childIdx}`} className="lemma-md-li-text">
                    {renderInline(child.text, `${key}-i${itemIdx}-p${childIdx}`, onOpenExternalUrl)}
                  </span>
                ) : (
                  renderBlock(child, `${key}-i${itemIdx}-b${childIdx}`, onOpenExternalUrl)
                )
              )}
            </li>
          ))}
        </ListTag>
      );
    }
    case "table":
      return (
        <div key={key} className="lemma-md-table-wrap">
          <table className="lemma-md-table">
            <thead>
              <tr>
                {block.headers.map((h, colIdx) => (
                  <th
                    key={`${key}-h${colIdx}`}
                    style={block.alignments[colIdx] ? { textAlign: block.alignments[colIdx] ?? undefined } : undefined}
                  >
                    {renderInline(h, `${key}-h${colIdx}`, onOpenExternalUrl)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIdx) => (
                <tr key={`${key}-r${rowIdx}`}>
                  {block.headers.map((_, colIdx) => (
                    <td
                      key={`${key}-r${rowIdx}-c${colIdx}`}
                      style={block.alignments[colIdx] ? { textAlign: block.alignments[colIdx] ?? undefined } : undefined}
                    >
                      {renderInline(row[colIdx] ?? "", `${key}-r${rowIdx}-c${colIdx}`, onOpenExternalUrl)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case "paragraph":
      return (
        <p key={key} className="lemma-md-p">
          {renderInline(block.text, key, onOpenExternalUrl)}
        </p>
      );
  }
}

export function SanitizedMarkdown({
  content,
}: SanitizedMarkdownProps): React.ReactElement {
  const [pendingUrl, setPendingUrl] = useState<string | null>(null);
  const sanitized = sanitizeMarkdownSource(content);
  const blocks = parseBlocks(sanitized.split("\n"));

  return (
    <div className="lemma-sanitized-markdown" data-testid="sanitized-markdown">
      <div className="lemma-markdown-body">
        {blocks.map((block, idx) => renderBlock(block, `b${idx}`, setPendingUrl))}
      </div>

      {pendingUrl && (
        <div
          role="dialog"
          aria-label="Confirm opening external URL"
          className="lemma-external-url-modal"
        >
          <h4>Confirm opening external URL</h4>
          <p>
            You are about to open an external link outside your workspace:{" "}
            <code>{pendingUrl}</code>
          </p>
          <div className="lemma-modal-actions">
            <button
              type="button"
              aria-label="Confirm Open"
              onClick={() => {
                if (typeof window !== "undefined" && typeof window.open === "function") {
                  window.open(pendingUrl, "_blank", "noopener,noreferrer");
                }
                setPendingUrl(null);
              }}
            >
              Confirm Open
            </button>
            <button
              type="button"
              aria-label="Cancel"
              onClick={() => setPendingUrl(null)}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
