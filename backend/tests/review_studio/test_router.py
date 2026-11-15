from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.ai.openrouter import CompletionResult
from app.config import bootstrap_data_dir
from app.main import create_app


def test_review_estimate_run_with_confirmation_and_fetch_resolved_report(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = bootstrap_data_dir(data_dir).model_copy(
        update={
            "workspace_password": "testpassword123",
            "review_confirm_usd": 0.50,
            "privacy_consent_accepted": True,
        }
    )

    app = create_app(settings=settings)

    with TestClient(app) as client:
        # 1. Login
        login_res = client.post(
            "/api/auth/login",
            json={"password": "testpassword123"},
        )
        assert login_res.status_code == 200
        csrf_token = login_res.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token}

        # 2. Create project
        create_res = client.post(
            "/api/projects",
            json={"name": "Review Paper Project"},
            headers=headers,
        )
        assert create_res.status_code == 201
        project_id = create_res.json()["project_id"]

        # 3. Write a manuscript file
        write_res = client.put(
            f"/api/projects/{project_id}/files/main.tex",
            json={
                "content": (
                    "\\documentclass{article}\n"
                    "\\begin{document}\n"
                    "We present a novel quantum transformer.\n"
                    "The convergence rate is strictly linear.\n"
                    "\\end{document}\n"
                )
            },
            headers=headers,
        )
        assert write_res.status_code == 200

        # Create dummy .build/main.pdf
        project_dir = settings.data_dir / "projects" / project_id
        build_dir = project_dir / ".build"
        build_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = build_dir / "main.pdf"
        pdf_path.write_bytes(b"%PDF-1.5 fake")

        # 4. Test estimate endpoint
        estimate_payload = {
            "assignments": [
                {"persona_id": "methodology_critic", "model": "anthropic/claude-3.7-sonnet"},
                {"persona_id": "literature_auditor", "model": "google/gemini-2.5-pro"},
            ],
            "include_visual": False,
        }

        with patch("app.review_studio.text_review._read_pdf_page_count", return_value=8):
            with patch.object(
                app.state.openrouter_client,
                "estimate_cost_usd",
                return_value=0.35,  # 2 * 0.35 = 0.70 >= 0.50
            ):
                est_res = client.post(
                    f"/api/projects/{project_id}/reviews/estimate",
                    json=estimate_payload,
                    headers=headers,
                )
        assert est_res.status_code == 200
        est_data = est_res.json()
        assert est_data["page_count"] == 8
        assert est_data["estimated_usd"] == pytest.approx(0.70)
        assert est_data["requires_confirmation"] is True

        # 5. Test run without confirmation -> should fail with 400 Confirmation Required
        run_payload = {
            **estimate_payload,
            "confirm_high_cost": False,
        }

        with patch.object(
            app.state.openrouter_client,
            "estimate_cost_usd",
            return_value=0.35,
        ):
            run_fail_res = client.post(
                f"/api/projects/{project_id}/reviews/run",
                json=run_payload,
                headers=headers,
            )
        assert run_fail_res.status_code == 400
        assert "confirmation" in run_fail_res.json()["detail"].lower()

        # 6. Test run WITH confirmation -> succeeds
        run_payload["confirm_high_cost"] = True

        mock_llm_output = [
            {
                "severity": "major",
                "file_path": "main.tex",
                "line_number": 4,
                "page_number": 1,
                "quoted_anchor": "The convergence rate is strictly linear.",
                "title": "Unsubstantiated convergence rate",
                "critique": "No proof or theorem is provided for linear convergence.",
                "suggested_fix": "Add formal theorem and proof in Appendix.",
            }
        ]

        app.state.openrouter_client.complete = AsyncMock(  # type: ignore[method-assign]
            return_value=CompletionResult(
                content=json.dumps(mock_llm_output),
                model="anthropic/claude-3.7-sonnet",
                prompt_tokens=150,
                completion_tokens=80,
                cost_usd=0.02,
                reservation_id="res_test_rev",
            )
        )

        with patch.object(
            app.state.openrouter_client,
            "estimate_cost_usd",
            return_value=0.35,
        ):
            run_success_res = client.post(
                f"/api/projects/{project_id}/reviews/run",
                json=run_payload,
                headers=headers,
            )

        assert run_success_res.status_code == 200
        run_data = run_success_res.json()
        assert "report_id" in run_data
        assert "snapshot_sha" in run_data
        report_id = run_data["report_id"]
        assert len(run_data["issues"]) > 0

        # 7. List reviews
        list_res = client.get(
            f"/api/projects/{project_id}/reviews",
            headers=headers,
        )
        assert list_res.status_code == 200
        reports = list_res.json()["reports"]
        assert len(reports) >= 1
        assert any(r["report_id"] == report_id for r in reports)

        # 8. Fetch specific review with re-resolved issue locations
        fetch_res = client.get(
            f"/api/projects/{project_id}/reviews/{report_id}",
            headers=headers,
        )
        assert fetch_res.status_code == 200
        report_detail = fetch_res.json()
        assert report_detail["report_id"] == report_id
        assert len(report_detail["issues"]) > 0

        # 9. Path traversal in report_id returns 400
        traversal_res = client.get(
            f"/api/projects/{project_id}/reviews/..%2F..%2Fetc%2Fpasswd",
            headers=headers,
        )
        assert traversal_res.status_code in (400, 404)
        iss = report_detail["issues"][0]
        assert iss["title"] == "Unsubstantiated convergence rate"
        assert iss["stale"] is False
        assert iss["resolved_line"] == 4


def test_review_returns_403_for_non_enabled_model(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = bootstrap_data_dir(data_dir).model_copy(
        update={
            "workspace_password": "testpassword123",
            "privacy_consent_accepted": True,
        }
    )
    app = create_app(settings=settings)

    with TestClient(app) as client:
        login_res = client.post("/api/auth/login", json={"password": "testpassword123"})
        assert login_res.status_code == 200
        headers = {"X-CSRF-Token": login_res.json()["csrf_token"]}

        create_res = client.post(
            "/api/projects", json={"name": "Review 403"}, headers=headers
        )
        assert create_res.status_code == 201
        project_id = create_res.json()["project_id"]
        client.put(
            f"/api/projects/{project_id}/files/main.tex",
            json={"content": "\\documentclass{article}\\begin{document}Hi\\end{document}\n"},
            headers=headers,
        )

        res = client.post(
            f"/api/projects/{project_id}/reviews/run",
            json={
                "assignments": [
                    {"persona_id": "methodology_critic", "model": "vendor/not-enabled-model"}
                ],
                "include_visual": False,
                "confirm_high_cost": True,
            },
            headers=headers,
        )

        assert res.status_code == 403
        assert "not enabled" in res.json()["detail"]


def _review_403_setup(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = bootstrap_data_dir(data_dir).model_copy(
        update={
            "workspace_password": "testpassword123",
            "privacy_consent_accepted": True,
        }
    )
    return create_app(settings=settings)


def _create_review_project(client: TestClient, headers: dict[str, str]) -> str:
    create_res = client.post("/api/projects", json={"name": "Review pre-check"}, headers=headers)
    assert create_res.status_code == 201
    project_id = create_res.json()["project_id"]
    client.put(
        f"/api/projects/{project_id}/files/main.tex",
        json={"content": "\\documentclass{article}\\begin{document}Hi\\end{document}\n"},
        headers=headers,
    )
    return project_id


def test_review_403_when_any_assignment_model_not_enabled_writes_nothing(
    tmp_path: Path,
) -> None:
    app = _review_403_setup(tmp_path)

    with TestClient(app) as client:
        login_res = client.post("/api/auth/login", json={"password": "testpassword123"})
        assert login_res.status_code == 200
        headers = {"X-CSRF-Token": login_res.json()["csrf_token"]}
        project_id = _create_review_project(client, headers)

        with patch.object(
            app.state.openrouter_client, "complete", new_callable=AsyncMock
        ) as mock_complete:
            res = client.post(
                f"/api/projects/{project_id}/reviews/run",
                json={
                    "assignments": [
                        {
                            "persona_id": "methodology_critic",
                            "model": "anthropic/claude-3.7-sonnet",
                        },
                        {"persona_id": "methodology_critic", "model": "vendor/not-enabled-model"},
                    ],
                    "include_visual": False,
                    "confirm_high_cost": True,
                },
                headers=headers,
            )
            mock_complete.assert_not_awaited()

        assert res.status_code == 403
        assert "vendor/not-enabled-model" in res.json()["detail"]
        assert "not enabled" in res.json()["detail"]
        assert "anthropic/claude-3.7-sonnet" not in res.json()["detail"]
        reviews = client.get(f"/api/projects/{project_id}/reviews")
        assert reviews.status_code == 200
        assert reviews.json()["reports"] == []


def test_review_403_when_vision_model_not_enabled(tmp_path: Path) -> None:
    app = _review_403_setup(tmp_path)

    with TestClient(app) as client:
        login_res = client.post("/api/auth/login", json={"password": "testpassword123"})
        assert login_res.status_code == 200
        headers = {"X-CSRF-Token": login_res.json()["csrf_token"]}
        project_id = _create_review_project(client, headers)

        with patch.object(
            app.state.openrouter_client, "complete", new_callable=AsyncMock
        ) as mock_complete:
            res = client.post(
                f"/api/projects/{project_id}/reviews/run",
                json={
                    "assignments": [
                        {
                            "persona_id": "methodology_critic",
                            "model": "anthropic/claude-3.7-sonnet",
                        }
                    ],
                    "include_visual": True,
                    "vision_model": "vendor/not-enabled-vision",
                    "confirm_high_cost": True,
                },
                headers=headers,
            )
            mock_complete.assert_not_awaited()

        assert res.status_code == 403
        assert "vendor/not-enabled-vision" in res.json()["detail"]
        assert "not enabled" in res.json()["detail"]
