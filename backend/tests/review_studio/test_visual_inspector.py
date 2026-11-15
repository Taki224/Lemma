from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.model_registry import ModelRegistry
from app.ai.openrouter import CompletionResult, OpenRouterClient
from app.config import AppSettings
from app.config_secrets import SecretsStore
from app.db import DatabaseManager
from app.review_studio.text_review import ReviewIssue
from app.review_studio.visual_inspector import VisualPageInspector


@pytest.fixture
def mock_openrouter(tmp_path: Path) -> OpenRouterClient:
    db = MagicMock(spec=DatabaseManager)
    secrets = MagicMock(spec=SecretsStore)
    settings = AppSettings(data_dir=tmp_path / "data")
    registry = MagicMock(spec=ModelRegistry)
    registry.is_enabled.return_value = True
    return OpenRouterClient(
        db=db,
        secrets=secrets,
        settings=settings,
        registry=registry,
    )


def test_render_pdf_pages_to_png_produces_valid_png_bytes(tmp_path: Path) -> None:
    inspector = VisualPageInspector()
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.5 dummy")

    # Mock pymupdf rasterization to return valid PNG bytes
    valid_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 20
    mock_pix = MagicMock()
    mock_pix.tobytes.return_value = valid_png
    mock_page1 = MagicMock()
    mock_page1.get_pixmap.return_value = mock_pix
    mock_page2 = MagicMock()
    mock_page2.get_pixmap.return_value = mock_pix
    mock_doc = [mock_page1, mock_page2]

    with patch("app.review_studio.visual_inspector._open_pdf_doc", return_value=mock_doc):
        pages = inspector.render_pdf_pages_to_png(pdf_path, dpi=150)

    assert len(pages) == 2
    assert pages[0][0] == 1
    assert pages[0][1].startswith(b"\x89PNG\r\n\x1a\n")
    assert pages[1][0] == 2
    assert pages[1][1].startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_inspect_pages_returns_page_linked_layout_issues(
    tmp_path: Path,
    mock_openrouter: OpenRouterClient,
) -> None:
    inspector = VisualPageInspector()
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.5 dummy")

    valid_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 20

    # Mock render_pdf_pages_to_png to return 2 pages
    with patch.object(inspector, "render_pdf_pages_to_png", return_value=[(1, valid_png), (2, valid_png)]):
        vision_output = [
            {
                "severity": "major",
                "page_number": 1,
                "title": "Overflowing equation in Section 3",
                "critique": "Equation (4) overflows right margin into two-column gutter.",
                "suggested_fix": "Break equation using multline or split environment.",
            },
            {
                "severity": "nit",
                "page_number": 999,  # Out of range, should be clamped/validated to total pages (2)
                "title": "Clipped figure caption",
                "critique": "Figure 2 caption bottom line is cut off.",
                "suggested_fix": "Adjust figure placement or shrink caption font.",
            },
        ]

        mock_openrouter.complete = AsyncMock(  # type: ignore[method-assign]
            return_value=CompletionResult(
                content=json.dumps(vision_output),
                model="anthropic/claude-3.5-sonnet",
                prompt_tokens=3200,
                completion_tokens=200,
                cost_usd=0.04,
                reservation_id="res_vis_1",
            )
        )

        issues = await inspector.inspect_pages(
            pdf_path=pdf_path,
            vision_model="anthropic/claude-3.5-sonnet",
            openrouter=mock_openrouter,
        )

    assert len(issues) == 2
    assert all(isinstance(i, ReviewIssue) for i in issues)
    assert issues[0].reviewer_persona == "visual_layout_inspector"
    assert issues[0].page_number == 1
    assert issues[0].severity == "major"
    assert "Overflowing equation" in issues[0].title

    # Out of range page 999 should be clamped to valid total pages (2)
    assert issues[1].page_number == 2
