from __future__ import annotations

import json
import re
import secrets as pysecrets
from pathlib import Path
from typing import Any

from app.ai.openrouter import OpenRouterClient
from app.review_studio.text_review import ReviewIssue, Severity


def _open_pdf_doc(pdf_path: Path) -> Any:
    """Open PDF document using pymupdf fitz."""
    try:
        import fitz  # type: ignore[import-not-found]

        return fitz.open(pdf_path)
    except Exception:
        return None


class VisualPageInspector:
    """Multi-modal visual inspector rasterizing PDF pages and detecting layout/typesetting defects."""

    def render_pdf_pages_to_png(
        self,
        pdf_path: Path,
        dpi: int = 150,
        max_pages: int = 20,
    ) -> list[tuple[int, bytes]]:
        """Rasterize compiled PDF pages in CPU memory to PNG bytes."""
        if not pdf_path.is_file():
            return []

        doc = _open_pdf_doc(pdf_path)
        if doc is None:
            # Fallback 1x1 valid PNG stub if fitz is unavailable
            png_header = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
                b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            return [(1, png_header)]

        rendered: list[tuple[int, bytes]] = []
        try:
            page_count = len(doc)
            for idx in range(min(page_count, max_pages)):
                page = doc[idx]
                pix = page.get_pixmap(dpi=dpi)
                png_bytes = pix.tobytes("png")
                rendered.append((idx + 1, png_bytes))
        finally:
            if hasattr(doc, "close"):
                doc.close()

        return rendered

    async def inspect_pages(
        self,
        pdf_path: Path,
        vision_model: str,
        openrouter: OpenRouterClient,
        session_id: str | None = None,
    ) -> list[ReviewIssue]:
        """Send rasterized PDF page PNGs to vision model to detect typesetting and layout defects."""
        rendered_pages = self.render_pdf_pages_to_png(pdf_path)
        if not rendered_pages:
            return []

        total_pages = len(rendered_pages)
        png_images = [img for _, img in rendered_pages]

        prompt_messages = [
            {
                "role": "user",
                "content": (
                    f"You are inspecting a {total_pages}-page compiled academic LaTeX paper.\n"
                    "Inspect the attached rendered page images for any visual, typesetting, and layout defects:\n"
                    "1. Overflowing equations (Overfull \\hbox protruding into margins or two-column gutters)\n"
                    "2. Clipped figures or charts extending beyond margins\n"
                    "3. Cramped or misaligned tables with overlapping column text\n"
                    "4. Tiny or unreadable axis labels and chart legends\n"
                    "5. Widows or orphans (isolated single lines at the top or bottom of a page/column)\n\n"
                    "Output ONLY a valid JSON array of issue objects with this schema:\n"
                    "[\n"
                    "  {\n"
                    '    "severity": "major" | "minor" | "nit",\n'
                    '    "page_number": 1,\n'
                    '    "title": "Short title",\n'
                    '    "critique": "Detailed description of the layout defect",\n'
                    '    "suggested_fix": "Targeted LaTeX fix (e.g. multline, resizebox, adjust columnwidth)"\n'
                    "  }\n"
                    "]\n"
                    "No markdown backticks or surrounding text outside the JSON array."
                ),
            }
        ]

        completion = await openrouter.complete(
            model=vision_model,
            messages=prompt_messages,
            vision_images=png_images,
            max_tokens=4096,
            session_id=session_id,
        )

        content = completion.content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)

        try:
            parsed = json.loads(content)
        except Exception:
            return []

        if not isinstance(parsed, list):
            return []

        issues: list[ReviewIssue] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue

            raw_severity = str(item.get("severity") or "minor").lower()
            sev: Severity = (
                "major"
                if raw_severity == "major"
                else ("nit" if raw_severity == "nit" else "minor")
            )

            raw_page = item.get("page_number")
            page_num = 1
            if isinstance(raw_page, (int, float)):
                page_num = int(raw_page)

            # Validate and clamp page_number between 1 and total_pages
            if page_num < 1:
                page_num = 1
            elif page_num > total_pages:
                page_num = total_pages

            iss = ReviewIssue(
                issue_id=f"iss_{pysecrets.token_hex(6)}",
                reviewer_persona="visual_layout_inspector",
                model=vision_model,
                severity=sev,
                file_path="",
                line_number=None,
                page_number=page_num,
                quoted_anchor="",
                title=str(item.get("title") or "Visual Layout Defect").strip(),
                critique=str(item.get("critique") or "").strip(),
                suggested_fix=(
                    str(item.get("suggested_fix")).strip()
                    if item.get("suggested_fix")
                    else None
                ),
            )
            issues.append(iss)

        return issues
