from __future__ import annotations

import re
from dataclasses import dataclass

_VERBATIM_ENV_RE = re.compile(
    r"\\begin\{(?:verbatim\*?|lstlisting|minted)\}.*?\\end\{(?:verbatim\*?|lstlisting|minted)\}",
    re.DOTALL,
)
_INLINE_VERB_RE = re.compile(r"\\verb\*?(?P<delim>[^a-zA-Z\s])(?P=delim)?.*?(?P=delim)")

# Single cite command: \cite, \citet, \citep, etc. with optional brackets and a single braced key group
_SINGLE_CITE_CMD_RE = re.compile(
    r"\\(?P<cmd>[a-zA-Z]*cite[a-zA-Z]*)\*?(?P<args>(?:\s*\[[^\]]*\])*\s*\{(?P<keys>[^}]+)\})",
    re.IGNORECASE,
)

# Multicite commands in biblatex (\cites, \autocites, \parencites, etc.) take multiple ([opt][opt]{key})+ groups
_MULTI_CITE_NAMES = frozenset(
    {
        "cites",
        "autocites",
        "parencites",
        "textcites",
        "supercites",
        "footcites",
        "smartcites",
        "citecount",
    }
)
_MULTI_CITE_CMD_RE = re.compile(
    r"\\(?P<cmd>[a-zA-Z]*cites)\*?(?P<args>(?:(?:\s*\[[^\]]*\])*\s*\{[^}]+\})+)",
    re.IGNORECASE,
)
_BRACE_GROUP_RE = re.compile(r"\{([^}]+)\}")

_NON_CITATION_CMDS = frozenset(
    {
        "bibliographystyle",
        "bibliography",
        "addbibresource",
        "citestyle",
        "declarecitecommand",
    }
)


@dataclass(frozen=True)
class BibEntry:
    key: str
    entry_type: str
    title: str
    authors: list[str]
    year: str | None
    doi: str | None
    raw_bibtex: str
    abstract: str | None = None


@dataclass(frozen=True)
class CitationVerificationResult:
    valid_keys: list[str]
    staged_keys: list[str]
    unknown_keys: list[str]
    is_blocked: bool


def strip_latex_comments_and_verbatim(latex_text: str) -> str:
    """Remove verbatim/lstlisting environments and unescaped % comments from LaTeX text."""
    without_envs = _VERBATIM_ENV_RE.sub("", latex_text)
    without_verb = _INLINE_VERB_RE.sub("", without_envs)

    cleaned_lines: list[str] = []
    for line in without_verb.splitlines():
        idx = 0
        n = len(line)
        cut_pos = n
        while idx < n:
            if line[idx] == "%":
                # Count preceding consecutive backslashes
                bs_count = 0
                k = idx - 1
                while k >= 0 and line[k] == "\\":
                    bs_count += 1
                    k -= 1
                if bs_count % 2 == 0:
                    cut_pos = idx
                    break
            idx += 1
        cleaned_lines.append(line[:cut_pos])
    return "\n".join(cleaned_lines)


def _clean_bib_value(raw_val: str, string_macros: dict[str, str]) -> str:
    val = raw_val.strip()
    if val.lower() in string_macros:
        val = string_macros[val.lower()]
    if (val.startswith("{") and val.endswith("}")) or (
        val.startswith('"') and val.endswith('"')
    ):
        val = val[1:-1].strip()
    # Strip inner protective braces like {NLP} or {\TeX}
    val = re.sub(r"[{}]", "", val)
    val = re.sub(r"\s+", " ", val).strip()
    return val


def _parse_bib_fields(body: str, string_macros: dict[str, str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    i = 0
    n = len(body)

    while i < n:
        while i < n and body[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break

        # Read field name
        name_start = i
        while i < n and body[i] not in " \t\r\n=,}":
            i += 1
        field_name = body[name_start:i].strip().lower()
        if not field_name:
            i += 1
            continue

        while i < n and body[i] in " \t\r\n":
            i += 1
        if i >= n or body[i] != "=":
            # Skip until next comma
            while i < n and body[i] != ",":
                i += 1
            continue
        i += 1  # skip '='
        while i < n and body[i] in " \t\r\n":
            i += 1
        if i >= n:
            break

        # Read field value (braced, quoted, or bare token/macro)
        if body[i] == "{":
            depth = 0
            val_start = i
            while i < n:
                if body[i] == "{":
                    depth += 1
                elif body[i] == "}":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            raw_val = body[val_start:i]
        elif body[i] == '"':
            val_start = i
            i += 1
            while i < n:
                if body[i] == '"' and body[i - 1] != "\\":
                    i += 1
                    break
                i += 1
            raw_val = body[val_start:i]
        else:
            val_start = i
            while i < n and body[i] not in ",\r\n}":
                i += 1
            raw_val = body[val_start:i]

        fields[field_name] = _clean_bib_value(raw_val, string_macros)

        while i < n and body[i] != ",":
            i += 1
        if i < n and body[i] == ",":
            i += 1

    return fields


def parse_bib_file(bib_content: str) -> dict[str, BibEntry]:
    """Parse standard BibTeX / BibLaTeX entries, @string macros, and comments."""
    # Remove full-line % comments outside entries
    lines = [
        line
        for line in bib_content.splitlines()
        if not line.lstrip().startswith("%")
    ]
    cleaned = "\n".join(lines)

    entries: dict[str, BibEntry] = {}
    string_macros: dict[str, str] = {}

    i = 0
    n = len(cleaned)
    while i < n:
        at_pos = cleaned.find("@", i)
        if at_pos == -1:
            break
        i = at_pos + 1
        type_start = i
        while i < n and (cleaned[i].isalnum() or cleaned[i] in "_-"):
            i += 1
        entry_type = cleaned[type_start:i].strip().lower()
        while i < n and cleaned[i] in " \t\r\n":
            i += 1
        if i >= n or cleaned[i] not in "{(":
            continue

        open_char = cleaned[i]
        close_char = "}" if open_char == "{" else ")"
        depth = 0
        block_start = at_pos
        body_start = i + 1
        while i < n:
            ch = cleaned[i]
            if ch == open_char:
                depth += 1
            elif ch == close_char:
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1

        raw_bibtex = cleaned[block_start:i].strip()
        inner_body = cleaned[body_start : i - 1].strip()

        if not entry_type or entry_type in {"comment", "preamble"}:
            continue

        if entry_type == "string":
            eq_pos = inner_body.find("=")
            if eq_pos != -1:
                macro_name = inner_body[:eq_pos].strip().lower()
                macro_val = _clean_bib_value(inner_body[eq_pos + 1 :], string_macros)
                if macro_name:
                    string_macros[macro_name] = macro_val
            continue

        comma_pos = inner_body.find(",")
        if comma_pos == -1:
            continue
        cite_key = inner_body[:comma_pos].strip()
        if not cite_key:
            continue

        fields = _parse_bib_fields(inner_body[comma_pos + 1 :], string_macros)
        title = fields.get("title", "")
        raw_author = fields.get("author") or fields.get("editor") or ""
        authors = (
            [a.strip() for a in re.split(r"\s+and\s+", raw_author) if a.strip()]
            if raw_author
            else []
        )
        year = fields.get("year") or fields.get("date")
        if year and len(year) >= 4:
            year_match = re.search(r"\d{4}", year)
            if year_match:
                year = year_match.group(0)
        doi = fields.get("doi") or None
        if doi:
            doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE).strip()
        abstract = fields.get("abstract") or None

        entries[cite_key] = BibEntry(
            key=cite_key,
            entry_type=entry_type,
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            raw_bibtex=raw_bibtex,
            abstract=abstract,
        )

    return entries


def extract_cite_keys(latex_text: str) -> list[str]:
    """Extract all citation keys from LaTeX text, ignoring comments, verbatim blocks, and \\nocite{*}."""
    cleaned = strip_latex_comments_and_verbatim(latex_text)
    keys: list[str] = []
    seen: set[str] = set()

    def add_key(raw_k: str) -> None:
        k = raw_k.strip()
        if not k or k == "*":
            return
        if k not in seen:
            seen.add(k)
            keys.append(k)

    # First check multicite commands (\autocites, \cites, etc.)
    matched_spans: list[tuple[int, int]] = []
    for match in _MULTI_CITE_CMD_RE.finditer(cleaned):
        cmd_name = match.group("cmd").lower()
        if cmd_name in _NON_CITATION_CMDS:
            continue
        matched_spans.append(match.span())
        args_str = match.group("args")
        for brace_match in _BRACE_GROUP_RE.finditer(args_str):
            for raw_k in brace_match.group(1).split(","):
                add_key(raw_k)

    # Then check standard single-bracket cite commands (\cite, \citep, \citet, etc.)
    for match in _SINGLE_CITE_CMD_RE.finditer(cleaned):
        cmd_name = match.group("cmd").lower()
        if cmd_name in _NON_CITATION_CMDS:
            continue
        # Avoid double-extracting spans already covered by multicite
        m_start, m_end = match.span()
        if any(s <= m_start and m_end <= e for s, e in matched_spans):
            continue
        raw_keys_str = match.group("keys")
        for raw_k in raw_keys_str.split(","):
            add_key(raw_k)

    return keys


def verify_citations(
    generated_latex: str,
    bib_entries: dict[str, BibEntry],
    staged_keys: set[str] | None = None,
) -> CitationVerificationResult:
    """Verify every extracted citation key against references.bib and staged candidate sources."""
    active_staged = staged_keys or set()
    extracted = extract_cite_keys(generated_latex)

    valid_keys: list[str] = []
    matched_staged: list[str] = []
    unknown_keys: list[str] = []

    for key in extracted:
        if key in bib_entries:
            valid_keys.append(key)
        elif key in active_staged:
            matched_staged.append(key)
        else:
            unknown_keys.append(key)

    return CitationVerificationResult(
        valid_keys=valid_keys,
        staged_keys=matched_staged,
        unknown_keys=unknown_keys,
        is_blocked=len(unknown_keys) > 0,
    )
