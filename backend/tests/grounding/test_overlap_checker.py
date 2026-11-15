from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from app.config import bootstrap_data_dir
from app.db import DatabaseManager
from app.grounding.overlap_checker import (
    ClaimEvidence,
    IndexedPdfChunk,
    OverlapMatch,
    check_copy_overlap,
    extract_claim_evidence,
)
from app.grounding.pdf_indexer import PdfIndexer
from app.zotero.staging_repo import CandidateStagingRepository


def test_flags_8_word_verbatim_overlap_across_pdf_and_abstracts_with_nfkc_and_dehyphenation(
    tmp_path: Path,
) -> None:
    settings = bootstrap_data_dir(tmp_path / "data").model_copy(
        update={"overlap_ngram_words": 8}
    )

    corpus_chunks = [
        IndexedPdfChunk(
            doc_id="doc_pdf_1",
            cite_key="vaswani2017",
            page_number=3,
            text=(
                "The dominant sequence trans-\nduction models are based on complex "
                "recurrent or convolutional neural networks."
            ),
            injection_flagged=False,
            injection_matches=[],
        )
    ]

    candidate_abstracts = [
        {
            "cite_key": "radford2018gpt",
            "abstract": "We demonstrate that large gains on these tasks can be achieved by generative pre-training.",
            "doc_id": "cand_abs_1",
        }
    ]

    # Exactly 8 consecutive words matching the PDF chunk across dehyphenation "trans-\nduction"
    # and with LaTeX formatting macros (\textbf{...}, \emph{...}, \cite{...})
    generated_text = (
        "In our research, the \\textbf{dominant} \\emph{sequence} transduction models "
        "are based on complex \\cite{vaswani2017} recurrent or convolutional networks."
    )

    matches = check_copy_overlap(
        generated_text=generated_text,
        corpus_chunks=corpus_chunks,
        candidate_and_bib_abstracts=candidate_abstracts,
        min_words=8,
        settings=settings,
    )

    assert len(matches) >= 1
    pdf_match = matches[0]
    assert pdf_match.cite_key == "vaswani2017"
    assert pdf_match.page_number == 3
    assert pdf_match.word_count >= 8

    # 7 consecutive words must NOT trigger the overlap check when min_words=8
    short_text = "The dominant sequence transduction models are based."
    short_matches = check_copy_overlap(
        generated_text=short_text,
        corpus_chunks=corpus_chunks,
        candidate_and_bib_abstracts=candidate_abstracts,
        min_words=8,
        settings=settings,
    )
    assert len(short_matches) == 0

    # Overlap with candidate abstract (≥ 8 words)
    abstract_text = (
        "We demonstrate that large gains on these tasks can be achieved by generative pre-training."
    )
    abs_matches = check_copy_overlap(
        generated_text=abstract_text,
        corpus_chunks=corpus_chunks,
        candidate_and_bib_abstracts=candidate_abstracts,
        min_words=8,
        settings=settings,
    )
    assert len(abs_matches) >= 1
    assert abs_matches[0].cite_key == "radford2018gpt"
    assert abs_matches[0].word_count >= 8


def test_extract_claim_evidence_returns_page_number_for_pdf_and_abstract_badge_when_missing(
    tmp_path: Path,
) -> None:
    settings = bootstrap_data_dir(tmp_path / "data")
    db = DatabaseManager(db_path=settings.db_path, backup_dir=settings.backups_dir)
    db.initialize()

    indexer = PdfIndexer(db=db)
    repo = CandidateStagingRepository(db=db)

    # 1. Index a PDF chunk with full text for lewis2020rag
    fake_pdf = tmp_path / "rag.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake rag")
    with pytest.MonkeyPatch.context() as m:
        m.setattr(
            "app.grounding.pdf_indexer.to_markdown_chunks",
            lambda path: [
                {
                    "page": 5,
                    "text": "Retrieval-Augmented Generation models combine pre-trained parametric and non-parametric memory.",
                }
            ],
        )
        indexer.index_pdf("proj-grounding", fake_pdf, cite_key="lewis2020rag")

    # 2. Stage an abstract-only candidate for smith2024abstract
    repo.stage_candidate(
        project_id="proj-grounding",
        cite_key="smith2024abstract",
        title="Abstract Only Paper",
        authors=["Smith, Alice"],
        year=2024,
        doi="10.1000/abstract.only",
        abstract="Neural theorem proving can be accelerated by heuristic lemmas.",
        source_api="crossref",
        bibtex="@article{smith2024abstract, title={Abstract Only Paper}}",
        grounding_tier="abstract_only",
    )

    draft_text = (
        "Recent breakthroughs show hybrid memories outperform pure models \\cite{lewis2020rag}. "
        "Furthermore, lemma heuristics accelerate automated reasoning \\cite{smith2024abstract}."
    )

    evidence_list = extract_claim_evidence(draft_text, project_id="proj-grounding", db=db)

    assert len(evidence_list) == 2

    # First claim (lewis2020rag) -> grounded on full-text PDF with page number
    ev1 = next(e for e in evidence_list if e.cite_key == "lewis2020rag")
    assert ev1.grounding_tier == "full_text_pdf"
    assert ev1.page_number == 5
    assert "Retrieval-Augmented Generation" in ev1.snippet

    # Second claim (smith2024abstract) -> grounded on abstract only
    ev2 = next(e for e in evidence_list if e.cite_key == "smith2024abstract")
    assert ev2.grounding_tier == "abstract_only"
    assert ev2.page_number is None
    assert "heuristic lemmas" in ev2.snippet
