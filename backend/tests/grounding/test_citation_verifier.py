from __future__ import annotations

from pathlib import Path

from app.grounding.citation_verifier import (
    BibEntry,
    CitationVerificationResult,
    extract_cite_keys,
    parse_bib_file,
    verify_citations,
)


def test_extracts_all_natbib_biblatex_and_tricky_cite_macros_ignoring_unescaped_comments() -> None:
    latex_snippet = r"""
% \cite{ignored_full_line_comment}
We achieved 100\% accuracy \cite{real2024} in our benchmark % \citep{ignored_trailing}
As shown by \citet{authorA2020} and \citealp[see][p.~12]{k1, k2}, the bound holds.
Furthermore, \Citep[cf.][]{k3} and \autocites[p.~5]{k4}{k5} confirm this alongside \parencite*{k6}.
Also test \textcite{k7}, \footcite{k8}, \smartcite{k9}, \citeauthor{k10}, \citeyear{k11}.
Wildcard \nocite{*} should be ignored, whereas \nocite{explicit_nocite} is extracted.
\begin{verbatim}
\cite{ignored_in_verbatim}
\end{verbatim}
\begin{lstlisting}
\cite{ignored_in_lstlisting}
\end{lstlisting}
"""
    keys = extract_cite_keys(latex_snippet)
    assert "real2024" in keys
    assert "authorA2020" in keys
    assert "k1" in keys
    assert "k2" in keys
    assert "k3" in keys
    assert "k4" in keys
    assert "k5" in keys
    assert "k6" in keys
    assert "k7" in keys
    assert "k8" in keys
    assert "k9" in keys
    assert "k10" in keys
    assert "k11" in keys
    assert "explicit_nocite" in keys

    # Negative assertions
    assert "*" not in keys
    assert "ignored_full_line_comment" not in keys
    assert "ignored_trailing" not in keys
    assert "ignored_in_verbatim" not in keys
    assert "ignored_in_lstlisting" not in keys


def test_blocks_hallucinated_cite_key_and_allows_bib_or_staged_keys() -> None:
    bib_text = """
@string{neurips = "Advances in Neural Information Processing Systems"}
% A comment line in bib
@inproceedings{vaswani2017attention,
  author = {Vaswani, Ashish and Shazeer, Noam},
  title = {Attention Is All You Need},
  booktitle = neurips,
  year = {2017},
  doi = {10.48550/arXiv.1706.03762}
}
"""
    bib_entries = parse_bib_file(bib_text)
    assert "vaswani2017attention" in bib_entries
    entry: BibEntry = bib_entries["vaswani2017attention"]
    assert entry.entry_type == "inproceedings"
    assert entry.title == "Attention Is All You Need"
    assert entry.year == "2017"
    assert entry.doi == "10.48550/arXiv.1706.03762"
    assert len(entry.authors) == 2

    # 1. Valid bib key + valid staged key -> not blocked
    draft_ok = r"Transformer models \cite{vaswani2017attention} and staged work \citep{staged2026new}."
    res_ok: CitationVerificationResult = verify_citations(
        generated_latex=draft_ok,
        bib_entries=bib_entries,
        staged_keys={"staged2026new"},
    )
    assert res_ok.is_blocked is False
    assert res_ok.valid_keys == ["vaswani2017attention"]
    assert res_ok.staged_keys == ["staged2026new"]
    assert res_ok.unknown_keys == []

    # 2. Hallucinated key -> blocked
    draft_bad = r"As proven in \cite{vaswani2017attention, nonexistent2099hallucinated}."
    res_bad = verify_citations(
        generated_latex=draft_bad,
        bib_entries=bib_entries,
        staged_keys={"staged2026new"},
    )
    assert res_bad.is_blocked is True
    assert res_bad.valid_keys == ["vaswani2017attention"]
    assert res_bad.unknown_keys == ["nonexistent2099hallucinated"]


def test_false_positive_rate_under_2_percent_on_reference_manuscript() -> None:
    fixture_dir = (
        Path(__file__).resolve().parent.parent / "fixtures" / "reference_manuscript"
    )
    bib_path = fixture_dir / "references.bib"
    bib_entries = parse_bib_file(bib_path.read_text(encoding="utf-8"))
    assert len(bib_entries) >= 5

    tex_files = [
        fixture_dir / "main.tex",
        fixture_dir / "chapters" / "introduction.tex",
        fixture_dir / "chapters" / "methodology.tex",
    ]
    total_citations_checked = 0
    false_positives = 0

    for tex_path in tex_files:
        content = tex_path.read_text(encoding="utf-8")
        extracted = extract_cite_keys(content)
        total_citations_checked += len(extracted)
        result = verify_citations(
            generated_latex=content,
            bib_entries=bib_entries,
            staged_keys=set(),
        )
        false_positives += len(result.unknown_keys)
        assert result.is_blocked is False

    assert total_citations_checked > 0
    false_positive_rate = false_positives / total_citations_checked
    assert false_positive_rate == 0.0
    assert false_positive_rate <= 0.02
