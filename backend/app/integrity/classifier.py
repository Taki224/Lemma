from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Sequence

IntegrityTier = Literal[1, 2, 3]

_CITE_CMD_RE = re.compile(
    r"\\(?:cite|citep|citet|autocite|parencite|textcite|footcite|nocite)[a-zA-Z*]*\s*(?:\[[^\]]*\]\s*)*\{[^}]+\}"
)
_BIBTEX_ENTRY_RE = re.compile(
    r"@(?:article|inproceedings|proceedings|book|incollection|phdthesis|mastersthesis|techreport|misc|unpublished)\s*[{(]",
    re.IGNORECASE,
)
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")
_TODO_VERIFY_CITE_RE = re.compile(r"%\s*TODO:\s*Verify\s+citation", re.IGNORECASE)

# Patterns for stripping LaTeX commands/formatting to isolate natural-language prose
_LATEX_COMMENT_RE = re.compile(r"(?<!\\)%.*$", re.MULTILINE)
_LATEX_ENV_TAG_RE = re.compile(r"\\(?:begin|end)\{[^}]+\}(?:\[[^\]]*\])?")
_LATEX_CMD_WITH_OPT_RE = re.compile(r"\\[a-zA-Z@]+\*?(?:\[[^\]]*\])?")
_MATH_BLOCK_RE = re.compile(r"\$\$.*?\$\$|\\\[.*?\\\]", re.DOTALL)
_INLINE_MATH_RE = re.compile(r"(?<!\\)\$[^$\n]+\$|\\\(.*?\\\)")
_BRACES_AND_TABLE_RE = re.compile(r"[{}\[\]&~^]|\\\\")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]*")

_SUMMARIZE_OR_TRANSLATE_RE = re.compile(
    r"\b(summariz\w*|summaris\w*|translat\w*|paraphras\w*|rephrase\w*|draft\b|write\s+(?:a|the|this)\s+(?:section|paragraph|chapter|introduction|abstract|conclusion|methodology|review))\b",
    re.IGNORECASE,
)

# Thresholds for distinguishing minor proofreading vs. substantive prose edits
_MINOR_NET_WORD_THRESHOLD = 25
_MINOR_SIMILARITY_THRESHOLD = 0.80
_PROPORTIONALITY_NEW_WORDS_THRESHOLD = 250
_SHORT_SUMMARY_INPUT_WORDS = 80
_CHAT_SUBSTANTIVE_REPLY_WORDS = 60


@dataclass(frozen=True)
class IntegrityFlags:
    has_citations: bool = False
    has_unknown_citations: bool = False
    has_overlap_warnings: bool = False
    substantive_prose: bool = False
    proportionality_nudge: bool = False

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


@dataclass(frozen=True)
class IntegrityAssessment:
    tier: IntegrityTier
    category: str
    reasons: list[str] = field(default_factory=list)
    flags: IntegrityFlags = field(default_factory=IntegrityFlags)
    suggested_purpose: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "category": self.category,
            "reasons": list(self.reasons),
            "flags": self.flags.to_dict(),
            "suggested_purpose": self.suggested_purpose,
        }


def strip_latex_to_prose(text: str) -> str:
    """Strip LaTeX commands, comments, math, and structural delimiters to compare prose."""
    if not text:
        return ""
    cleaned = _LATEX_COMMENT_RE.sub(" ", text)
    cleaned = _MATH_BLOCK_RE.sub(" ", cleaned)
    cleaned = _INLINE_MATH_RE.sub(" ", cleaned)
    cleaned = _LATEX_ENV_TAG_RE.sub(" ", cleaned)
    cleaned = _LATEX_CMD_WITH_OPT_RE.sub(" ", cleaned)
    cleaned = _BRACES_AND_TABLE_RE.sub(" ", cleaned)
    return " ".join(cleaned.split())


def _prose_words(text: str) -> list[str]:
    return _WORD_RE.findall(strip_latex_to_prose(text))


def _detect_citation_signals(
    text: str,
    *,
    cite_keys: Sequence[str] = (),
    unknown_cite_keys: Sequence[str] = (),
) -> tuple[bool, bool]:
    has_unknown = bool(unknown_cite_keys)
    has_citations = bool(
        cite_keys
        or unknown_cite_keys
        or _CITE_CMD_RE.search(text or "")
        or _BIBTEX_ENTRY_RE.search(text or "")
        or _DOI_RE.search(text or "")
        or _TODO_VERIFY_CITE_RE.search(text or "")
    )
    return has_citations, has_unknown


def _purpose_for_profile(profile_id: str, category: str) -> str:
    pid = (profile_id or "").strip().lower()
    mapping = {
        "writing": "Drafting and structural revision",
        "synthesis": "Literature synthesis and comparison",
        "proofreading": "Language polishing and proofreading",
        "formal_tone": "Academic tone revision",
        "tighten": "Condensing and tightening manuscript prose",
        "verify_claim": "Claim verification against cited sources",
        "explain_math": "Mathematical derivation explanation",
        "explain": "Research Q&A and conceptual analysis",
        "fix_latex": "LaTeX syntax and formatting fix",
        "compile_autofix": "LaTeX compilation error fix",
        "visual_to_latex": "Visual-to-LaTeX conversion",
    }
    if pid in mapping:
        return mapping[pid]
    if category == "citation_or_claim":
        return "Citation-bearing content revision"
    if category == "substantive_drafting":
        return "Substantive content drafting or paraphrasing"
    return "AI-assisted manuscript revision"


def classify_hunk(
    *,
    profile_id: str,
    original_text: str,
    replacement_text: str,
    cite_keys: Sequence[str] = (),
    unknown_cite_keys: Sequence[str] = (),
    overlap_warnings: Sequence[Any] = (),
    section_hint: str = "",
) -> IntegrityAssessment:
    """Zero-latency deterministic Tier 1/2/3 classification for a single proposed hunk."""
    pid = (profile_id or "writing").strip().lower()
    reasons: list[str] = []

    has_citations, has_unknown = _detect_citation_signals(
        replacement_text,
        cite_keys=cite_keys,
        unknown_cite_keys=unknown_cite_keys,
    )
    has_overlap = bool(overlap_warnings)

    orig_words = _prose_words(original_text)
    repl_words = _prose_words(replacement_text)
    delta_words = len(repl_words) - len(orig_words)
    net_new_words = max(0, delta_words)
    prose_identical = [w.lower() for w in orig_words] == [w.lower() for w in repl_words]

    if orig_words or repl_words:
        similarity = difflib.SequenceMatcher(
            None,
            [w.lower() for w in orig_words],
            [w.lower() for w in repl_words],
        ).ratio()
    else:
        similarity = 1.0

    # Pure technical profiles remain Tier 1 unless prose or citations are added
    is_technical_profile = pid in {"compile_autofix", "fix_latex", "visual_to_latex"}

    substantive_prose = False
    proportionality_nudge = False

    if prose_identical:
        # Pure LaTeX formatting / macro / equation / table edit
        category = "technical_latex"
        tier: IntegrityTier = 1
    elif (
        is_technical_profile
        and net_new_words < _MINOR_NET_WORD_THRESHOLD
        and (not orig_words or similarity >= _MINOR_SIMILARITY_THRESHOLD)
    ):
        category = "technical_latex"
        tier = 1
    elif pid in {"proofreading", "tighten"}:
        if net_new_words < _MINOR_NET_WORD_THRESHOLD and similarity >= _MINOR_SIMILARITY_THRESHOLD:
            category = "minor_language_polish"
            tier = 1
        else:
            substantive_prose = True
            category = "substantive_paraphrasing"
            tier = 2
            reasons.append(
                f"Substantive phrasing/structure change ({delta_words:+d} prose words, similarity {similarity:.0%})."
            )
    else:
        # writing, synthesis, formal_tone, etc.
        substantive_prose = True
        category = "substantive_drafting"
        tier = 2
        if net_new_words >= _MINOR_NET_WORD_THRESHOLD:
            reasons.append(f"Generated or expanded substantive prose (+{net_new_words} words).")
        else:
            reasons.append(f"Substantive prose revision under '{pid}' profile.")

    if net_new_words >= _PROPORTIONALITY_NEW_WORDS_THRESHOLD:
        proportionality_nudge = True
        reasons.append(
            f"Large generated passage (+{net_new_words} prose words) — review critically for human authorship."
        )

    if has_citations or has_unknown or has_overlap:
        tier = 3
        if has_unknown:
            reasons.append("Contains unverified or unknown citation key(s).")
        elif has_citations:
            reasons.append("Contains citations, BibTeX entries, or DOIs requiring primary-source verification.")
        if has_overlap:
            reasons.append("Contains verbatim overlap warning(s) with indexed source texts.")
        if category in {"technical_latex", "minor_language_polish"}:
            category = "citation_or_claim"

    base_purpose = _purpose_for_profile(pid, category)
    suggested_purpose = f"{base_purpose} ({section_hint})" if section_hint else base_purpose

    return IntegrityAssessment(
        tier=tier,
        category=category,
        reasons=reasons,
        flags=IntegrityFlags(
            has_citations=has_citations,
            has_unknown_citations=has_unknown,
            has_overlap_warnings=has_overlap,
            substantive_prose=substantive_prose,
            proportionality_nudge=proportionality_nudge,
        ),
        suggested_purpose=suggested_purpose,
    )


def classify_turn(
    *,
    profile_id: str,
    user_prompt: str,
    reply: str,
    hunk_assessments: Sequence[IntegrityAssessment] = (),
    has_attachments_or_refs: bool = False,
    selection_text: str | None = None,
) -> IntegrityAssessment:
    """Aggregate hunk-level assessments or classify a chat-only reply into Tier 1/2/3."""
    pid = (profile_id or "writing").strip().lower()

    if hunk_assessments:
        max_tier: IntegrityTier = max(a.tier for a in hunk_assessments)  # type: ignore[assignment]
        reasons: list[str] = []
        for a in hunk_assessments:
            for r in a.reasons:
                if r not in reasons:
                    reasons.append(r)

        reply_cites, reply_unknown = _detect_citation_signals(reply)
        has_citations = any(a.flags.has_citations for a in hunk_assessments) or reply_cites
        has_unknown = any(a.flags.has_unknown_citations for a in hunk_assessments) or reply_unknown
        has_overlap = any(a.flags.has_overlap_warnings for a in hunk_assessments)
        substantive_prose = any(a.flags.substantive_prose for a in hunk_assessments)
        proportionality = any(a.flags.proportionality_nudge for a in hunk_assessments)

        if (has_citations or has_unknown or has_overlap) and max_tier < 3:
            max_tier = 3
            if reply_cites:
                reasons.append("Reply contains citations, BibTeX entries, or DOIs requiring verification.")

        primary = max(hunk_assessments, key=lambda a: a.tier)
        category = primary.category
        if max_tier == 3 and category in {"technical_latex", "minor_language_polish"}:
            category = "citation_or_claim"
        return IntegrityAssessment(
            tier=max_tier,
            category=category,
            reasons=reasons,
            flags=IntegrityFlags(
                has_citations=has_citations,
                has_unknown_citations=has_unknown,
                has_overlap_warnings=has_overlap,
                substantive_prose=substantive_prose,
                proportionality_nudge=proportionality,
            ),
            suggested_purpose=primary.suggested_purpose,
        )

    # Chat-only turn (no proposed hunks)
    reasons = []
    reply_words = _prose_words(reply)
    has_citations, has_unknown = _detect_citation_signals(reply)

    substantive_prose = False
    proportionality_nudge = False
    tier: IntegrityTier = 1
    category = "advisory_qa"

    asks_substantive = bool(_SUMMARIZE_OR_TRANSLATE_RE.search(user_prompt or ""))
    if (
        len(reply_words) >= _CHAT_SUBSTANTIVE_REPLY_WORDS
        and (pid in {"writing", "synthesis", "formal_tone"} or has_attachments_or_refs or asks_substantive)
    ):
        tier = 2
        substantive_prose = True
        category = "substantive_chat_assistance"
        if has_attachments_or_refs and asks_substantive:
            reasons.append("Summarized or synthesized referenced/attached source material.")
        else:
            reasons.append(f"Generated substantive prose/analysis in chat ({len(reply_words)} words).")

    # Proportionality nudge: asking to summarize very short input
    if (
        selection_text
        and asks_substantive
        and "summar" in (user_prompt or "").lower()
        and 0 < len(_prose_words(selection_text)) < _SHORT_SUMMARY_INPUT_WORDS
    ):
        proportionality_nudge = True
        reasons.append(
            "Requested summary of a very short passage — consider reviewing and synthesizing directly."
        )

    if has_citations or has_unknown:
        tier = 3
        reasons.append("Reply references citations, BibTeX entries, or DOIs — verify against primary sources.")
        if category == "advisory_qa":
            category = "citation_or_claim"

    return IntegrityAssessment(
        tier=tier,
        category=category,
        reasons=reasons,
        flags=IntegrityFlags(
            has_citations=has_citations,
            has_unknown_citations=has_unknown,
            has_overlap_warnings=False,
            substantive_prose=substantive_prose,
            proportionality_nudge=proportionality_nudge,
        ),
        suggested_purpose=_purpose_for_profile(pid, category),
    )
