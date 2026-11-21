from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import bootstrap_data_dir
from app.db import DatabaseManager
from app.integrity.audit_log import AUDIT_LOG_FILENAME, AuditLogStore
from app.integrity.classifier import classify_hunk, classify_turn
from app.integrity.latex_generator import DISCLOSURE_FILENAME, render_disclosure_latex
from app.integrity.prompts import build_integrity_overlay
from app.main import create_app
from app.projects.service import ProjectService


def _make_app(tmp_path: Path):
    settings = bootstrap_data_dir(tmp_path / "data")
    settings = settings.model_copy(update={"workspace_password": "password"})
    return create_app(settings=settings)


def _login(client: TestClient) -> str:
    resp = client.post("/api/auth/login", json={"password": "password"})
    assert resp.status_code == 200
    csrf = resp.json()["csrf_token"]
    return csrf


def test_integrity_overlay_clauses() -> None:
    writing_overlay = build_integrity_overlay("writing")
    assert "Anti-Sycophancy" in writing_overlay or "Critical & Objective Stance" in writing_overlay
    assert "% TODO: Verify citation" in writing_overlay
    assert "Extension, Not Replacement" in writing_overlay

    proof_overlay = build_integrity_overlay("proofreading")
    assert "Zero Hallucinated Citations" in proof_overlay
    assert "Extension, Not Replacement" not in proof_overlay

    assert build_integrity_overlay("compile_autofix") == ""


def test_classifier_tiers_for_hunks() -> None:
    # Tier 1: pure LaTeX formatting / macro change (prose unchanged)
    t1_latex = classify_hunk(
        profile_id="writing",
        original_text="We measure energy in joules.",
        replacement_text=r"We measure \textbf{energy} in \textit{joules}.",
    )
    assert t1_latex.tier == 1
    assert t1_latex.category == "technical_latex"

    # Tier 1: minor proofreading edit
    t1_proof = classify_hunk(
        profile_id="proofreading",
        original_text="The algorithm converge quickly on standard benchmarks.",
        replacement_text="The algorithm converges quickly on standard benchmarks.",
    )
    assert t1_proof.tier == 1
    assert t1_proof.category == "minor_language_polish"

    # Tier 2: substantive prose generation
    t2_draft = classify_hunk(
        profile_id="writing",
        original_text="Methodology placeholder.",
        replacement_text=(
            "We evaluate our proposed framework across three diverse datasets using "
            "five-fold cross-validation and report mean accuracy, F1-score, and calibration "
            "error alongside ablation studies for each architectural component."
        ),
        section_hint="Methodology",
    )
    assert t2_draft.tier == 2
    assert t2_draft.flags.substantive_prose is True
    assert "Methodology" in t2_draft.suggested_purpose

    # Tier 3: citations present
    t3_cite = classify_hunk(
        profile_id="writing",
        original_text="Prior work studied transformers.",
        replacement_text=r"Prior work studied transformers \cite{vaswani2017attention}.",
        cite_keys=["vaswani2017attention"],
    )
    assert t3_cite.tier == 3
    assert t3_cite.flags.has_citations is True

    # Tier 2: proportionality nudge on large generated passage
    big_prose = " ".join(["analysis"] * 280)
    t2_big = classify_hunk(
        profile_id="writing",
        original_text="",
        replacement_text=big_prose,
    )
    assert t2_big.tier == 2
    assert t2_big.flags.proportionality_nudge is True


def test_classifier_turn_chat_only() -> None:
    # Short advisory Q&A -> Tier 1
    t1 = classify_turn(
        profile_id="explain",
        user_prompt="What does \\usepackage{booktabs} do?",
        reply="It provides publication-quality horizontal rules for LaTeX tables.",
    )
    assert t1.tier == 1

    # Substantive chat summary of attached paper -> Tier 2
    long_summary = " ".join(["summary"] * 75)
    t2 = classify_turn(
        profile_id="explain",
        user_prompt="Please summarize the attached paper.",
        reply=long_summary,
        has_attachments_or_refs=True,
    )
    assert t2.tier == 2

    # Proportionality nudge when summarizing very short selection
    t2_short = classify_turn(
        profile_id="explain",
        user_prompt="Summarize this selection.",
        reply=long_summary,
        selection_text="This is a very short two-sentence note. It has few words.",
    )
    assert t2_short.flags.proportionality_nudge is True


def test_audit_log_store_and_hidden_from_file_tree(tmp_path: Path) -> None:
    settings = bootstrap_data_dir(tmp_path / "data")
    db = DatabaseManager(settings.db_path, settings.backups_dir)
    db.initialize()
    projects = ProjectService(settings.data_dir, db)
    meta = projects.create_project("Thesis")

    store = AuditLogStore(projects)
    e1 = store.append_entry(
        meta.project_id,
        source_surface="chat",
        provider="openrouter",
        model_id="anthropic/claude-3.7-sonnet",
        model_display_name="Claude 3.7 Sonnet",
        tier=2,
        purpose="Restructure Section 3.2",
        affected_files=["main.tex"],
        section_hint="Methodology",
        hunk_id="hunk_123",
    )
    # Deduplicates by hunk_id
    e1_dup = store.append_entry(
        meta.project_id,
        source_surface="chat",
        provider="openrouter",
        model_id="anthropic/claude-3.7-sonnet",
        hunk_id="hunk_123",
    )
    assert e1_dup.entry_id == e1.entry_id

    # Hidden dotfile on disk, not returned by list_files
    log_path = projects.get_project_dir(meta.project_id) / AUDIT_LOG_FILENAME
    assert log_path.is_file()
    raw = json.loads(log_path.read_text(encoding="utf-8"))
    assert len(raw["entries"]) == 1

    listed_paths = [f.path for f in projects.list_files(meta.project_id)]
    assert AUDIT_LOG_FILENAME not in listed_paths

    # Update & verify
    updated = store.update_entry(
        meta.project_id,
        e1.entry_id,
        human_verified=True,
        verification_note="Checked equations and citations",
    )
    assert updated.human_verified is True
    assert updated.verification_note == "Checked equations and citations"

    # Preserved on duplicate_project
    projects.update_project_settings(meta.project_id, academic_integrity_mode=True)
    dup_meta = projects.duplicate_project(meta.project_id, "Thesis Copy")
    assert dup_meta.academic_integrity_mode is True
    dup_log = store.read_log(dup_meta.project_id)
    assert len(dup_log.entries) == 1

    # Delete entry
    store.delete_entry(meta.project_id, e1.entry_id)
    assert len(store.read_log(meta.project_id).entries) == 0


def test_latex_generator_heading_and_escaping() -> None:
    store_entry = AuditLogStore  # just type reference
    _ = store_entry
    from app.integrity.audit_log import AuditLogEntry

    entry = AuditLogEntry(
        entry_id="ailog_1",
        timestamp="2026-11-20T12:00:00Z",
        source_surface="chat",
        provider="openrouter",
        model_id="anthropic/claude-3.7-sonnet",
        model_display_name="Claude 3.7 Sonnet",
        tier=2,
        category="substantive_drafting",
        purpose="Polishing & restructuring_v1 (50% draft)",
        affected_files=["chapters/intro_1.tex"],
        section_hint="Introduction",
        human_verified=True,
    )

    tex_article = render_disclosure_latex([entry], root_tex_content=r"\documentclass{article}")
    assert r"\section*{Declaration of Generative AI and AI-Assisted Technologies}" in tex_article
    assert r"Polishing \& restructuring\_v1 (50\% draft)" in tex_article
    assert r"\texttt{chapters/intro\_1.tex}" in tex_article
    assert "Verified by author" in tex_article

    tex_book = render_disclosure_latex([entry], root_tex_content=r"\documentclass[11pt]{report}")
    assert r"\chapter*{Declaration of Generative AI and AI-Assisted Technologies}" in tex_book


def test_integrity_router_end_to_end(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        csrf = _login(client)
        headers = {"X-CSRF-Token": csrf}

        # Create project & toggle academic_integrity_mode
        p_resp = client.post("/api/projects", json={"name": "My Thesis"}, headers=headers)
        assert p_resp.status_code == 201
        project_id = p_resp.json()["project_id"]
        assert p_resp.json()["academic_integrity_mode"] is False

        s_resp = client.patch(
            f"/api/projects/{project_id}/settings",
            json={"academic_integrity_mode": True},
            headers=headers,
        )
        assert s_resp.status_code == 200
        assert s_resp.json()["academic_integrity_mode"] is True

        # Add log entry
        c_resp = client.post(
            f"/api/projects/{project_id}/integrity/log",
            json={
                "source_surface": "chat",
                "provider": "openrouter",
                "model_id": "openai/gpt-4o",
                "model_display_name": "GPT-4o",
                "tier": 2,
                "purpose": "Drafting literature review outline",
                "affected_files": ["main.tex"],
                "section_hint": "Related Work",
            },
            headers=headers,
        )
        assert c_resp.status_code == 201
        entry_id = c_resp.json()["entry_id"]

        # Read log
        g_resp = client.get(f"/api/projects/{project_id}/integrity/log")
        assert g_resp.status_code == 200
        assert len(g_resp.json()["entries"]) == 1

        # Update entry
        u_resp = client.patch(
            f"/api/projects/{project_id}/integrity/log/{entry_id}",
            json={"human_verified": True, "verification_note": "Verified all claims"},
            headers=headers,
        )
        assert u_resp.status_code == 200
        assert u_resp.json()["human_verified"] is True

        # Read main.tex before writing appendix
        main_before = client.get(f"/api/projects/{project_id}/files/main.tex").json()["content"]

        # Write standalone ai_disclosure.tex without modifying main.tex
        w_resp = client.post(
            f"/api/projects/{project_id}/integrity/appendix/write",
            headers=headers,
        )
        assert w_resp.status_code == 200
        assert w_resp.json()["path"] == DISCLOSURE_FILENAME
        assert "GPT-4o" in w_resp.json()["latex"]

        main_after = client.get(f"/api/projects/{project_id}/files/main.tex").json()["content"]
        assert main_after == main_before
        assert "ai_disclosure" not in main_after

        # ai_disclosure.tex is visible in project files
        files = [f["path"] for f in client.get(f"/api/projects/{project_id}/files").json()["files"]]
        assert DISCLOSURE_FILENAME in files

        # Delete entry
        d_resp = client.delete(
            f"/api/projects/{project_id}/integrity/log/{entry_id}",
            headers=headers,
        )
        assert d_resp.status_code == 204
        assert client.get(f"/api/projects/{project_id}/integrity/log").json()["entries"] == []


def test_classifier_and_latex_edge_cases() -> None:
    from app.integrity.audit_log import AuditLogEntry

    # Two-optional-argument citation in reply triggers Tier 3 and overrides Tier 1 hunk category
    t1_hunk = classify_hunk(
        profile_id="fix_latex",
        original_text="We evaluate the model.",
        replacement_text=r"We evaluate the \textbf{model}.",
    )
    assert t1_hunk.tier == 1
    turn_with_two_arg_cite = classify_turn(
        profile_id="fix_latex",
        user_prompt="Fix formatting",
        reply=r"Formatted, see \citep[e.g.][p.~10]{smith2020} for details.",
        hunk_assessments=[t1_hunk],
    )
    assert turn_with_two_arg_cite.tier == 3
    assert turn_with_two_arg_cite.category == "citation_or_claim"
    assert turn_with_two_arg_cite.flags.has_citations is True

    # Tighten removing many words reports negative delta rather than +0
    t2_tight = classify_hunk(
        profile_id="tighten",
        original_text=" ".join(["word"] * 80),
        replacement_text="Concise summary of the method.",
    )
    assert t2_tight.tier == 2
    assert any("-75 prose words" in r for r in t2_tight.reasons)

    # Commented-out \documentclass{book} does not trigger \chapter*
    # Leading '[' and newlines in purpose/notes are safely escaped
    e = AuditLogEntry(
        entry_id="ailog_edge",
        timestamp="2026-11-20T12:00:00Z",
        source_surface="chat",
        provider="openrouter",
        model_id="openai/gpt-4o",
        model_display_name="[Custom] GPT-4o",
        tier=2,
        category="substantive_drafting",
        purpose="Line 1\n\nLine 2",
        affected_files=["main.tex"],
        human_verified=False,
        verification_note="Checked [1]",
    )
    tex = render_disclosure_latex(
        [e],
        root_tex_content="% \\documentclass{book}\n\\documentclass{article}\n",
    )
    assert r"\section*{Declaration of Generative AI" in tex
    assert r"\chapter*" not in tex
    assert "{[}Custom{]} GPT-4o" in tex
    assert "Line 1 Line 2" in tex
    assert "Pending author review: Checked {[}1{]}" in tex

    # Empty entries renders cleanly
    empty_tex = render_disclosure_latex([], root_tex_content=r"\documentclass{article}")
    assert "No substantive generative AI assistance" in empty_tex


def test_audit_log_resilient_to_malformed_ints_and_router_preview(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        csrf = _login(client)
        headers = {"X-CSRF-Token": csrf}

        p_resp = client.post("/api/projects", json={"name": "Edge Project"}, headers=headers)
        project_id = p_resp.json()["project_id"]

        # Corrupt .ai-audit-log.json with non-numeric tier & schema_version
        log_path = app.state.project_service.get_project_dir(project_id) / AUDIT_LOG_FILENAME
        log_path.write_text(
            json.dumps({
                "schema_version": "bad",
                "project_id": project_id,
                "entries": [{"entry_id": "e1", "tier": "not-an-int", "purpose": "Test"}],
            }),
            encoding="utf-8",
        )

        g_resp = client.get(f"/api/projects/{project_id}/integrity/log")
        assert g_resp.status_code == 200
        assert g_resp.json()["schema_version"] == 1
        assert g_resp.json()["entries"][0]["tier"] == 2

        # Preview endpoint works and 404s on missing project
        prev_resp = client.post(
            f"/api/projects/{project_id}/integrity/appendix/preview",
            headers=headers,
        )
        assert prev_resp.status_code == 200
        assert prev_resp.json()["filename"] == DISCLOSURE_FILENAME
        assert prev_resp.json()["entry_count"] == 1

        missing_resp = client.get("/api/projects/nonexistent-id/integrity/log")
        assert missing_resp.status_code == 404


def test_visual_to_latex_and_explain_math_integrity_metadata(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        csrf = _login(client)
        headers = {"X-CSRF-Token": csrf}

        p_resp = client.post("/api/projects", json={"name": "Tools Integrity"}, headers=headers)
        project_id = p_resp.json()["project_id"]
        client.patch(
            f"/api/projects/{project_id}/settings",
            json={"academic_integrity_mode": True},
            headers=headers,
        )

        v_resp = client.post(
            f"/api/projects/{project_id}/tools/visual-to-latex",
            json={"csv_text": "A,B\n1,2\n", "file_path": "main.tex"},
            headers=headers,
        )
        assert v_resp.status_code == 200
        hunk = v_resp.json()["hunk"]
        assert hunk["integrity"] is not None
        assert hunk["integrity"]["tier"] == 1

        # explain_math quick action returns integrity metadata (and exercises classify_turn)
        class _FakeResult:
            content = "This equation defines the inner product over the Hilbert space."
            finish_reason = "stop"

        class _FakeOpenRouter:
            async def complete(self, **kwargs):
                return _FakeResult()

        app.state.openrouter_client = _FakeOpenRouter()
        q_resp = client.post(
            f"/api/projects/{project_id}/tools/quick-action",
            json={
                "file_path": "main.tex",
                "start_line": 1,
                "end_line": 1,
                "selected_text": r"\langle x, y \rangle",
                "action": "explain_math",
                "model": "openai/gpt-4o",
            },
            headers=headers,
        )
        assert q_resp.status_code == 200
        assert q_resp.json()["integrity"] is not None
        assert q_resp.json()["integrity"]["tier"] == 1


@pytest.mark.asyncio
async def test_review_studio_injects_integrity_overlay_when_enabled(tmp_path: Path) -> None:
    from app.ai.prompts import PromptsManager
    from app.review_studio.text_review import ReviewerAssignment, TextReviewOrchestrator

    settings = bootstrap_data_dir(tmp_path / "data")
    project_dir = tmp_path / "proj"
    project_dir.mkdir(parents=True)
    (project_dir / "main.tex").write_text("\\documentclass{article}\nHello world.\n", encoding="utf-8")

    captured_prompts: list[str] = []

    class _FakeRes:
        content = "[]"
        finish_reason = "stop"

    class _FakeOpenRouter:
        def __init__(self, settings):
            self.settings = settings

        def estimate_cost_usd(self, model, prompt_tokens, completion_tokens):
            return 0.0

        def get_daily_spend_usd(self):
            return 0.0

        async def complete(self, *, model, messages, **kwargs):
            captured_prompts.append(messages[0]["content"])
            return _FakeRes()

    prompts_mgr = PromptsManager(settings.prompts_yaml_path)
    orchestrator = TextReviewOrchestrator(_FakeOpenRouter(settings), prompts_mgr, settings)
    assignments = [ReviewerAssignment(persona_id="literature_auditor", model="openai/gpt-4o")]

    await orchestrator.run_parallel_reviewers(
        project_dir=project_dir,
        root_tex="main.tex",
        assignments=assignments,
        academic_integrity_mode=False,
    )
    assert len(captured_prompts) == 1
    assert "Zero Hallucinated Citations" not in captured_prompts[0]

    await orchestrator.run_parallel_reviewers(
        project_dir=project_dir,
        root_tex="main.tex",
        assignments=assignments,
        academic_integrity_mode=True,
    )
    assert len(captured_prompts) == 2
    assert "Zero Hallucinated Citations" in captured_prompts[1]
    assert "Critical & Objective Stance" in captured_prompts[1]
