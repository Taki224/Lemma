"""Code-owned output contracts appended to user-editable prompt instructions.

The backend parsers depend on these formats, so they are never serialized to
prompts.yaml and cannot be edited by the user.
"""

from __future__ import annotations

PRECEDENCE_LINE = "OUTPUT FORMAT — these rules override any formatting instructions above:"

_SEARCH_REPLACE_FORMAT = (
    "- Propose every manuscript edit as a block in exactly this form:\n"
    "<<<<<<< SEARCH\n"
    "(lines copied verbatim from the active file)\n"
    "=======\n"
    "(replacement lines)\n"
    ">>>>>>> REPLACE\n"
)

CHAT_EDIT_CONTRACT = (
    _SEARCH_REPLACE_FORMAT
    + "- Copy SEARCH text character-for-character from the active file, including whitespace "
    "and % comments. Include only the lines you change plus enough neighbouring lines to "
    "make the match unique.\n"
    "- Use one block per separate change. Never wrap blocks in code fences.\n"
    "- Write explanations outside the blocks. If no edit is needed, reply without any block."
)

QUICK_EDIT_CONTRACT = (
    "- Reply with ONLY the replacement LaTeX for the given passage.\n"
    "- No code fences, no commentary, nothing before or after the replacement."
)

EXPLAIN_MATH_CONTRACT = (
    "- Reply with the explanation only, in prose with inline LaTeX math where helpful.\n"
    "- Never propose edits, SEARCH/REPLACE blocks, or a rewritten passage."
)

COMPILE_FIX_CONTRACT = (
    _SEARCH_REPLACE_FORMAT
    + "- Return at most two blocks. SEARCH text must be copied verbatim from the file contents "
    "shown above (the error context or the preamble).\n"
    "- A fix may be anywhere in those contents, e.g. one block adding \\usepackage in the "
    "preamble and one at the error.\n"
    "- State the cause in one or two sentences before the blocks. If you cannot fix it, "
    "return no block."
)

AGENT_STEP_CONTRACT: str = (
    "- Reply with ONLY a single valid JSON object (no code fences, no markdown, no extra text).\n"
    '- Include `"status"` (or `"finding"` on post-read steps): one short plain-English sentence '
    "(at most 160 characters) describing what you are about to check or actually found.\n"
    '- Include `"action"`: `"search"`, `"read"`, or `"answer"`.\n'
    '- When `"action"` is `"search"`, include `"query"` (required search string) and optional '
    '`"path"` (file path filter to narrow the search).\n'
    '- When `"action"` is `"read"`, include `"path"` (exact path from the available project or '
    'attached files), plus optional `"focus"` (keywords), optional `"start_line"` / `"end_line"` '
    '(1-based integers for seeking within text files), and optional `"pages"` (1-based page list '
    "for PDFs).\n"
    '- Choose `"answer"` only if nothing useful remains to search or read.\n'
    "- Never claim a file was updated, saved, or compiled.\n"
    "- Never propose edits inside a step decision."
)


def compose_system_prompt(instructions: str, contract: str) -> str:
    """instructions.strip() + blank line + precedence line + contract (contract always last)."""
    tail = f"{PRECEDENCE_LINE}\n{contract}"
    head = instructions.strip()
    return f"{head}\n\n{tail}" if head else tail
