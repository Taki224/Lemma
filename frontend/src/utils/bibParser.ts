export interface BibEntry {
  key: string;
  entryType: string;
  title: string;
  author: string;
  year: string;
}

const SKIPPED_ENTRY_TYPES = new Set(["comment", "preamble", "string"]);

/**
 * Remove outer BibTeX braces/quotes and inner grouping braces, and collapse whitespace.
 */
function cleanBibValue(raw: string): string {
  let text = raw.trim();
  if (
    (text.startsWith("{") && text.endsWith("}")) ||
    (text.startsWith('"') && text.endsWith('"'))
  ) {
    text = text.slice(1, -1);
  }
  // Strip remaining TeX grouping braces while preserving escaped braces.
  text = text.replace(/(?<!\\)[{}]/g, "");
  return text.replace(/\s+/g, " ").trim();
}

/**
 * Parse `key = value` fields from inside a BibTeX entry body.
 */
function parseBibFields(body: string): Record<string, string> {
  const fields: Record<string, string> = {};
  let i = 0;
  const n = body.length;

  while (i < n) {
    while (i < n && /[\s,]/.test(body[i])) i++;
    if (i >= n) break;

    const nameStart = i;
    while (i < n && /[a-zA-Z0-9_-]/.test(body[i])) i++;
    const fieldName = body.slice(nameStart, i).toLowerCase();
    if (!fieldName) {
      i++;
      continue;
    }

    while (i < n && /\s/.test(body[i])) i++;
    if (i >= n || body[i] !== "=") {
      // Skip until next comma at top level.
      while (i < n && body[i] !== ",") i++;
      continue;
    }
    i++; // skip '='
    while (i < n && /\s/.test(body[i])) i++;
    if (i >= n) break;

    let rawVal = "";
    const ch = body[i];
    if (ch === "{") {
      let depth = 0;
      const valStart = i;
      while (i < n) {
        if (body[i] === "{" && body[i - 1] !== "\\") depth++;
        else if (body[i] === "}" && body[i - 1] !== "\\") {
          depth--;
          if (depth === 0) {
            i++;
            break;
          }
        }
        i++;
      }
      rawVal = body.slice(valStart, i);
    } else if (ch === '"') {
      const valStart = i;
      i++;
      let depth = 0;
      while (i < n) {
        if (body[i] === "{" && body[i - 1] !== "\\") depth++;
        else if (body[i] === "}" && body[i - 1] !== "\\") depth = Math.max(0, depth - 1);
        else if (body[i] === '"' && depth === 0 && body[i - 1] !== "\\") {
          i++;
          break;
        }
        i++;
      }
      rawVal = body.slice(valStart, i);
    } else {
      const valStart = i;
      while (i < n && body[i] !== "," && body[i] !== "}") i++;
      rawVal = body.slice(valStart, i);
    }

    fields[fieldName] = cleanBibValue(rawVal);
  }

  return fields;
}

/**
 * Parse BibTeX / BibLaTeX entries from a `.bib` file string.
 */
export function parseBibFile(bibText: string): BibEntry[] {
  if (!bibText) return [];

  const cleaned = bibText
    .split(/\r?\n/)
    .filter((line) => !line.trimStart().startsWith("%"))
    .join("\n");

  const entries: BibEntry[] = [];
  const seenKeys = new Set<string>();
  const n = cleaned.length;
  let i = 0;

  while (i < n) {
    const atPos = cleaned.indexOf("@", i);
    if (atPos === -1) break;
    i = atPos + 1;

    const typeStart = i;
    while (i < n && /[a-zA-Z0-9_-]/.test(cleaned[i])) i++;
    const entryType = cleaned.slice(typeStart, i).toLowerCase();

    while (i < n && /\s/.test(cleaned[i])) i++;
    if (i >= n || (cleaned[i] !== "{" && cleaned[i] !== "(")) continue;

    const openChar = cleaned[i];
    const closeChar = openChar === "{" ? "}" : ")";
    const bodyStart = i + 1;
    let depth = 0;
    let inQuotes = false;

    while (i < n) {
      const ch = cleaned[i];
      const prev = cleaned[i - 1];
      if (ch === '"' && prev !== "\\" && depth === 1) {
        inQuotes = !inQuotes;
      } else if (!inQuotes) {
        if (ch === openChar && prev !== "\\") depth++;
        else if (ch === closeChar && prev !== "\\") {
          depth--;
          if (depth === 0) {
            i++;
            break;
          }
        }
      }
      i++;
    }

    if (!entryType || SKIPPED_ENTRY_TYPES.has(entryType)) continue;

    const inner = cleaned.slice(bodyStart, i - 1).trim();
    const commaIdx = inner.indexOf(",");
    if (commaIdx === -1) continue;

    const key = inner.slice(0, commaIdx).trim();
    if (!key || seenKeys.has(key)) continue;
    seenKeys.add(key);

    const fields = parseBibFields(inner.slice(commaIdx + 1));
    entries.push({
      key,
      entryType,
      title: fields.title || "",
      author: fields.author || fields.editor || "",
      year: fields.year || (fields.date ? fields.date.slice(0, 4) : ""),
    });
  }

  return entries;
}

function extractLastName(author: string): string {
  const trimmed = author.trim();
  if (!trimmed) return "";
  if (trimmed.includes(",")) {
    return trimmed.split(",")[0].trim();
  }
  const parts = trimmed.split(/\s+/);
  return parts[parts.length - 1] || trimmed;
}

function formatShortAuthors(authorField: string): string {
  if (!authorField) return "";
  const authors = authorField
    .split(/\s+and\s+/i)
    .map(extractLastName)
    .filter(Boolean);
  if (authors.length === 0) return "";
  if (authors.length === 1) return authors[0];
  if (authors.length === 2) return `${authors[0]} & ${authors[1]}`;
  return `${authors[0]} et al.`;
}

/**
 * Format a compact one-line summary for the citation dropdown.
 */
export function formatBibDetail(entry: BibEntry): string {
  const shortAuthor = formatShortAuthors(entry.author);
  const meta =
    shortAuthor && entry.year
      ? `${shortAuthor} (${entry.year})`
      : shortAuthor || (entry.year ? `(${entry.year})` : "");

  const parts: string[] = [];
  if (meta) parts.push(meta);
  if (entry.title) {
    const title = entry.title.length > 64 ? `${entry.title.slice(0, 61)}...` : entry.title;
    parts.push(title);
  }
  return parts.join(" — ");
}
