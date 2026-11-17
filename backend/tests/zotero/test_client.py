from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import bootstrap_data_dir
from app.db import DatabaseManager
from app.grounding.citation_verifier import parse_bib_file
from app.projects.service import ETagConflictError, ProjectService
from app.zotero.client import NonDestructivePolicyError, ZoteroClient
from app.zotero.staging_repo import CandidateSource


@pytest.mark.asyncio
async def test_zotero_transport_guard_blocks_delete_put_patch_and_verifies_write_scope() -> None:
    post_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/keys/"):
            # Simulate a read-only Zotero API key (library read=True, write=False)
            return httpx.Response(
                200,
                json={
                    "key": "readonly-key",
                    "userID": 12345,
                    "access": {
                        "user": {"library": True, "files": True, "notes": False, "write": False}
                    },
                },
            )
        if request.url.path.endswith("/collections"):
            return httpx.Response(
                200,
                json=[
                    {
                        "key": "COLL01",
                        "data": {"key": "COLL01", "name": "Manuscript References"},
                        "meta": {"numItems": 12},
                    }
                ],
            )
        if request.method == "POST":
            post_calls.append(request.url.path)
            return httpx.Response(
                200,
                json={"successful": {"0": {"key": "NEWITEM1"}}},
            )
        return httpx.Response(200, json=[])

    inner_transport = httpx.MockTransport(handler)
    client = ZoteroClient(
        api_key="readonly-key",
        library_id="12345",
        library_type="user",
        transport=inner_transport,
    )

    # 1. Verify no DELETE, PUT, or PATCH method exists on ZoteroClient
    assert not hasattr(client, "delete")
    assert not hasattr(client, "delete_item")
    assert not hasattr(client, "put")
    assert not hasattr(client, "update_item")
    assert not hasattr(client, "patch")

    # 2. Verify transport guard blocks DELETE, PUT, PATCH, and non-/items POST requests directly
    guarded_http = client._create_http_client()
    async with guarded_http:
        for forbidden_method in ("DELETE", "PUT", "PATCH"):
            with pytest.raises(NonDestructivePolicyError):
                await guarded_http.request(
                    forbidden_method, "/users/12345/items/ITEM99"
                )
        # POST to a path other than /users/<id>/items or /groups/<id>/items is also blocked
        with pytest.raises(NonDestructivePolicyError):
            await guarded_http.post("/users/12345/collections", json=[])

    # 3. Verify key check detects read-only scope (has_read_scope=True, has_write_scope=False)
    key_check = await client.verify_key_and_list_collections()
    assert key_check.valid is True
    assert key_check.has_read_scope is True
    assert key_check.has_write_scope is False
    assert len(key_check.collections) == 1
    assert key_check.collections[0].key == "COLL01"
    assert key_check.collections[0].name == "Manuscript References"

    # 4. When push_enabled=True but key is read-only, create_accepted_item skips POST safely
    candidate = CandidateSource(
        candidate_id="cand_1",
        project_id="proj-1",
        cite_key="vaswani2017",
        title="Attention Is All You Need",
        authors=["Vaswani, Ashish"],
        year=2017,
        doi="10.48550/arXiv.1706.03762",
        abstract="Transformer architecture.",
        source_api="semantic_scholar",
        bibtex="@inproceedings{vaswani2017, title={Attention Is All You Need}}",
        grounding_tier="abstract_only",
        pdf_path=None,
        created_at="2026-11-01T00:00:00Z",
        expires_at="2026-11-15T00:00:00Z",
        status="accepted",
    )
    created_key = await client.create_accepted_item(
        candidate=candidate,
        collection_key="COLL01",
        push_enabled=True,
    )
    assert created_key is None
    assert len(post_calls) == 0
    assert client.last_status_warning is not None
    assert "read-only" in client.last_status_warning.lower()


@pytest.mark.asyncio
async def test_create_accepted_item_defaults_push_disabled_and_deduplicates_by_doi_and_title() -> None:
    existing_items: list[dict[str, Any]] = [
        {
            "key": "ZOT_EXISTING_1",
            "data": {
                "key": "ZOT_EXISTING_1",
                "itemType": "journalArticle",
                "title": "Attention Is All You Need",
                "DOI": "10.48550/arXiv.1706.03762",
            },
        }
    ]
    post_payloads: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/keys/"):
            return httpx.Response(
                200,
                json={
                    "key": "rw-key",
                    "userID": 12345,
                    "access": {
                        "user": {"library": True, "files": True, "write": True}
                    },
                },
            )
        if request.url.path.endswith("/collections"):
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path.endswith("/items"):
            return httpx.Response(200, json=existing_items)
        if request.method == "POST" and request.url.path.endswith("/items"):
            body = json.loads(request.content.decode("utf-8"))
            post_payloads.append(body)
            return httpx.Response(
                200,
                json={"successful": {"0": {"key": "ZOT_CREATED_2"}}},
            )
        return httpx.Response(200, json=[])

    client = ZoteroClient(
        api_key="rw-key",
        library_id="12345",
        library_type="user",
        transport=httpx.MockTransport(handler),
    )

    cand_existing_doi = CandidateSource(
        candidate_id="cand_dup",
        project_id="proj-1",
        cite_key="vaswani2017",
        title="Different Title Same DOI",
        authors=["Vaswani, Ashish"],
        year=2017,
        doi="https://doi.org/10.48550/ARXIV.1706.03762",
        abstract=None,
        source_api="crossref",
        bibtex="@article{vaswani2017, title={Attention Is All You Need}}",
        grounding_tier="abstract_only",
        pdf_path=None,
        created_at="2026-11-01T00:00:00Z",
        expires_at="2026-11-15T00:00:00Z",
        status="accepted",
    )

    # 1. Default push_enabled=False -> does nothing and returns None
    res_default = await client.create_accepted_item(
        candidate=cand_existing_doi,
        collection_key="COLL01",
    )
    assert res_default is None
    assert len(post_payloads) == 0

    # 2. push_enabled=True with matching DOI in Zotero -> skips POST and returns existing key
    res_dedup_doi = await client.create_accepted_item(
        candidate=cand_existing_doi,
        collection_key="COLL01",
        push_enabled=True,
    )
    assert res_dedup_doi == "ZOT_EXISTING_1"
    assert len(post_payloads) == 0

    # 3. push_enabled=True with matching normalized title (even without DOI) -> skips POST
    cand_existing_title = CandidateSource(
        candidate_id="cand_title",
        project_id="proj-1",
        cite_key="vaswani2017b",
        title="  Attention is all you need! ",
        authors=["Vaswani, Ashish"],
        year=2017,
        doi=None,
        abstract=None,
        source_api="semantic_scholar",
        bibtex="@article{vaswani2017b, title={Attention Is All You Need}}",
        grounding_tier="abstract_only",
        pdf_path=None,
        created_at="2026-11-01T00:00:00Z",
        expires_at="2026-11-15T00:00:00Z",
        status="accepted",
    )
    res_dedup_title = await client.create_accepted_item(
        candidate=cand_existing_title,
        collection_key="COLL01",
        push_enabled=True,
    )
    assert res_dedup_title == "ZOT_EXISTING_1"
    assert len(post_payloads) == 0

    # 4. Brand new paper -> performs create-only POST /items and returns new key
    cand_brand_new = CandidateSource(
        candidate_id="cand_new",
        project_id="proj-1",
        cite_key="taylor2026lemma",
        title="Sandboxed LaTeX Compilation with Grounded AI",
        authors=["Taylor, Jordan"],
        year=2026,
        doi="10.5555/taylor.2026.999",
        abstract="New paper abstract.",
        source_api="crossref",
        bibtex="@article{taylor2026lemma, title={Sandboxed LaTeX Compilation with Grounded AI}}",
        grounding_tier="abstract_only",
        pdf_path=None,
        created_at="2026-11-01T00:00:00Z",
        expires_at="2026-11-15T00:00:00Z",
        status="accepted",
    )
    res_created = await client.create_accepted_item(
        candidate=cand_brand_new,
        collection_key="COLL01",
        push_enabled=True,
    )
    assert res_created == "ZOT_CREATED_2"
    assert len(post_payloads) == 1


@pytest.mark.asyncio
async def test_sync_merges_verified_bib_with_etag_check_without_removing_local_entries(
    tmp_path: Path,
) -> None:
    settings = bootstrap_data_dir(tmp_path / "data")
    db = DatabaseManager(db_path=settings.db_path, backup_dir=settings.backups_dir)
    db.initialize()
    project_service = ProjectService(data_dir=settings.data_dir, db=db)
    meta = project_service.create_project("Zotero Sync Study")

    # Seed local custom .bib entry and a colliding key "smith2024"
    local_bib = """@book{localcustom1999,
  author = {Local, Author},
  title = {My Custom Local Monograph},
  year = {1999}
}

@article{smith2024,
  author = {Smith, Alice},
  title = {Local Smith Paper},
  year = {2024},
  doi = {10.1000/local.smith.2024}
}
"""
    project_service.write_file(
        project_id=meta.project_id,
        rel_path="references.bib",
        content=local_bib,
        expected_etag=None,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/collections/COLL01/items"):
            return httpx.Response(
                200,
                json=[
                    {
                        "key": "ZITEM1",
                        "data": {
                            "key": "ZITEM1",
                            "itemType": "journalArticle",
                            "citationKey": "smith2024",
                            "title": "Zotero Smith Paper with Different DOI",
                            "creators": [{"firstName": "Bob", "lastName": "Smith", "creatorType": "author"}],
                            "date": "2024",
                            "DOI": "10.2000/zotero.smith.2024",
                        },
                    },
                    {
                        "key": "ZITEM2",
                        "data": {
                            "key": "ZITEM2",
                            "itemType": "conferencePaper",
                            "citationKey": "lewis2020rag",
                            "title": "Retrieval-Augmented Generation",
                            "creators": [{"firstName": "Patrick", "lastName": "Lewis", "creatorType": "author"}],
                            "date": "2020",
                            "DOI": "10.48550/arXiv.2005.11401",
                        },
                    },
                    {
                        "key": "ZPDF1",
                        "data": {
                            "key": "ZPDF1",
                            "itemType": "attachment",
                            "parentItem": "ZITEM2",
                            "contentType": "application/pdf",
                            "filename": "lewis2020rag.pdf",
                        },
                    },
                ],
            )
        if request.url.path.endswith("/items/ZPDF1/file"):
            return httpx.Response(200, content=b"%PDF-1.5 fake zotero pdf bytes")
        return httpx.Response(200, json=[])

    client = ZoteroClient(
        api_key="sync-key",
        library_id="12345",
        library_type="user",
        transport=httpx.MockTransport(handler),
    )

    sync_res = await client.sync_collection_to_project(
        collection_key="COLL01",
        project_id=meta.project_id,
        project_service=project_service,
        cache_dir=settings.zotero_cache_dir,
    )

    assert sync_res.synced_bib_count == 2
    assert sync_res.synced_pdf_count == 1
    assert sync_res.key_remapping == {"smith2024": "smith2024a"}

    # Verify local custom entries survived and colliding Zotero key was disambiguated to smith2024a
    updated_bib = project_service.read_file(meta.project_id, "references.bib")
    entries = parse_bib_file(updated_bib.text)
    assert "localcustom1999" in entries
    assert entries["localcustom1999"].title == "My Custom Local Monograph"
    assert "smith2024" in entries
    assert entries["smith2024"].title == "Local Smith Paper"
    assert "smith2024a" in entries
    assert entries["smith2024a"].title == "Zotero Smith Paper with Different DOI"
    assert "lewis2020rag" in entries

    # Verify ETag check is enforced via ProjectService.write_file when concurrent edit occurs
    orig_write = project_service.write_file

    def simulate_concurrent_modification(project_id: str, rel_path: str, content: bytes | str, expected_etag: str | None = None):
        # Enforce that ZoteroClient passed a non-None expected_etag
        assert expected_etag is not None
        raise ETagConflictError(current_etag="concurrent-etag", server_content="")

    project_service.write_file = simulate_concurrent_modification  # type: ignore[method-assign]
    try:
        with pytest.raises(ETagConflictError):
            await client.sync_collection_to_project(
                collection_key="COLL01",
                project_id=meta.project_id,
                project_service=project_service,
                cache_dir=settings.zotero_cache_dir,
            )
    finally:
        project_service.write_file = orig_write  # type: ignore[method-assign]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            {"title": "ReAct: Synergizing Reasoning and Acting", "date": "2023-05-01",
             "creators": [{"lastName": "Yao", "firstName": "Shunyu"}]},
            "yao_react_2023",
        ),
        (
            {"title": "On the Dangers of Stochastic Parrots", "date": "2021",
             "creators": [{"lastName": "Bender"}]},
            "bender_dangers_2021",
        ),
        (
            {"title": "NVIDIA Management Library", "date": "",
             "creators": [{"name": "NVIDIA Corporation"}]},
            "nvidia_corporation_nvidia_nodate",
        ),
        (
            {"title": "Anything", "date": "2020", "creators": [],
             "extra": "Citation Key: pinned_key"},
            "pinned_key",
        ),
    ],
)
def test_derived_cite_key_matches_zotero_bibtex_export(data: dict, expected: str) -> None:
    from app.zotero.client import _derive_zotero_cite_key

    assert _derive_zotero_cite_key(data) == expected


@pytest.mark.asyncio
async def test_sync_refreshes_existing_entries_from_zotero_under_their_local_keys(
    tmp_path: Path,
) -> None:
    settings = bootstrap_data_dir(tmp_path / "data")
    db = DatabaseManager(db_path=settings.db_path, backup_dir=settings.backups_dir)
    db.initialize()
    project_service = ProjectService(data_dir=settings.data_dir, db=db)
    meta = project_service.create_project("Zotero Refresh Study")

    local_bib = """@misc{myleviathan,
  title = {Fast inference (old title)},
  doi = {10.48550/arXiv.2211.17192}
}

@misc{yao_react_2023,
  title = {ReAct: Synergizing Reasoning and Acting in Language Models},
  author = {Yao, S.}
}

@book{untouched1999,
  title = {Not In Zotero},
  year = {1999}
}
"""
    project_service.write_file(
        project_id=meta.project_id, rel_path="references.bib", content=local_bib, expected_etag=None
    )

    items = [
        {
            "key": "Z1",
            "data": {
                "itemType": "conferencePaper",
                "title": "Fast Inference from Transformers via Speculative Decoding",
                "creators": [{"creatorType": "author", "lastName": "Leviathan", "firstName": "Yaniv"}],
                "proceedingsTitle": "Proceedings of ICML",
                "pages": "19274--19286",
                "date": "2023",
                "DOI": "10.48550/arXiv.2211.17192",
            },
        },
        {
            "key": "Z2",
            "data": {
                "itemType": "conferencePaper",
                "title": "ReAct: Synergizing Reasoning and Acting in Language Models",
                "creators": [{"creatorType": "author", "lastName": "Yao", "firstName": "Shunyu"}],
                "conferenceName": "ICLR",
                "date": "2023",
            },
        },
        {
            "key": "Z3",
            "data": {
                "itemType": "preprint",
                "title": "Brand New Paper",
                "creators": [{"creatorType": "author", "lastName": "Zhang", "firstName": "Li"}],
                "repository": "arXiv",
                "archiveID": "arXiv:2601.00001",
                "date": "2026",
            },
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/collections/COLL01/items"):
            return httpx.Response(200, json=items)
        return httpx.Response(200, json=[])

    client = ZoteroClient(
        api_key="sync-key", library_id="12345", library_type="user",
        transport=httpx.MockTransport(handler),
    )

    first = await client.sync_collection_to_project(
        collection_key="COLL01", project_id=meta.project_id,
        project_service=project_service, cache_dir=settings.zotero_cache_dir,
    )
    assert (first.added_bib_count, first.updated_bib_count) == (1, 2)

    entries = parse_bib_file(project_service.read_file(meta.project_id, "references.bib").text)
    assert set(entries) == {"myleviathan", "yao_react_2023", "untouched1999", "zhang_brand_2026"}
    assert entries["myleviathan"].entry_type == "inproceedings"
    assert entries["myleviathan"].title == "Fast Inference from Transformers via Speculative Decoding"
    assert "booktitle = {Proceedings of ICML}" in entries["myleviathan"].raw_bibtex
    assert "booktitle = {ICLR}" in entries["yao_react_2023"].raw_bibtex
    assert entries["untouched1999"].title == "Not In Zotero"
    assert "publisher = {arXiv}" in entries["zhang_brand_2026"].raw_bibtex

    second = await client.sync_collection_to_project(
        collection_key="COLL01", project_id=meta.project_id,
        project_service=project_service, cache_dir=settings.zotero_cache_dir,
    )
    assert (second.added_bib_count, second.updated_bib_count) == (0, 0)


@pytest.mark.asyncio
async def test_verify_key_rejects_inaccessible_library_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/keys/"):
            return httpx.Response(
                200,
                json={
                    "key": "valid-key",
                    "userID": 12345,
                    "access": {"user": {"library": True, "write": False}},
                },
            )
        if request.url.path == "/users/5W82EMPR/collections":
            return httpx.Response(400, text="Invalid user ID")
        return httpx.Response(404)

    client = ZoteroClient(
        api_key="valid-key",
        library_id="5W82EMPR",
        library_type="user",
        transport=httpx.MockTransport(handler),
    )
    result = await client.verify_key_and_list_collections()
    assert result.valid is False
    assert result.warning_message is not None
    assert "5W82EMPR" in result.warning_message
    assert "400" in result.warning_message


@pytest.mark.asyncio
async def test_sync_root_collection_queries_library_items_and_omits_root_on_push(
    tmp_path: Path,
) -> None:
    settings = bootstrap_data_dir(tmp_path / "lemma_data")
    db = DatabaseManager(db_path=settings.db_path, backup_dir=settings.backups_dir)
    db.initialize()
    project_service = ProjectService(data_dir=settings.data_dir, db=db)
    meta = project_service.create_project("Root Sync Test")

    requested_paths: list[str] = []
    post_payloads: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(f"{request.method} {request.url.path}")
        if request.url.path.startswith("/keys/"):
            return httpx.Response(
                200,
                json={
                    "key": "rw-key",
                    "userID": 12345,
                    "access": {"user": {"library": True, "write": True}},
                },
            )
        if request.url.path == "/users/12345/collections":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/users/12345/items":
            return httpx.Response(
                200,
                json=[
                    {
                        "key": "ITEM1",
                        "data": {
                            "itemType": "journalArticle",
                            "title": "Root Library Paper",
                            "creators": [{"creatorType": "author", "lastName": "Doe", "firstName": "Jane"}],
                            "date": "2025",
                        },
                    }
                ],
            )
        if request.method == "POST" and request.url.path == "/users/12345/items":
            post_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"successful": {"0": {"key": "NEWROOT1"}}})
        return httpx.Response(400, text=f"Unexpected path: {request.url.path}")

    client = ZoteroClient(
        api_key="rw-key",
        library_id="12345",
        library_type="user",
        transport=httpx.MockTransport(handler),
    )

    sync_res = await client.sync_collection_to_project(
        collection_key="root",
        project_id=meta.project_id,
        project_service=project_service,
        cache_dir=settings.zotero_cache_dir,
    )
    assert sync_res.collection_key == "root"
    assert sync_res.synced_bib_count == 1
    assert "GET /users/12345/items" in requested_paths
    assert "GET /users/12345/collections/root/items" not in requested_paths

    candidate = CandidateSource(
        candidate_id="cand_root",
        project_id=meta.project_id,
        cite_key="smith2026",
        title="Pushed Without Root Collection",
        authors=["Smith, Alice"],
        year=2026,
        doi="10.1000/root.test",
        abstract="",
        source_api="semantic_scholar",
        bibtex="@article{smith2026, title={Pushed Without Root Collection}}",
        grounding_tier="abstract_only",
        pdf_path=None,
        created_at="2026-11-01T00:00:00Z",
        expires_at="2026-11-15T00:00:00Z",
        status="accepted",
    )
    created_key = await client.create_accepted_item(
        candidate=candidate,
        collection_key="root",
        push_enabled=True,
    )
    assert created_key == "NEWROOT1"
    assert len(post_payloads) == 1
    assert "collections" not in post_payloads[0][0]
