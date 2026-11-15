from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.model_registry import ModelRegistry
from app.ai.openrouter import CompletionResult, OpenRouterClient, SpendCapExceededError
from app.ai.prompts import PromptsManager
from app.config import AppSettings
from app.config_secrets import SecretsStore
from app.db import DatabaseManager
from app.review_studio.text_review import (
    ConfirmationRequiredError,
    CostEstimate,
    ReviewIssue,
    ReviewRunResult,
    ReviewerAssignment,
    TextReviewOrchestrator,
)


@pytest.fixture
def mock_openrouter(tmp_path: Path) -> OpenRouterClient:
    db = MagicMock(spec=DatabaseManager)
    secrets = MagicMock(spec=SecretsStore)
    settings = AppSettings(
        data_dir=tmp_path / "data",
        review_confirm_usd=0.50,
        request_spend_cap_usd=2.00,
        daily_spend_cap_usd=10.00,
    )
    registry = MagicMock(spec=ModelRegistry)
    registry.is_enabled.return_value = True
    client = OpenRouterClient(
        db=db,
        secrets=secrets,
        settings=settings,
        registry=registry,
    )
    client.estimate_cost_usd = MagicMock(return_value=0.30)  # type: ignore[method-assign]
    client.get_daily_spend_usd = MagicMock(return_value=0.0)  # type: ignore[method-assign]
    return client


@pytest.fixture
def prompts_mgr(tmp_path: Path) -> PromptsManager:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    return PromptsManager(prompts_path=config_dir / "prompts.yaml")


def test_cost_estimator_derives_pdf_page_count_and_enforces_confirm_high_cost(
    tmp_path: Path,
    mock_openrouter: OpenRouterClient,
    prompts_mgr: PromptsManager,
) -> None:
    project_dir = tmp_path / "project1"
    project_dir.mkdir(parents=True, exist_ok=True)
    main_tex = project_dir / "main.tex"
    main_tex.write_text(r"\documentclass{article}\begin{document}Hello world\end{document}", encoding="utf-8")

    build_dir = project_dir / ".build"
    build_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = build_dir / "main.pdf"
    pdf_path.write_bytes(b"%PDF-1.5 dummy content")

    settings = AppSettings(
        data_dir=tmp_path / "data",
        review_confirm_usd=0.50,
        request_spend_cap_usd=2.00,
    )

    orchestrator = TextReviewOrchestrator(
        openrouter=mock_openrouter,
        prompts_mgr=prompts_mgr,
        settings=settings,
    )

    assignments = [
        ReviewerAssignment(persona_id="methodology_critic", model="anthropic/claude-3.7-sonnet"),
        ReviewerAssignment(persona_id="literature_auditor", model="google/gemini-2.5-pro"),
    ]

    with patch("app.review_studio.text_review._read_pdf_page_count", return_value=12):
        # 2 models * $0.30 each = $0.60, which is >= settings.review_confirm_usd ($0.50)
        estimate = orchestrator.estimate_review_run(
            project_dir=project_dir,
            root_tex="main.tex",
            assignments=assignments,
            include_visual=False,
            settings=settings,
        )

    assert isinstance(estimate, CostEstimate)
    assert estimate.page_count == 12
    assert estimate.estimated_usd == pytest.approx(0.60)
    assert estimate.requires_confirmation is True

    # Calling run_parallel_reviewers without confirm_high_cost must raise ConfirmationRequiredError
    with pytest.raises(ConfirmationRequiredError) as exc_info:
        import asyncio

        asyncio.run(
            orchestrator.run_parallel_reviewers(
                project_dir=project_dir,
                root_tex="main.tex",
                assignments=assignments,
                confirm_high_cost=False,
                settings=settings,
            )
        )
    assert "confirmation" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_parallel_reviewers_handles_partial_failure_and_validates_file_paths_and_anchors(
    tmp_path: Path,
    mock_openrouter: OpenRouterClient,
    prompts_mgr: PromptsManager,
) -> None:
    project_dir = tmp_path / "project2"
    project_dir.mkdir(parents=True, exist_ok=True)
    main_tex = project_dir / "main.tex"
    main_tex.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "This is an empirical study of neural quantum compilers.\n"
        "We achieved 99% accuracy on all synthetic benchmarks.\n"
        "\\end{document}\n",
        encoding="utf-8",
    )

    settings = AppSettings(
        data_dir=tmp_path / "data",
        review_confirm_usd=1.00,  # high threshold so confirm not required
    )

    orchestrator = TextReviewOrchestrator(
        openrouter=mock_openrouter,
        prompts_mgr=prompts_mgr,
        settings=settings,
    )

    assignments = [
        ReviewerAssignment(persona_id="methodology_critic", model="model-success"),
        ReviewerAssignment(persona_id="literature_auditor", model="model-fail"),
        ReviewerAssignment(persona_id="structure_editor", model="model-malicious-path"),
    ]

    async def fake_complete(model: str, messages: list[dict], **kwargs: object) -> CompletionResult:
        if model == "model-fail":
            raise RuntimeError("API timeout or rate limit error")

        if model == "model-success":
            payload = [
                {
                    "severity": "major",
                    "file_path": "main.tex",
                    "line_number": 4,
                    "page_number": 1,
                    "quoted_anchor": "We achieved 99% accuracy on all synthetic benchmarks.",
                    "title": "Unsubstantiated synthetic benchmark accuracy claim",
                    "critique": "The claim lacks statistical confidence intervals or significance tests.",
                    "suggested_fix": "Report mean and std across 5 random seeds.",
                }
            ]
            return CompletionResult(
                content=json.dumps(payload),
                model=model,
                prompt_tokens=100,
                completion_tokens=50,
                cost_usd=0.01,
                reservation_id="res_1",
            )

        if model == "model-malicious-path":
            # Model returns malicious path traversal and out-of-bounds line number
            payload = [
                {
                    "severity": "nit",
                    "file_path": "../../etc/passwd",
                    "line_number": 9999,
                    "page_number": 1,
                    "quoted_anchor": "neural quantum compilers",
                    "title": "Terminology clarification",
                    "critique": "Clarify quantum compiler definition.",
                    "suggested_fix": None,
                }
            ]
            return CompletionResult(
                content=json.dumps(payload),
                model=model,
                prompt_tokens=100,
                completion_tokens=50,
                cost_usd=0.01,
                reservation_id="res_2",
            )

        raise ValueError(f"Unexpected model {model}")

    mock_openrouter.complete = AsyncMock(side_effect=fake_complete)  # type: ignore[method-assign]

    result = await orchestrator.run_parallel_reviewers(
        project_dir=project_dir,
        root_tex="main.tex",
        assignments=assignments,
        confirm_high_cost=False,
        settings=settings,
    )

    assert isinstance(result, ReviewRunResult)
    assert len(result.failed_reviewers) == 1
    assert "literature_auditor" in result.failed_reviewers[0]

    assert len(result.issues) == 2

    # Issue from model-success
    iss1 = next(i for i in result.issues if i.reviewer_persona == "methodology_critic")
    assert iss1.severity == "major"
    assert iss1.file_path == "main.tex"
    assert iss1.line_number == 4
    assert iss1.quoted_anchor == "We achieved 99% accuracy on all synthetic benchmarks."

    # Issue from model-malicious-path must have sanitized path (not ../../etc/passwd)
    iss2 = next(i for i in result.issues if i.reviewer_persona == "structure_editor")
    assert iss2.severity == "nit"
    assert not iss2.file_path.startswith("..")
    # File path sanitized to relative valid file in project
    assert iss2.file_path == "main.tex"
    # Line number re-resolved / clamped to the line containing the quoted anchor (line 3)
    assert iss2.line_number == 3


def test_number_lines_prefixes_each_line_from_one() -> None:
    from app.review_studio.text_review import _number_lines

    assert _number_lines("first\nsecond\n\nfourth") == "1| first\n2| second\n3| \n4| fourth"


@pytest.mark.asyncio
async def test_reviewer_request_contains_numbered_manuscript_and_prefix_rule(
    tmp_path: Path,
    mock_openrouter: OpenRouterClient,
    prompts_mgr: PromptsManager,
) -> None:
    project_dir = tmp_path / "project_numbered"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nHello.\n\\end{document}\n",
        encoding="utf-8",
    )
    settings = AppSettings(data_dir=tmp_path / "data", review_confirm_usd=1.00)
    orchestrator = TextReviewOrchestrator(
        openrouter=mock_openrouter, prompts_mgr=prompts_mgr, settings=settings
    )
    captured: list[list[dict]] = []

    async def fake_complete(model: str, messages: list[dict], **kwargs: object) -> CompletionResult:
        captured.append(messages)
        return CompletionResult(
            content="[]", model=model, prompt_tokens=1, completion_tokens=1,
            cost_usd=0.0, reservation_id="r",
        )

    mock_openrouter.complete = AsyncMock(side_effect=fake_complete)  # type: ignore[method-assign]

    await orchestrator.run_parallel_reviewers(
        project_dir=project_dir,
        root_tex="main.tex",
        assignments=[ReviewerAssignment(persona_id="methodology_critic", model="m")],
        settings=settings,
    )

    system = next(m["content"] for m in captured[0] if m["role"] == "system")
    user = next(m["content"] for m in captured[0] if m["role"] == "user")
    assert "1| \\documentclass{article}" in user
    assert "3| Hello." in user
    assert "3. Each manuscript line is prefixed with its line number as 'N| '." in system
    assert "Never include the 'N| ' prefix in quoted_anchor." in system
    assert "4. Return ONLY a valid JSON array" in system


def test_strip_line_prefixes_single_and_multiline() -> None:
    from app.review_studio.text_review import _strip_line_prefixes

    assert _strip_line_prefixes("12| We prove...") == "We prove..."
    assert _strip_line_prefixes("12| first\n13| second") == "first\nsecond"
    assert _strip_line_prefixes("no prefix here") == "no prefix here"


async def _run_single_issue_review(
    tmp_path: Path,
    mock_openrouter: OpenRouterClient,
    prompts_mgr: PromptsManager,
    project_dir: Path,
    item: dict,
) -> ReviewRunResult:
    settings = AppSettings(data_dir=tmp_path / "data", review_confirm_usd=1.00)
    orchestrator = TextReviewOrchestrator(
        openrouter=mock_openrouter, prompts_mgr=prompts_mgr, settings=settings
    )

    async def fake_complete(model: str, messages: list[dict], **kwargs: object) -> CompletionResult:
        return CompletionResult(
            content=json.dumps([item]), model=model, prompt_tokens=1,
            completion_tokens=1, cost_usd=0.0, reservation_id="r",
        )

    mock_openrouter.complete = AsyncMock(side_effect=fake_complete)  # type: ignore[method-assign]
    return await orchestrator.run_parallel_reviewers(
        project_dir=project_dir,
        root_tex="main.tex",
        assignments=[ReviewerAssignment(persona_id="methodology_critic", model="m")],
        settings=settings,
    )


@pytest.mark.asyncio
async def test_reviewer_issue_with_prefixed_anchor_gets_correct_line(
    tmp_path: Path,
    mock_openrouter: OpenRouterClient,
    prompts_mgr: PromptsManager,
) -> None:
    project_dir = tmp_path / "project_prefixed"
    project_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"Filler line {i}." for i in range(1, 20)]
    lines[11] = "We prove that the algorithm converges."
    (project_dir / "main.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = await _run_single_issue_review(
        tmp_path, mock_openrouter, prompts_mgr, project_dir,
        {
            "severity": "major",
            "file_path": "main.tex",
            "line_number": 12,
            "quoted_anchor": "12| We prove that the algorithm converges.",
            "title": "t",
            "critique": "c",
        },
    )

    assert len(result.issues) == 1
    assert result.issues[0].line_number == 12
    assert result.issues[0].quoted_anchor == "We prove that the algorithm converges."


@pytest.mark.asyncio
async def test_reviewer_issue_with_correct_line_number_kept(
    tmp_path: Path,
    mock_openrouter: OpenRouterClient,
    prompts_mgr: PromptsManager,
) -> None:
    fixture = Path(__file__).resolve().parent.parent / "fixtures" / "reference_manuscript" / "main.tex"
    project_dir = tmp_path / "reference_manuscript"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "main.tex").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    anchor = "This manuscript presents a deterministic verification architecture"

    result = await _run_single_issue_review(
        tmp_path, mock_openrouter, prompts_mgr, project_dir,
        {
            "severity": "minor",
            "file_path": "main.tex",
            "line_number": 13,
            "quoted_anchor": anchor,
            "title": "t",
            "critique": "c",
        },
    )

    assert len(result.issues) == 1
    assert result.issues[0].line_number == 13
    assert result.issues[0].quoted_anchor == anchor


@pytest.mark.asyncio
async def test_parallel_reviewers_share_single_openrouter_session_id(
    tmp_path: Path,
    mock_openrouter: OpenRouterClient,
    prompts_mgr: PromptsManager,
) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "main.tex").write_text("Line one.\n", encoding="utf-8")

    settings = AppSettings(data_dir=tmp_path / "data")
    orchestrator = TextReviewOrchestrator(
        openrouter=mock_openrouter, prompts_mgr=prompts_mgr, settings=settings
    )
    seen_session_ids: list[str | None] = []

    async def fake_complete(**kwargs):
        seen_session_ids.append(kwargs.get("session_id"))
        return CompletionResult(
            content="[]",
            model=kwargs["model"],
            prompt_tokens=10,
            completion_tokens=2,
            cost_usd=0.001,
            reservation_id="r",
        )

    mock_openrouter.complete = AsyncMock(side_effect=fake_complete)  # type: ignore[method-assign]
    assignments = [
        ReviewerAssignment(persona_id="reviewer_method", model="openai/gpt-4o"),
        ReviewerAssignment(persona_id="reviewer_clarity", model="anthropic/claude-3.7-sonnet"),
    ]

    await orchestrator.run_parallel_reviewers(
        project_dir=project_dir,
        root_tex="main.tex",
        assignments=assignments,
        confirm_high_cost=True,
        session_id="review:proj:abc123",
    )
    assert seen_session_ids == ["review:proj:abc123", "review:proj:abc123"]

    seen_session_ids.clear()
    await orchestrator.run_parallel_reviewers(
        project_dir=project_dir,
        root_tex="main.tex",
        assignments=assignments,
        confirm_high_cost=True,
    )
    assert len(seen_session_ids) == 2
    assert seen_session_ids[0] is not None
    assert seen_session_ids[0].startswith("review:")
    assert seen_session_ids[0] == seen_session_ids[1]
