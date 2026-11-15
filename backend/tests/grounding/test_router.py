from __future__ import annotations

from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.config import bootstrap_data_dir
from app.grounding.scholarly_client import ScholarlyWork
from app.main import create_app


def _login_and_get_csrf(
    client: TestClient, password: str = "grounding-pass"
) -> dict[str, str]:
    res = client.post("/api/auth/login", json={"password": password})
    assert res.status_code == 200
    csrf = res.json()["csrf_token"]
    return {"X-CSRF-Token": csrf}


def test_search_is_read_only_and_post_stage_stages_candidate_with_csrf(
    tmp_path: Path,
) -> None:
    settings = bootstrap_data_dir(tmp_path / "data").model_copy(
        update={
            "workspace_password": "grounding-pass",
            "privacy_consent_accepted": True,
        }
    )
    app = create_app(settings=settings)

    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)

        # Create project
        proj_res = client.post(
            "/api/projects",
            json={"name": "Grounding Test Project"},
            headers=headers,
        )
        assert proj_res.status_code == 201
        project_id = proj_res.json()["project_id"]

        # Mock ScholarlyClient on app.state
        mock_work = ScholarlyWork(
            title="Transformer Reasoning Models",
            authors=["Vaswani, Ashish"],
            year=2017,
            doi="10.48550/arxiv.1706.03762",
            arxiv_id="1706.03762",
            abstract="Transformer sequence models.",
            oa_pdf_url="https://arxiv.org/pdf/1706.03762.pdf",
            source_api="semantic_scholar",
            metadata_verified=True,
            injection_flagged=False,
        )

        async def mock_search(query: str, limit: int = 10) -> list[ScholarlyWork]:
            return [mock_work]

        app.state.scholarly_client.search_works = mock_search

        # 1. GET /grounding/search is read-only (returns ScholarlyWork[])
        search_res = client.get(
            f"/api/projects/{project_id}/grounding/search?q=Transformer",
            headers=headers,
        )
        assert search_res.status_code == 200
        works = search_res.json()["works"]
        assert len(works) == 1
        assert works[0]["title"] == "Transformer Reasoning Models"

        # Verify search did not stage any candidate
        candidates = app.state.staging_repo.list_active_candidates(project_id)
        assert len(candidates) == 0

        # 2. POST /grounding/stage requires CSRF and stages the candidate
        no_csrf = client.post(
            f"/api/projects/{project_id}/grounding/stage",
            json={"work": works[0]},
        )
        assert no_csrf.status_code == 403

        stage_res = client.post(
            f"/api/projects/{project_id}/grounding/stage",
            json={"work": works[0]},
            headers=headers,
        )
        assert stage_res.status_code == 200
        staged_cand = stage_res.json()["candidate"]
        assert staged_cand["cite_key"].startswith("vaswani2017")
        assert staged_cand["status"] == "staged"

        # Verify candidate is now staged in SQLite
        active = app.state.staging_repo.list_active_candidates(project_id)
        assert len(active) == 1
        assert active[0].doi == "10.48550/arxiv.1706.03762"


def test_chat_service_and_verify_draft_run_doi_overlap_and_claim_evidence(
    tmp_path: Path,
) -> None:
    settings = bootstrap_data_dir(tmp_path / "data").model_copy(
        update={
            "workspace_password": "grounding-pass",
            "privacy_consent_accepted": True,
            "overlap_ngram_words": 8,
        }
    )
    app = create_app(settings=settings)

    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)

        proj_res = client.post(
            "/api/projects",
            json={"name": "Grounding Pipeline Project"},
            headers=headers,
        )
        assert proj_res.status_code == 201
        project_id = proj_res.json()["project_id"]

        # Index a PDF chunk with 10 words
        indexer = app.state.pdf_indexer
        fake_pdf = tmp_path / "corpus.pdf"
        import pymupdf
        doc = pymupdf.open()
        doc.new_page()
        doc.save(str(fake_pdf))
        doc.close()
        indexer.index_pdf(
            project_id=project_id,
            pdf_path=fake_pdf,
            cite_key="vaswani2017",
        )
        # Directly insert test chunk text to test verbatim overlap and claim evidence
        with app.state.db.get_connection() as conn:
            conn.execute(
                """
                UPDATE pdf_chunks
                SET text = 'The dominant sequence transduction models are based on complex recurrent or convolutional neural networks.'
                WHERE project_id = ? AND cite_key = 'vaswani2017';
                """,
                (project_id,),
            )

        # 1. POST /api/projects/{project_id}/grounding/verify-draft
        draft_with_verbatim_copy = (
            "In this study, the dominant sequence transduction models are based on complex "
            "recurrent or convolutional neural networks \\cite{vaswani2017}."
        )

        verify_res = client.post(
            f"/api/projects/{project_id}/grounding/verify-draft",
            json={"draft_text": draft_with_verbatim_copy, "file_path": "main.tex"},
            headers=headers,
        )
        assert verify_res.status_code == 200
        data = verify_res.json()
        assert len(data["overlap_warnings"]) >= 1
        assert data["overlap_warnings"][0]["cite_key"] == "vaswani2017"
        assert data["overlap_warnings"][0]["word_count"] >= 8

        assert len(data["claim_evidence"]) >= 1
        assert data["claim_evidence"][0]["cite_key"] == "vaswani2017"
        assert data["claim_evidence"][0]["grounding_tier"] == "full_text_pdf"
        assert data["claim_evidence"][0]["page_number"] == 1
