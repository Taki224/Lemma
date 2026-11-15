from __future__ import annotations

import re
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Literal

from app.config import AppSettings
from app.db import DatabaseManager
from app.grounding.pdf_indexer import IndexedPdfChunk, scan_for_prompt_injection

_CITE_CMD_RE = re.compile(r"\\(?:[a-zA-Z]*cite[a-zA-Z]*\*?)(?:\[[^\]]*\])*\{([^}]+)\}", re.IGNORECASE)
_LATEX_MACRO_RE = re.compile(r"\\[a-zA-Z]+(?:\[[^\]]*\])*\{([^}]*)\}")
_LATEX_COMMANDS_RE = re.compile(r"\\[a-zA-Z]+")
_MATH_BLOCK_RE = re.compile(r"\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]")
_MATH_INLINE_RE = re.compile(r"(?<!\\)\$[^$]+(?<!\\)\$|\\\([^\)]+\\\)")
_DEHYPHEN_RE = re.compile(r"(\w+)[-\xad]\s*\n\s*(\w+)")
_WORD_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class OverlapMatch:
    matched_text: str
    word_count: int
    cite_key: str | None
    doc_id: str
    page_number: int | None


@dataclass(frozen=True)
class ClaimEvidence:
    claim_text: str
    cite_key: str
    grounding_tier: Literal["full_text_pdf", "abstract_only"]
    snippet: str
    page_number: int | None
    injection_warning: bool


def normalize_to_words(text: str) -> list[str]:
    """Applies NFKC normalization, dehyphenation, strips LaTeX commands, and tokenizes into words."""
    if not text:
        return []
    # 1. NFKC normalization
    norm = unicodedata.normalize("NFKC", text)

    # 2. Dehyphenate soft hyphens and line-break split words: "trans-\nduction" -> "transduction"
    norm = _DEHYPHEN_RE.sub(r"\1\2", norm)
    norm = norm.replace("\xad", "")

    # 3. Strip LaTeX math blocks and inline math
    norm = _MATH_BLOCK_RE.sub(" ", norm)
    norm = _MATH_INLINE_RE.sub(" ", norm)

    # 4. Strip citations
    norm = _CITE_CMD_RE.sub(" ", norm)

    # 5. Unwrap formatting macros: \textbf{word} -> word, \emph{word} -> word (repeat to handle nesting)
    for _ in range(3):
        norm = _LATEX_MACRO_RE.sub(r"\1", norm)

    # 6. Strip remaining standalone commands
    norm = _LATEX_COMMANDS_RE.sub(" ", norm)

    # 7. Tokenize alphanumeric words in lowercase
    return [w.lower() for w in _WORD_TOKEN_RE.findall(norm)]


def check_copy_overlap(
    generated_text: str,
    corpus_chunks: list[IndexedPdfChunk],
    candidate_and_bib_abstracts: list[dict[str, Any]] | None = None,
    min_words: int | None = None,
    settings: AppSettings | None = None,
) -> list[OverlapMatch]:
    """Detects contiguous verbatim n-gram overlap of >= min_words words against corpus chunks and abstracts."""
    threshold = min_words or (settings.overlap_ngram_words if settings else 8)
    gen_words = normalize_to_words(generated_text)
    if len(gen_words) < threshold:
        return []

    # Build n-grams from generated text
    gen_ngrams: dict[tuple[str, ...], int] = {}
    for i in range(len(gen_words) - threshold + 1):
        ngram = tuple(gen_words[i : i + threshold])
        if ngram not in gen_ngrams:
            gen_ngrams[ngram] = i

    matches: list[OverlapMatch] = []
    seen_matches: set[tuple[str, int | None, str]] = set()

    # 1. Check against full-text PDF corpus chunks
    for chunk in corpus_chunks:
        chunk_words = normalize_to_words(chunk.text)
        if len(chunk_words) < threshold:
            continue

        chunk_ngrams_set = set()
        for j in range(len(chunk_words) - threshold + 1):
            chunk_ngrams_set.add(tuple(chunk_words[j : j + threshold]))

        for ngram, start_idx in gen_ngrams.items():
            if ngram in chunk_ngrams_set:
                # Find maximum contiguous overlap length starting at start_idx
                matched_words = list(ngram)
                k = threshold
                while (
                    start_idx + k < len(gen_words)
                    and tuple(gen_words[start_idx + k + 1 - threshold : start_idx + k + 1]) in chunk_ngrams_set
                ):
                    matched_words.append(gen_words[start_idx + k])
                    k += 1

                matched_str = " ".join(matched_words)
                key = (chunk.doc_id, chunk.page_number, matched_str)
                if key not in seen_matches:
                    seen_matches.add(key)
                    matches.append(
                        OverlapMatch(
                            matched_text=matched_str,
                            word_count=len(matched_words),
                            cite_key=chunk.cite_key,
                            doc_id=chunk.doc_id,
                            page_number=chunk.page_number,
                        )
                    )

    # 2. Check against candidate and references.bib abstracts
    if candidate_and_bib_abstracts:
        for item in candidate_and_bib_abstracts:
            abs_text = item.get("abstract") or ""
            abs_words = normalize_to_words(abs_text)
            if len(abs_words) < threshold:
                continue

            abs_ngrams_set = set()
            for j in range(len(abs_words) - threshold + 1):
                abs_ngrams_set.add(tuple(abs_words[j : j + threshold]))

            doc_id = str(item.get("doc_id") or item.get("candidate_id") or "abstract")
            cite_key = item.get("cite_key")

            for ngram, start_idx in gen_ngrams.items():
                if ngram in abs_ngrams_set:
                    matched_words = list(ngram)
                    k = threshold
                    while (
                        start_idx + k < len(gen_words)
                        and tuple(gen_words[start_idx + k + 1 - threshold : start_idx + k + 1]) in abs_ngrams_set
                    ):
                        matched_words.append(gen_words[start_idx + k])
                        k += 1

                    matched_str = " ".join(matched_words)
                    key = (doc_id, None, matched_str)
                    if key not in seen_matches:
                        seen_matches.add(key)
                        matches.append(
                            OverlapMatch(
                                matched_text=matched_str,
                                word_count=len(matched_words),
                                cite_key=cite_key,
                                doc_id=doc_id,
                                page_number=None,
                            )
                        )

    # Filter out sub-spans of longer matches on the same document
    filtered: list[OverlapMatch] = []
    matches.sort(key=lambda m: m.word_count, reverse=True)
    for m in matches:
        if not any(m.matched_text in other.matched_text and m.doc_id == other.doc_id and m != other for other in filtered):
            filtered.append(m)
    return filtered


def extract_claim_evidence(
    generated_text: str,
    project_id: str,
    db: DatabaseManager,
) -> list[ClaimEvidence]:
    """Extracts per-sentence claims with citations and matches them against PDF chunks or abstracts."""
    if not generated_text:
        return []

    # Split into approximate sentences
    raw_sentences = re.split(r"(?<=[.!?])\s+", generated_text)
    claim_items: list[tuple[str, str]] = []  # (sentence, cite_key)

    for sentence in raw_sentences:
        matches = _CITE_CMD_RE.findall(sentence)
        for key_group in matches:
            for k in key_group.split(","):
                clean_k = k.strip()
                if clean_k:
                    claim_items.append((sentence.strip(), clean_k))

    evidence_results: list[ClaimEvidence] = []
    seen_claims: set[tuple[str, str]] = set()

    with closing(db.get_connection()) as conn:
        for claim_text, cite_key in claim_items:
            if (claim_text, cite_key) in seen_claims:
                continue
            seen_claims.add((claim_text, cite_key))

            claim_tokens = set(normalize_to_words(claim_text))

            # 1. Search for indexed PDF chunks with this cite_key (case-insensitive)
            chunk_rows = conn.execute(
                """
                SELECT doc_id, page_number, text, injection_flagged
                FROM pdf_chunks
                WHERE project_id = ? AND cite_key = ? COLLATE NOCASE
                ORDER BY page_number ASC;
                """,
                (project_id, cite_key),
            ).fetchall()

            if chunk_rows:
                best_row = None
                best_overlap = -1
                for r in chunk_rows:
                    r_tokens = set(normalize_to_words(str(r["text"])))
                    overlap_len = len(claim_tokens.intersection(r_tokens))
                    if overlap_len > best_overlap:
                        best_overlap = overlap_len
                        best_row = r

                target_row = best_row if best_row is not None else chunk_rows[0]
                text_val = str(target_row["text"])
                snippet = text_val[:300].strip()
                flagged = bool(target_row["injection_flagged"]) or bool(scan_for_prompt_injection(snippet))

                evidence_results.append(
                    ClaimEvidence(
                        claim_text=claim_text,
                        cite_key=cite_key,
                        grounding_tier="full_text_pdf",
                        snippet=snippet,
                        page_number=int(target_row["page_number"]),
                        injection_warning=flagged,
                    )
                )
                continue

            # 2. Fallback to candidate_sources abstract
            cand_row = conn.execute(
                """
                SELECT abstract, title
                FROM candidate_sources
                WHERE project_id = ? AND cite_key = ? COLLATE NOCASE
                ORDER BY created_at DESC LIMIT 1;
                """,
                (project_id, cite_key),
            ).fetchone()

            if cand_row and cand_row["abstract"]:
                abs_text = str(cand_row["abstract"]).strip()
                flagged = bool(scan_for_prompt_injection(abs_text))
                evidence_results.append(
                    ClaimEvidence(
                        claim_text=claim_text,
                        cite_key=cite_key,
                        grounding_tier="abstract_only",
                        snippet=abs_text[:300],
                        page_number=None,
                        injection_warning=flagged,
                    )
                )
            elif cand_row and cand_row["title"]:
                title_snippet = str(cand_row["title"]).strip()
                evidence_results.append(
                    ClaimEvidence(
                        claim_text=claim_text,
                        cite_key=cite_key,
                        grounding_tier="abstract_only",
                        snippet=title_snippet[:300],
                        page_number=None,
                        injection_warning=False,
                    )
                )

    return evidence_results
