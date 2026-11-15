from pathlib import Path

from app.ai.project_index import (
    CHUNK_TARGET_LINES,
    FileIndex,
    IndexedChunk,
    IndexedEntity,
    IndexSearchHit,
    ProjectIndex,
    ProjectIndexStore,
    _normalize_rel_path,
    format_project_index,
    index_bib_text,
    index_tex_text,
    search_project_index,
)
from app.db import DatabaseManager
from app.projects.service import ProjectService


def test_index_tex_extracts_headings_labels_envs_macros_and_cites() -> None:
    tex = "\n".join(
        [
            "\\documentclass{article}",
            "\\newcommand{\\vecnorm}[1]{\\|#1\\|}",
            "\\DeclareMathOperator*{\\argmin}{arg\\,min}",
            "\\begin{document}",
            "\\part{Foundations}",
            "\\section{Introduction}",
            "\\label{sec:intro}",
            "As shown in \\cite{smith2024} and \\citep[p.~3]{doe2023, lee2022}.",
            "\\subsection{Main Result}",
            "\\begin{theorem}[Spectral Bound]",
            "\\label{thm:spectral}",
            "Every bounded operator satisfies \\vecnorm{x} \\ge 0.",
            "\\begin{equation}",
            "\\label{eq:norm}",
            "\\argmin_x f(x) = 0",
            "\\end{equation}",
            "\\end{theorem}",
            "\\begin{figure}",
            "\\caption{Overview}",
            "\\end{figure}",
            "\\end{document}",
        ]
    )

    idx = index_tex_text("chapters/intro.tex", tex, content_hash="abc123", mtime_ns=42)

    assert isinstance(idx, FileIndex)
    assert idx.rel_path == "chapters/intro.tex"
    assert idx.content_hash == "abc123"
    assert idx.mtime_ns == 42
    assert idx.line_count == 21

    headings = [e for e in idx.entities if e.kind == "heading"]
    assert [(h.name, h.start_line, h.end_line) for h in headings] == [
        ("Foundations", 5, 5),
        ("Introduction", 6, 6),
        ("Main Result", 9, 9),
    ]

    labels = [e for e in idx.entities if e.kind == "label"]
    assert [(l.name, l.start_line, l.end_line) for l in labels] == [
        ("sec:intro", 7, 7),
        ("thm:spectral", 11, 11),
        ("eq:norm", 14, 14),
    ]

    envs = [e for e in idx.entities if e.kind == "env"]
    assert [(e.name, e.start_line, e.end_line) for e in envs] == [
        ("equation", 13, 16),
        ("theorem", 10, 17),
        ("figure", 18, 20),
    ]

    macros = [e for e in idx.entities if e.kind == "macro"]
    assert [(m.name, m.start_line, m.end_line) for m in macros] == [
        ("\\vecnorm", 2, 2),
        ("\\argmin", 3, 3),
    ]

    cites = [e for e in idx.entities if e.kind == "cite"]
    assert [(c.name, c.start_line, c.end_line) for c in cites] == [
        ("smith2024", 8, 8),
        ("doe2023", 8, 8),
        ("lee2022", 8, 8),
    ]
    assert all(e.file_path == "chapters/intro.tex" for e in idx.entities)

    # Sanity-check ProjectIndex dataclass construction
    proj = ProjectIndex(project_id="proj-1", files={idx.rel_path: idx})
    assert proj.project_id == "proj-1"
    assert proj.files["chapters/intro.tex"] is idx


def test_index_tex_ignores_comments_and_verbatim_blocks() -> None:
    tex = "\n".join(
        [
            "% \\section{Commented Heading}",
            "% \\label{sec:commented}",
            "\\section{Real Heading} % \\label{sec:inline_comment}",
            "Escaped percent 100\\% with \\label{sec:real}.",
            "\\begin{verbatim}",
            "\\section{Verbatim Heading}",
            "\\label{sec:verbatim}",
            "\\cite{fake_cite}",
            "\\end{verbatim}",
            "\\begin{minted}{python}",
            "\\newcommand{\\notamacro}{1}",
            "\\begin{theorem}",
            "\\end{theorem}",
            "\\end{minted}",
            "\\begin{lstlisting}",
            "\\label{sec:lst}",
            "\\end{lstlisting}",
            "\\cite{real_cite}",
        ]
    )

    idx = index_tex_text("main.tex", tex)

    names_by_kind = {(e.kind, e.name) for e in idx.entities}
    assert ("heading", "Real Heading") in names_by_kind
    assert ("label", "sec:real") in names_by_kind
    assert ("cite", "real_cite") in names_by_kind

    assert ("heading", "Commented Heading") not in names_by_kind
    assert ("label", "sec:commented") not in names_by_kind
    assert ("label", "sec:inline_comment") not in names_by_kind
    assert ("heading", "Verbatim Heading") not in names_by_kind
    assert ("label", "sec:verbatim") not in names_by_kind
    assert ("cite", "fake_cite") not in names_by_kind
    assert ("macro", "\\notamacro") not in names_by_kind
    assert ("env", "theorem") not in names_by_kind
    assert ("label", "sec:lst") not in names_by_kind


def test_index_tex_chunks_cover_file_with_exact_line_spans() -> None:
    lines = [
        "\\documentclass{article}",
        "\\begin{document}",
        "\\section{First Section}",
    ]
    lines.extend([f"First body line {i}" for i in range(1, 91)])
    lines.append("\\subsection{Nested Sub}")
    lines.extend([f"Sub body line {i}" for i in range(1, 21)])
    tex = "\n".join(lines)

    idx = index_tex_text("long.tex", tex)

    assert len(idx.chunks) >= 3
    assert all(isinstance(c, IndexedChunk) for c in idx.chunks)
    assert idx.chunks[0].start_line == 1
    assert idx.chunks[-1].end_line == len(lines)

    for i, chunk in enumerate(idx.chunks):
        assert 1 <= chunk.start_line <= chunk.end_line <= len(lines)
        assert chunk.end_line - chunk.start_line + 1 <= CHUNK_TARGET_LINES
        assert chunk.file_path == "long.tex"
        expected_text = "\n".join(lines[chunk.start_line - 1 : chunk.end_line])
        assert chunk.text == expected_text
        if i > 0:
            assert chunk.start_line == idx.chunks[i - 1].end_line + 1

    # Chunks before \section{First Section} have empty heading; inside First Section
    # they track "First Section"; inside \subsection{Nested Sub} they track "Nested Sub".
    assert idx.chunks[0].heading == ""
    first_sec_chunks = [c for c in idx.chunks if c.heading == "First Section"]
    nested_chunks = [c for c in idx.chunks if c.heading == "Nested Sub"]
    assert len(first_sec_chunks) >= 2
    assert len(nested_chunks) == 1
    assert "\\subsection{Nested Sub}" in nested_chunks[0].text


def test_index_bib_extracts_entries_and_line_spans() -> None:
    bib = "\n".join(
        [
            "@preamble{{\\newcommand{\\noopsort}[1]{}}}",
            "",
            "@article{smith2024,",
            "  author = {Alice Smith and Bob Jones},",
            "  title = {A {Spectral} Method for Partial Differential Equations},",
            "  journal = {Journal of Numerical Analysis},",
            "  year = {2024}",
            "}",
            "",
            "@inproceedings{doe2023ml,",
            "  author = {Jane Doe},",
            "  title = {Fast Attention Mechanisms},",
            "  booktitle = {NeurIPS},",
            "  year = {2023}",
            "}",
        ]
    )

    idx = index_bib_text("refs.bib", bib, content_hash="bibhash", mtime_ns=99)

    assert idx.rel_path == "refs.bib"
    assert idx.content_hash == "bibhash"
    assert idx.mtime_ns == 99
    assert idx.line_count == 15

    assert [(e.kind, e.name, e.start_line, e.end_line) for e in idx.entities] == [
        ("cite", "smith2024", 3, 8),
        ("cite", "doe2023ml", 10, 15),
    ]
    assert len(idx.chunks) == 2
    assert idx.chunks[0].start_line == 3
    assert idx.chunks[0].end_line == 8
    assert idx.chunks[0].heading == "smith2024"
    assert "Alice Smith" in idx.chunks[0].text
    assert "Spectral" in idx.chunks[0].text
    assert idx.chunks[1].start_line == 10
    assert idx.chunks[1].end_line == 15
    assert idx.chunks[1].heading == "doe2023ml"


def _make_store(tmp_path: Path) -> tuple[DatabaseManager, ProjectService, ProjectIndexStore, str]:
    db = DatabaseManager(db_path=tmp_path / "app.db", backup_dir=tmp_path / "backups")
    db.initialize()
    project_service = ProjectService(data_dir=tmp_path / "data", db=db)
    project = project_service.create_project("Test Paper")
    store = ProjectIndexStore(db=db, project_service=project_service)
    return db, project_service, store, project.project_id


def test_store_upsert_load_delete_and_move(tmp_path: Path) -> None:
    db, project_service, store, project_id = _make_store(tmp_path)

    intro_tex = "\\section{Introduction}\n\\label{sec:intro}\nHello \\cite{smith2024}.\n"
    bib_text = "@article{smith2024,\n  author = {Smith},\n  title = {Paper},\n  year = {2024}\n}\n"

    project_service.write_file(project_id, "chapters/intro.tex", intro_tex)
    project_service.write_file(project_id, "chapters/methods.tex", "\\section{Methods}\n\\label{sec:methods}\n")
    project_service.write_file(project_id, "references.bib", bib_text)
    project_service.write_file(project_id, "notes.txt", "plain text ignored")

    # Non-.tex/.bib files are skipped
    assert store.upsert_file(project_id, "notes.txt") is None

    upserted_intro = store.upsert_file(project_id, "chapters/intro.tex")
    assert upserted_intro is not None
    assert upserted_intro.rel_path == "chapters/intro.tex"
    assert upserted_intro.line_count == 3
    assert len(upserted_intro.content_hash) == 64

    upserted_bib = store.upsert_file(project_id, "references.bib")
    assert upserted_bib is not None
    assert [e.name for e in upserted_bib.entities] == ["smith2024"]

    # Round-trip load from SQLite
    loaded = store.load_or_rebuild(project_id)
    assert "chapters/intro.tex" in loaded.files
    assert "chapters/methods.tex" in loaded.files
    assert "references.bib" in loaded.files
    assert "notes.txt" not in loaded.files
    assert loaded.files["chapters/intro.tex"].content_hash == upserted_intro.content_hash
    assert [e.name for e in loaded.files["chapters/intro.tex"].entities] == [
        "Introduction",
        "sec:intro",
        "smith2024",
    ]

    # Move single file on disk and in store
    project_service.move_path(project_id, "references.bib", "bib/refs.bib")
    store.move_path(project_id, "references.bib", "bib/refs.bib")

    # Move folder prefix on disk and in store
    project_service.move_path(project_id, "chapters", "sections")
    store.move_path(project_id, "chapters", "sections")

    reloaded = store.load_or_rebuild(project_id)
    assert "references.bib" not in reloaded.files
    assert "bib/refs.bib" in reloaded.files
    assert "chapters/intro.tex" not in reloaded.files
    assert "sections/intro.tex" in reloaded.files
    assert "sections/methods.tex" in reloaded.files
    assert all(e.file_path == "sections/intro.tex" for e in reloaded.files["sections/intro.tex"].entities)
    assert all(c.file_path == "sections/intro.tex" for c in reloaded.files["sections/intro.tex"].chunks)
    assert all(e.file_path == "bib/refs.bib" for e in reloaded.files["bib/refs.bib"].entities)

    # Delete single file and folder prefix in SQLite (and on disk so load_or_rebuild agrees)
    project_service.delete_file(project_id, "bib/refs.bib")
    store.delete_file(project_id, "bib/refs.bib")
    project_service.delete_folder(project_id, "sections")
    store.delete_file(project_id, "sections")

    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT rel_path FROM project_index_files WHERE project_id = ?;",
            (project_id,),
        ).fetchall()
    remaining_paths = {r["rel_path"] for r in rows}
    assert "bib/refs.bib" not in remaining_paths
    assert "sections/intro.tex" not in remaining_paths
    assert "sections/methods.tex" not in remaining_paths


def test_load_or_rebuild_recovers_missing_or_stale_files(tmp_path: Path) -> None:
    db, project_service, store, project_id = _make_store(tmp_path)

    initial = store.load_or_rebuild(project_id)
    assert set(initial.files.keys()) == {"main.tex", "references.bib"}
    old_main_hash = initial.files["main.tex"].content_hash

    # Mutate files directly on disk, bypassing ProjectService/store
    project_dir = project_service.get_project_dir(project_id)
    (project_dir / "main.tex").write_text(
        "\\section{Updated}\n\\label{sec:updated}\nBody\n",
        encoding="utf-8",
    )
    (project_dir / "extra").mkdir(parents=True, exist_ok=True)
    (project_dir / "extra" / "appendix.tex").write_text(
        "\\section{Appendix}\n\\label{sec:app}\n",
        encoding="utf-8",
    )
    (project_dir / "references.bib").unlink()

    rebuilt = store.load_or_rebuild(project_id)
    assert set(rebuilt.files.keys()) == {"main.tex", "extra/appendix.tex"}
    assert rebuilt.files["main.tex"].content_hash != old_main_hash
    assert [e.name for e in rebuilt.files["main.tex"].entities] == ["Updated", "sec:updated"]
    assert [e.name for e in rebuilt.files["extra/appendix.tex"].entities] == ["Appendix", "sec:app"]

    # Verify SQLite was updated to match
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT rel_path, content_hash FROM project_index_files WHERE project_id = ?;",
            (project_id,),
        ).fetchall()
    db_hashes = {r["rel_path"]: r["content_hash"] for r in rows}
    assert set(db_hashes.keys()) == {"main.tex", "extra/appendix.tex"}
    assert db_hashes["main.tex"] == rebuilt.files["main.tex"].content_hash


def test_overlay_buffers_updates_line_ranges_without_persisting(tmp_path: Path) -> None:
    _db, project_service, store, project_id = _make_store(tmp_path)

    saved_tex = "\\section{Intro}\n\\label{sec:intro}\nSaved line.\n"
    project_service.write_file(project_id, "main.tex", saved_tex)
    saved_index = store.load_or_rebuild(project_id)

    saved_label = next(e for e in saved_index.files["main.tex"].entities if e.kind == "label")
    assert saved_label.start_line == 2

    unsaved_tex = "% added line 1\n% added line 2\n\\section{Intro}\n\\label{sec:intro}\nUnsaved line.\n"
    overlaid = store.overlay_buffers(
        saved_index,
        {
            "main.tex": unsaved_tex,
            "ignored.txt": "not indexed",
        },
    )

    assert overlaid is not saved_index
    assert overlaid.project_id == saved_index.project_id
    assert "ignored.txt" not in overlaid.files

    overlaid_label = next(e for e in overlaid.files["main.tex"].entities if e.kind == "label")
    assert overlaid_label.start_line == 4
    assert overlaid.files["main.tex"].line_count == 5

    # Original ProjectIndex and SQLite remain unchanged
    orig_label = next(e for e in saved_index.files["main.tex"].entities if e.kind == "label")
    assert orig_label.start_line == 2

    reloaded = store.load_or_rebuild(project_id)
    reloaded_label = next(e for e in reloaded.files["main.tex"].entities if e.kind == "label")
    assert reloaded_label.start_line == 2
    assert reloaded.files["main.tex"].content_hash == saved_index.files["main.tex"].content_hash


def test_index_bib_ignores_percent_commented_entries_and_commented_braces() -> None:
    bib = "\n".join(
        [
            "% @article{commented_out,",
            "%   author = {Nobody},",
            "%   title = {Unclosed {brace in comment},",
            "% }",
            "@article{real2024,",
            "  author = {Real Author}, % {unclosed brace in inline comment",
            "  title = {100\\% Valid Title},",
            "  year = {2024}",
            "}",
            "% @inproceedings{trailing_commented,",
            "@book{next2025,",
            "  author = {Next Author},",
            "  title = {Next Book},",
            "  year = {2025}",
            "}",
        ]
    )

    idx = index_bib_text("refs.bib", bib)

    assert [(e.name, e.start_line, e.end_line) for e in idx.entities] == [
        ("real2024", 5, 9),
        ("next2025", 11, 15),
    ]
    assert [c.heading for c in idx.chunks] == ["real2024", "next2025"]


def test_index_tex_emits_unclosed_env_at_eof() -> None:
    tex = "\n".join(
        [
            "\\section{Draft}",
            "\\begin{theorem}[WIP]",
            "\\label{thm:wip}",
            "Still typing this theorem...",
        ]
    )

    idx = index_tex_text("draft.tex", tex)

    envs = [e for e in idx.entities if e.kind == "env"]
    assert len(envs) == 1
    assert envs[0] == IndexedEntity(
        kind="env",
        name="theorem",
        file_path="draft.tex",
        start_line=2,
        end_line=4,
        detail="WIP",
    )


def test_normalize_rel_path_handles_dot_slash_backslashes_and_duplicates() -> None:
    assert _normalize_rel_path("./main.tex") == "main.tex"
    assert _normalize_rel_path("chapters\\intro.tex") == "chapters/intro.tex"
    assert _normalize_rel_path("/chapters//intro.tex/") == "chapters/intro.tex"
    assert _normalize_rel_path(".") == ""
    assert _normalize_rel_path("./") == ""


def test_load_or_rebuild_recovers_from_corrupt_index_json(tmp_path: Path) -> None:
    db, project_service, store, project_id = _make_store(tmp_path)

    project_service.write_file(project_id, "main.tex", "\\section{Recovered}\n\\label{sec:rec}\n")
    store.upsert_file(project_id, "main.tex")

    with db.get_connection() as conn:
        conn.execute(
            "UPDATE project_index_files SET index_json = ? WHERE project_id = ? AND rel_path = ?;",
            ("{not-valid-json", project_id, "main.tex"),
        )

    rebuilt = store.load_or_rebuild(project_id)
    assert "main.tex" in rebuilt.files
    assert [e.name for e in rebuilt.files["main.tex"].entities] == ["Recovered", "sec:rec"]


def test_move_path_to_non_indexable_extension_deletes_row(tmp_path: Path) -> None:
    db, project_service, store, project_id = _make_store(tmp_path)

    project_service.write_file(project_id, "notes.tex", "\\section{Notes}\n")
    store.upsert_file(project_id, "notes.tex")

    project_service.move_path(project_id, "notes.tex", "notes.txt")
    store.move_path(project_id, "notes.tex", "notes.txt")

    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT rel_path FROM project_index_files WHERE project_id = ?;",
            (project_id,),
        ).fetchall()
    paths = {r["rel_path"] for r in rows}
    assert "notes.tex" not in paths
    assert "notes.txt" not in paths


def test_overlay_buffers_normalizes_paths_and_adds_unsaved_files(tmp_path: Path) -> None:
    _db, project_service, store, project_id = _make_store(tmp_path)

    project_service.write_file(project_id, "main.tex", "\\section{Saved}\n")
    saved_index = store.load_or_rebuild(project_id)

    overlaid = store.overlay_buffers(
        saved_index,
        {
            "./main.tex": "\\section{Edited}\n",
            "chapters\\new_unsaved.tex": "\\section{Unsaved Chapter}\n\\label{sec:unsaved}\n",
        },
    )

    assert [e.name for e in overlaid.files["main.tex"].entities] == ["Edited"]
    assert "chapters/new_unsaved.tex" in overlaid.files
    assert [e.name for e in overlaid.files["chapters/new_unsaved.tex"].entities] == [
        "Unsaved Chapter",
        "sec:unsaved",
    ]
    assert "chapters/new_unsaved.tex" not in saved_index.files


def test_upsert_file_with_explicit_text(tmp_path: Path) -> None:
    db, _project_service, store, project_id = _make_store(tmp_path)

    upserted = store.upsert_file(
        project_id,
        "unsaved/virtual.tex",
        text="\\section{Virtual}\n\\label{sec:virtual}\n",
    )
    assert upserted is not None
    assert upserted.rel_path == "unsaved/virtual.tex"
    assert upserted.mtime_ns == 0
    assert [e.name for e in upserted.entities] == ["Virtual", "sec:virtual"]

    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT rel_path, content_hash FROM project_index_files WHERE project_id = ? AND rel_path = ?;",
            (project_id, "unsaved/virtual.tex"),
        ).fetchone()
    assert row is not None
    assert row["content_hash"] == upserted.content_hash


def test_load_or_rebuild_skips_file_deleted_after_list_files(
    tmp_path: Path, monkeypatch
) -> None:
    db, project_service, store, project_id = _make_store(tmp_path)
    project_service.write_file(project_id, "vanishing.tex", "\\section{Vanishing}\n")
    store.upsert_file(project_id, "vanishing.tex")

    original_list_files = project_service.list_files

    def list_files_then_delete(pid: str):
        nodes = original_list_files(pid)
        project_dir = project_service.get_project_dir(pid)
        vanishing = project_dir / "vanishing.tex"
        if vanishing.exists():
            vanishing.unlink()
        return nodes

    monkeypatch.setattr(project_service, "list_files", list_files_then_delete)

    rebuilt = store.load_or_rebuild(project_id)
    assert "vanishing.tex" not in rebuilt.files
    assert "main.tex" in rebuilt.files

    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT rel_path FROM project_index_files WHERE project_id = ?;",
            (project_id,),
        ).fetchall()
    assert "vanishing.tex" not in {r["rel_path"] for r in rows}


def test_search_project_index_ranks_exact_labels_and_headings_first() -> None:
    intro_tex = "\n".join(
        [
            "\\section{Introduction}",
            "\\label{sec:intro}",
            "We mention thm:main in passing here.",
            "Later we also discuss \\myop in prose.",
        ]
    )
    results_tex = "\n".join(
        [
            "\\newcommand{\\myop}{\\operatorname{Op}}",
            "\\section{Main Results}",
            "\\label{sec:results}",
            "\\begin{theorem}[Main Theorem]",
            "\\label{thm:main}",
            "Every bounded operator satisfies \\myop(x) \\ge 0.",
            "\\end{theorem}",
        ]
    )
    appendix_tex = "\n".join(
        [
            "\\section{Appendix}",
            "\\label{thm:main_aux}",
            "Additional notes on thm:main and \\myop.",
        ]
    )

    project_index = ProjectIndex(
        project_id="proj-search",
        files={
            "chapters/intro.tex": index_tex_text("chapters/intro.tex", intro_tex),
            "chapters/results.tex": index_tex_text("chapters/results.tex", results_tex),
            "appendix/extra.tex": index_tex_text("appendix/extra.tex", appendix_tex),
        },
    )

    # Exact label match ("thm:main") ranks above substring label ("thm:main_aux") and chunk body hits
    hits = search_project_index(project_index, "thm:main")
    assert len(hits) >= 2
    assert all(isinstance(h, IndexSearchHit) for h in hits)
    assert hits[0].file_path == "chapters/results.tex"
    assert hits[0].start_line == 5
    assert hits[0].end_line == 5
    assert hits[0].score > hits[1].score
    assert "label" in hits[0].reason.lower()

    # Overlapping line ranges in the same file are deduplicated
    # (e.g., label at line 5 in chapters/results.tex overlaps theorem env 4-7 and chunk 2-7)
    results_hits = [h for h in hits if h.file_path == "chapters/results.tex"]
    for i, a in enumerate(results_hits):
        for b in results_hits[i + 1 :]:
            assert a.end_line < b.start_line or b.end_line < a.start_line

    # LaTeX command tokenization preserves \myop and ranks macro definition first
    macro_hits = search_project_index(project_index, "\\myop")
    assert macro_hits[0].file_path == "chapters/results.tex"
    assert macro_hits[0].start_line == 1

    # Heading exact match ranks first
    heading_hits = search_project_index(project_index, "Main Results")
    assert heading_hits[0].file_path == "chapters/results.tex"
    assert heading_hits[0].start_line == 2

    # path_filter restricts results to matching file or directory prefix
    filtered = search_project_index(project_index, "thm:main", path_filter="chapters/intro.tex")
    assert filtered
    assert all(h.file_path == "chapters/intro.tex" for h in filtered)

    dir_filtered = search_project_index(project_index, "thm:main", path_filter="appendix")
    assert dir_filtered
    assert all(h.file_path.startswith("appendix/") for h in dir_filtered)

    # active_path breaks ties when scores are equal
    tie_index = ProjectIndex(
        project_id="proj-tie",
        files={
            "a.tex": index_tex_text("a.tex", "\\section{Shared}\nBody\n"),
            "b.tex": index_tex_text("b.tex", "\\section{Shared}\nBody\n"),
        },
    )
    tie_hits = search_project_index(tie_index, "Shared", active_path="b.tex")
    assert tie_hits[0].file_path == "b.tex"


def test_format_project_index_caps_macros_at_20_and_respects_budget() -> None:
    macro_names = [f"\\macro{chr(ord('a') + i)}" for i in range(25)]
    macro_lines = [f"\\newcommand{{{name}}}{{{i}}}" for i, name in enumerate(macro_names)]
    main_tex = "\n".join(
        [
            "\\documentclass{article}",
            *macro_lines,
            "\\begin{document}",
            "\\section{Introduction}",
            "\\label{sec:intro}",
            "\\subsection{Background}",
            "\\begin{theorem}[Key Bound]",
            "\\label{thm:key}",
            "Statement.",
            "\\end{theorem}",
            "\\end{document}",
        ]
    )
    methods_tex = "\n".join(
        [
            "\\section{Methods}",
            "\\label{sec:methods}",
            "Method details.",
        ]
    )

    project_index = ProjectIndex(
        project_id="proj-format",
        files={
            "main.tex": index_tex_text("main.tex", main_tex),
            "chapters/methods.tex": index_tex_text("chapters/methods.tex", methods_tex),
        },
    )

    formatted = format_project_index(
        project_index,
        active_path="chapters/methods.tex",
        root_path="main.tex",
    )

    # Files + line counts
    assert "main.tex" in formatted
    assert "chapters/methods.tex" in formatted
    assert f"{project_index.files['main.tex'].line_count} lines" in formatted

    # Hierarchical headings with file:start-end
    assert "Introduction" in formatted
    assert "Background" in formatted
    assert "main.tex:" in formatted
    assert "chapters/methods.tex:1-1" in formatted

    # Key labels and environments
    assert "sec:intro" in formatted
    assert "thm:key" in formatted
    assert "theorem" in formatted

    # Macro cap at 20 with overflow note
    assert macro_names[0] in formatted
    assert macro_names[19] in formatted
    assert macro_names[20] not in formatted
    assert "(+5 more macros; search index to inspect)" in formatted

    # Respects max_chars budget
    tight = format_project_index(
        project_index,
        active_path="chapters/methods.tex",
        root_path="main.tex",
        max_chars=250,
    )
    assert len(tight) <= 250
    assert len(tight) > 0


def test_format_project_index_preserves_custom_macros_when_headings_exceed_budget() -> None:
    macro_names = [f"\\macro{chr(ord('a') + i)}" for i in range(25)]
    lines = ["\\documentclass{article}"]
    lines.extend(f"\\newcommand{{{name}}}{{{i}}}" for i, name in enumerate(macro_names))
    lines.append("\\begin{document}")
    for i in range(60):
        lines.append(f"\\section{{Long Section Title Number {i} With Extra Words}}")
        lines.append(f"\\label{{sec:long_section_label_number_{i}}}")
    lines.append("\\end{document}")
    tex = "\n".join(lines)

    project_index = ProjectIndex(
        project_id="proj-budget",
        files={"main.tex": index_tex_text("main.tex", tex)},
    )

    formatted = format_project_index(
        project_index,
        active_path="main.tex",
        root_path="main.tex",
        max_chars=1400,
    )
    assert len(formatted) <= 1400
    assert "## Files" in formatted
    assert "## Custom Macros" in formatted
    assert macro_names[0] in formatted
    assert "(+5 more macros; search index to inspect)" in formatted


def test_search_project_index_ranks_multi_token_heading_above_single_token_match() -> None:
    tex = "\n".join(
        [
            "\\section{Results}",
            "Some introductory results.",
            "\\section{Main Results}",
            "The primary findings.",
        ]
    )
    project_index = ProjectIndex(
        project_id="proj-multi-token",
        files={"main.tex": index_tex_text("main.tex", tex)},
    )

    hits = search_project_index(project_index, "where is Main Results")
    assert hits
    assert hits[0].start_line == 3
    assert "Main Results" in hits[0].preview


def test_search_project_index_empty_query_and_limit() -> None:
    tex = "\n".join(f"\\section{{Topic {i}}}\n\\label{{sec:t{i}}}" for i in range(10))
    project_index = ProjectIndex(
        project_id="proj-empty-limit",
        files={"main.tex": index_tex_text("main.tex", tex)},
    )

    assert search_project_index(project_index, "") == []
    assert search_project_index(project_index, "   ") == []

    limited = search_project_index(project_index, "Topic", limit=3)
    assert len(limited) == 3
    assert search_project_index(project_index, "Topic", limit=0) == []
