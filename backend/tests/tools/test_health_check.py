import pytest
from pathlib import Path
from app.db import DatabaseManager
from app.tools.health_check import run_citation_health_check, HealthCheckReport

def test_health_check_detects_missing_keys_uncited_bib_entries_and_uncited_claims(tmp_path: Path):
    db_path = tmp_path / "app.db"
    backup_dir = tmp_path / "backups"
    db = DatabaseManager(db_path=db_path, backup_dir=backup_dir)
    db.initialize()

    project_dir = tmp_path / "projects" / "test_proj"
    project_dir.mkdir(parents=True)
    chapters_dir = project_dir / "chapters"
    chapters_dir.mkdir()

    # Create references.bib with two entries: 'vaswani2017' and 'unused2020'
    bib_content = """
@article{vaswani2017,
  title={Attention is all you need},
  author={Vaswani, Ashish},
  year={2017}
}

@article{unused2020,
  title={An unused survey},
  author={Smith, John},
  year={2020}
}
"""
    (project_dir / "references.bib").write_text(bib_content)

    # Root tex includes chapter1.tex
    main_tex = r"""\documentclass{article}
\usepackage{cite}
\begin{document}
\input{chapters/chapter1.tex}
\bibliography{references}
\end{document}
"""
    (project_dir / "main.tex").write_text(main_tex)

    # chapter1.tex cites vaswani2017 (valid) and missing2024 (missing from bib),
    # contains an uncited empirical claim sentence,
    # and cites abstract_only_key.
    chapter1_tex = r"""\section{Introduction}
Transformers revolutionized NLP \cite{vaswani2017}.
Recent work proposed another architecture \cite{missing2024}.
Our proposed method improves accuracy by 25% over baseline.
Another paper was cited here \cite{abstract_only_key}.
"""
    (chapters_dir / "chapter1.tex").write_text(chapter1_tex)

    # In database, add abstract_only_key to candidate_sources with grounding_tier='abstract_only'
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO candidate_sources (
                candidate_id, project_id, cite_key, title, authors_json, year,
                source_api, bibtex, grounding_tier, created_at, expires_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "cand_1", "test_proj", "abstract_only_key", "Abstract Title",
                "[\"Doe, Jane\"]", 2023, "semantic_scholar",
                "@article{abstract_only_key, title={Abstract Title}}",
                "abstract_only", "2026-01-01T00:00:00Z", "2026-01-15T00:00:00Z", "staged"
            )
        )

    report = run_citation_health_check(project_dir, "main.tex", db)

    assert isinstance(report, HealthCheckReport)
    # Check missing bib keys (missing2024, and abstract_only_key if not in references.bib)
    missing_keys = [item["key"] for item in report.missing_bib_keys]
    assert "missing2024" in missing_keys

    # Check uncited bib entries
    assert "unused2020" in report.uncited_bib_keys
    assert "vaswani2017" not in report.uncited_bib_keys

    # Check uncited factual claims
    assert len(report.uncited_factual_claims) >= 1
    claims_text = " ".join([c.get("sentence", "") or c.get("claim", "") for c in report.uncited_factual_claims])
    assert "25%" in claims_text or "improves accuracy" in claims_text

    # Check abstract-only citations
    assert "abstract_only_key" in report.abstract_only_citations


def test_health_check_nocite_star_suppresses_uncited_bib_keys(tmp_path: Path):
    project_dir = tmp_path / "projects" / "test_proj_nocite"
    project_dir.mkdir(parents=True)
    (project_dir / "references.bib").write_text("@article{key1, title={Title 1}}\n@article{key2, title={Title 2}}")
    (project_dir / "main.tex").write_text(r"""\documentclass{article}
\begin{document}
\nocite{*}
\bibliography{references}
\end{document}
""")
    report = run_citation_health_check(project_dir, "main.tex")
    assert report.uncited_bib_keys == []
