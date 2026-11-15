import { stripTypeScriptTypes as nodeStripTs } from "node:module";

/**
 * Transform TSX/TS source code into valid JavaScript ESM for Node 26.
 * First strips generic type arguments on functions so `<Type>(` is not mistaken for JSX,
 * transforms JSX into `React.createElement(...)`, then runs Node 26's native `stripTypeScriptTypes`.
 */
export function transformTsxToJs(source, filename = "module.tsx", stripFn = nodeStripTs) {
  let code = stripGenericCallsBeforeJsx(source);
  code = transformJsx(code);
  code = stripFn(code, { mode: "strip", sourceUrl: filename });
  if (!/\bimport\s+React\b/.test(code) && /\bReact\.(createElement|Fragment)\b/.test(code)) {
    code = `import React from "react";\n` + code;
  }
  return code;
}

function stripGenericCallsBeforeJsx(code) {
  // Strip `<...>` directly preceding `(` on function/hook calls like `useState<string | null>(`
  // or `useRef<HTMLDivElement | null>(` so the JSX parser only sees actual JSX tags.
  let out = "";
  let i = 0;
  while (i < code.length) {
    const ch = code[i];
    if (ch === '"' || ch === "'" || ch === "`") {
      const [s, nextI] = readStringOrTemplateRaw(code, i);
      out += s;
      i = nextI;
      continue;
    }
    if (ch === "/" && code[i + 1] === "/") {
      const nl = code.indexOf("\n", i);
      if (nl === -1) {
        out += code.slice(i);
        break;
      }
      out += code.slice(i, nl + 1);
      i = nl + 1;
      continue;
    }
    if (ch === "/" && code[i + 1] === "*") {
      const end = code.indexOf("*/", i + 2);
      if (end === -1) {
        out += code.slice(i);
        break;
      }
      out += code.slice(i, end + 2);
      i = end + 2;
      continue;
    }
    // Check if `<` immediately follows an identifier and balances to `>(`
    if (ch === "<" && out.length > 0 && /[A-Za-z0-9_$]/.test(out[out.length - 1])) {
      let depth = 1;
      let j = i + 1;
      while (j < code.length && depth > 0) {
        if (code[j] === "<") depth++;
        else if (code[j] === ">") depth--;
        else if (code[j] === "\n" || code[j] === ";") break;
        j++;
      }
      if (depth === 0 && code[j] === "(") {
        i = j;
        continue;
      }
    }
    out += ch;
    i++;
  }
  return out;
}

function readStringOrTemplateRaw(code, start) {
  const quote = code[start];
  let i = start + 1;
  let out = quote;
  while (i < code.length) {
    const c = code[i];
    if (c === "\\") {
      out += code.slice(i, i + 2);
      i += 2;
      continue;
    }
    out += c;
    i++;
    if (c === quote) break;
  }
  return [out, i];
}

function transformJsx(code) {
  let out = "";
  let i = 0;
  const len = code.length;

  while (i < len) {
    const ch = code[i];

    if (ch === '"' || ch === "'" || ch === "`") {
      const [strText, nextIdx] = readStringOrTemplate(code, i);
      out += strText;
      i = nextIdx;
      continue;
    }

    if (ch === "/" && code[i + 1] === "/") {
      const nl = code.indexOf("\n", i);
      if (nl === -1) {
        out += code.slice(i);
        break;
      }
      out += code.slice(i, nl + 1);
      i = nl + 1;
      continue;
    }
    if (ch === "/" && code[i + 1] === "*") {
      const endComment = code.indexOf("*/", i + 2);
      if (endComment === -1) {
        out += code.slice(i);
        break;
      }
      out += code.slice(i, endComment + 2);
      i = endComment + 2;
      continue;
    }

    if (ch === "<" && isJsxStart(code, i, out)) {
      const [jsxExpr, nextIdx] = parseJsxElement(code, i);
      out += jsxExpr;
      i = nextIdx;
      continue;
    }

    out += ch;
    i++;
  }

  return out;
}

function isJsxStart(code, i, precedingOut) {
  if (code[i] !== "<") return false;
  if (code[i + 1] === ">") return true;
  if (!/[A-Za-z_$]/.test(code[i + 1] || "")) return false;
  const trimmedPrev = precedingOut.trimEnd();
  if (!trimmedPrev) return true;
  const lastChar = trimmedPrev[trimmedPrev.length - 1];
  if ("([{,:;?=<!&|+-*/~^>".includes(lastChar)) return true;
  if (/(?:return|case|throw|yield|=>|\belse|\band|\bor)\s*$/.test(trimmedPrev)) return true;
  return false;
}

function readStringOrTemplate(code, start) {
  const quote = code[start];
  let i = start + 1;
  let out = quote;
  while (i < code.length) {
    const c = code[i];
    if (c === "\\") {
      out += code.slice(i, i + 2);
      i += 2;
      continue;
    }
    if (quote === "`" && c === "$" && code[i + 1] === "{") {
      out += "${";
      i += 2;
      const [expr, nextI] = readBalancedJsExpr(code, i, "}");
      out += transformJsx(expr) + "}";
      i = nextI;
      continue;
    }
    out += c;
    i++;
    if (c === quote) break;
  }
  return [out, i];
}

function readBalancedJsExpr(code, start, closingChar = "}") {
  let i = start;
  let depth = 1;
  let expr = "";
  while (i < code.length && depth > 0) {
    const c = code[i];
    if (c === '"' || c === "'" || c === "`") {
      const [s, nextI] = readStringOrTemplate(code, i);
      expr += s;
      i = nextI;
      continue;
    }
    if (c === "/" && code[i + 1] === "/") {
      const nl = code.indexOf("\n", i);
      if (nl === -1) {
        expr += code.slice(i);
        i = code.length;
        break;
      }
      expr += code.slice(i, nl + 1);
      i = nl + 1;
      continue;
    }
    if (c === "{") {
      depth++;
      expr += c;
      i++;
      continue;
    }
    if (c === "}") {
      depth--;
      if (depth === 0 && closingChar === "}") {
        i++;
        break;
      }
      expr += c;
      i++;
      continue;
    }
    expr += c;
    i++;
  }
  return [expr, i];
}

function parseJsxElement(code, start) {
  let i = start + 1;

  if (code[i] === ">") {
    i++;
    const [childrenList, nextIdx] = parseJsxChildren(code, i);
    const childrenArgs = childrenList.length ? ", " + childrenList.join(", ") : "";
    return [`React.createElement(React.Fragment, null${childrenArgs})`, nextIdx];
  }

  let tagName = "";
  while (i < code.length && /[A-Za-z0-9_$.-]/.test(code[i])) {
    tagName += code[i];
    i++;
  }

  const tagExpr = /^[a-z]/.test(tagName) ? JSON.stringify(tagName) : tagName;

  const propsEntries = [];
  let selfClosing = false;

  while (i < code.length) {
    while (i < code.length && /\s/.test(code[i])) i++;
    if (code[i] === "/" && code[i + 1] === ">") {
      selfClosing = true;
      i += 2;
      break;
    }
    if (code[i] === ">") {
      i++;
      break;
    }

    if (code[i] === "{") {
      i++;
      const [spreadExpr, nextI] = readBalancedJsExpr(code, i, "}");
      i = nextI;
      const trimmedSpread = spreadExpr.trim().replace(/^\.\.\./, "");
      propsEntries.push(`...(${transformJsx(trimmedSpread)})`);
      continue;
    }

    let propName = "";
    while (i < code.length && /[A-Za-z0-9_$-:]/.test(code[i])) {
      propName += code[i];
      i++;
    }
    if (!propName) {
      i++;
      continue;
    }

    while (i < code.length && /\s/.test(code[i])) i++;
    if (code[i] !== "=") {
      propsEntries.push(`${JSON.stringify(propName)}: true`);
      continue;
    }
    i++;
    while (i < code.length && /\s/.test(code[i])) i++;

    if (code[i] === '"' || code[i] === "'") {
      const [strVal, nextI] = readStringOrTemplate(code, i);
      i = nextI;
      propsEntries.push(`${JSON.stringify(propName)}: ${strVal}`);
    } else if (code[i] === "{") {
      i++;
      const [valExpr, nextI] = readBalancedJsExpr(code, i, "}");
      i = nextI;
      propsEntries.push(`${JSON.stringify(propName)}: (${transformJsx(valExpr)})`);
    }
  }

  const propsArg = propsEntries.length ? `{ ${propsEntries.join(", ")} }` : "null";

  if (selfClosing) {
    return [`React.createElement(${tagExpr}, ${propsArg})`, i];
  }

  const [childrenList, nextIdx] = parseJsxChildren(code, i);
  const childrenArgs = childrenList.length ? ", " + childrenList.join(", ") : "";
  return [`React.createElement(${tagExpr}, ${propsArg}${childrenArgs})`, nextIdx];
}

function parseJsxChildren(code, start) {
  const children = [];
  let i = start;
  let textBuf = "";

  const flushText = () => {
    if (!textBuf) return;
    const hasNewline = textBuf.includes("\n");
    let cleaned = textBuf;
    if (hasNewline) {
      const lines = cleaned
        .split("\n")
        .map((l, idx, arr) => {
          if (idx === 0) return l.trimEnd();
          if (idx === arr.length - 1) return l.trimStart();
          return l.trim();
        })
        .filter((l) => l.length > 0);
      cleaned = lines.join(" ");
    }
    if (cleaned) {
      children.push(JSON.stringify(decodeHtmlEntities(cleaned)));
    }
    textBuf = "";
  };

  while (i < code.length) {
    if (code[i] === "<" && code[i + 1] === "/") {
      flushText();
      i += 2;
      while (i < code.length && code[i] !== ">") i++;
      i++;
      break;
    }

    if (code[i] === "<" && (code[i + 1] === ">" || /[A-Za-z_$]/.test(code[i + 1] || ""))) {
      flushText();
      const [childJsx, nextI] = parseJsxElement(code, i);
      children.push(childJsx);
      i = nextI;
      continue;
    }

    if (code[i] === "{") {
      flushText();
      i++;
      const [expr, nextI] = readBalancedJsExpr(code, i, "}");
      i = nextI;
      const trimmed = expr.trim();
      if (trimmed && !/^\/\*[\s\S]*\*\/$/.test(trimmed)) {
        children.push(`(${transformJsx(trimmed)})`);
      }
      continue;
    }

    textBuf += code[i];
    i++;
  }

  return [children, i];
}

function decodeHtmlEntities(str) {
  return str
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, "\u00a0");
}
