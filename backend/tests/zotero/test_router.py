from __future__ import annotations

from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.config import bootstrap_data_dir
from app.main import create_app


def _login_and_get_csrf(
    client: TestClient, password: str = "zotero-router-pass"
) -> dict[str, str]:
    res = client.post("/api/auth/login", json={"password": password})
    assert res.status_code == 200
    csrf = res.json()["csrf_token"]
    return {"X-CSRF-Token": csrf}


def test_wizard_verify_and_sync_endpoints(tmp_path: Path) -> None:
    settings = bootstrap_data_dir(tmp_path / "data").model_copy(
        update={
            "workspace_password": "zotero-router-pass",
            "privacy_consent_accepted": True,
        }
    )
    app = create_app(settings=settings)

    def mock_zotero_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/keys/"):
            return httpx.Response(
                200,
                json={
                    "key": "zot-test-key",
                    "userID": 98765,
                    "access": {
                        "user": {"library": True, "files": True, "write": False}
                    },
                },
            )
        if request.url.path.endswith("/collections"):
            return httpx.Response(
                200,
                json=[
                    {
                        "key": "COLL_AI",
                        "data": {"key": "COLL_AI", "name": "AI & LaTeX Papers"},
                        "meta": {"numItems": 5},
                    }
                ],
            )
        if request.url.path.endswith("/collections/COLL_AI/items"):
            return httpx.Response(
                200,
                json=[
                    {
                        "key": "ZITEM_10",
                        "data": {
                            "key": "ZITEM_10",
                            "itemType": "journalArticle",
                            "citationKey": "vaswani2017",
                            "title": "Attention Is All You Need",
                            "creators": [
                                {
                                    "firstName": "Ashish",
                                    "lastName": "Vaswani",
                                    "creatorType": "author",
                                }
                            ],
                            "date": "2017",
                            "DOI": "10.48550/arXiv.1706.03762",
                        },
                    },
                    {
                        "key": "ZATT_10",
                        "data": {
                            "key": "ZATT_10",
                            "itemType": "attachment",
                            "parentItem": "ZITEM_10",
                            "contentType": "application/pdf",
                            "filename": "vaswani2017.pdf",
                        },
                    },
                ],
            )
        if request.url.path.endswith("/items/ZATT_10/file"):
            return httpx.Response(200, content=b"%PDF-1.4 mock vaswani pdf")
        return httpx.Response(200, json=[])

    with TestClient(app) as client:
        # Inject mock Zotero transport on app.state so no real network calls occur
        app.state.zotero_transport = httpx.MockTransport(mock_zotero_handler)

        # Unauthenticated check
        unauth = client.post(
            "/api/zotero/verify-key",
            json={"api_key": "zot-test-key", "library_id": "98765"},
        )
        assert unauth.status_code == 401

        headers = _login_and_get_csrf(client)

        # Create a project
        proj_res = client.post(
            "/api/projects",
            json={"name": "Wizard Sync Project"},
            headers=headers,
        )
        assert proj_res.status_code == 201
        project_id = proj_res.json()["project_id"]

        # Initial status before connecting
        initial_status = client.get(f"/api/projects/{project_id}/zotero")
        assert initial_status.status_code == 200
        assert initial_status.json() == {
            "connected": False,
            "collection_key": None,
            "has_credentials": False,
            "library_id": "",
            "library_type": "user",
            "push_enabled": False,
        }

        # Step 1 of wizard: POST /api/zotero/verify-key
        verify_res = client.post(
            "/api/zotero/verify-key",
            json={
                "api_key": "zot-test-key",
                "library_id": "98765",
                "library_type": "user",
                "push_enabled": False,
            },
            headers=headers,
        )
        assert verify_res.status_code == 200
        v_data = verify_res.json()
        assert v_data["valid"] is True
        assert v_data["has_read_scope"] is True
        assert v_data["has_write_scope"] is False
        assert len(v_data["collections"]) == 1
        assert v_data["collections"][0]["key"] == "COLL_AI"
        assert v_data["collections"][0]["name"] == "AI & LaTeX Papers"
        assert v_data["collections"][0]["num_items"] == 5

        # Step 2 & 3 of wizard: POST /api/projects/{project_id}/zotero/sync
        sync_res = client.post(
            f"/api/projects/{project_id}/zotero/sync",
            json={"collection_key": "COLL_AI"},
            headers=headers,
        )
        assert sync_res.status_code == 200
        s_data = sync_res.json()
        assert s_data["collection_key"] == "COLL_AI"
        assert s_data["synced_bib_count"] == 1
        assert s_data["synced_pdf_count"] == 1

        # Pull again from the editor: no collection_key, so the project's linked collection is used
        resync_res = client.post(
            f"/api/projects/{project_id}/zotero/sync", json={}, headers=headers
        )
        assert resync_res.status_code == 200
        assert resync_res.json()["collection_key"] == "COLL_AI"
        assert resync_res.json()["added_bib_count"] == 0

        # Stage a candidate source and exercise GET /candidates, upload-pdf, and decision
        cand = app.state.staging_repo.stage_candidate(
            project_id=project_id,
            cite_key="brown2020gpt3",
            title="Language Models are Few-Shot Learners",
            authors=["Brown, Tom"],
            year=2020,
            doi="10.48550/arXiv.2005.14165",
            abstract="Few-shot learning abstract.",
            source_api="semantic_scholar",
            bibtex="@article{brown2020gpt3,\n  author = {Brown, Tom},\n  title = {Language Models are Few-Shot Learners},\n  year = {2020}\n}",
            grounding_tier="abstract_only",
        )

        # GET /api/projects/{project_id}/candidates
        cands_res = client.get(f"/api/projects/{project_id}/candidates")
        assert cands_res.status_code == 200
        cands_list = cands_res.json()["candidates"]
        assert len(cands_list) == 1
        assert cands_list[0]["cite_key"] == "brown2020gpt3"
        assert cands_list[0]["grounding_tier"] == "abstract_only"

        # POST /api/projects/{project_id}/candidates/{candidate_id}/upload-pdf
        upload_res = client.post(
            f"/api/projects/{project_id}/candidates/{cand.candidate_id}/upload-pdf",
            files={"file": ("brown2020.pdf", b"%PDF-1.5 full text pdf", "application/pdf")},
            headers=headers,
        )
        assert upload_res.status_code == 200
        up_cand = upload_res.json()["candidate"]
        assert up_cand["grounding_tier"] == "full_text_pdf"
        assert up_cand["pdf_path"] is not None

        # POST /api/projects/{project_id}/candidates/decision
        dec_res = client.post(
            f"/api/projects/{project_id}/candidates/decision",
            json={
                "accepted_cite_keys": ["brown2020gpt3"],
                "rejected_cite_keys": [],
            },
            headers=headers,
        )
        assert dec_res.status_code == 200
        dec_data = dec_res.json()
        assert dec_data["accepted_cite_keys"] == ["brown2020gpt3"]
        assert dec_data["key_remapping"] == {}

        # Read references.bib content while Zotero is connected
        bib_before = client.get(f"/api/projects/{project_id}/files/references.bib")
        assert bib_before.status_code == 200
        bib_content_before = bib_before.json()["content"]
        assert "vaswani2017" in bib_content_before
        assert "brown2020gpt3" in bib_content_before

        # Manual editing of references.bib is locked while Zotero is connected
        locked_put = client.put(
            f"/api/projects/{project_id}/files/references.bib",
            json={"content": "@misc{overwritten, title={Nope}}"},
            headers=headers,
        )
        assert locked_put.status_code == 403
        assert (
            client.get(f"/api/projects/{project_id}/files/references.bib").json()["content"]
            == bib_content_before
        )

        # Updating root_tex alone preserves zotero_collection_key
        patch_root = client.patch(
            f"/api/projects/{project_id}/settings",
            json={"root_tex": "main.tex"},
            headers=headers,
        )
        assert patch_root.status_code == 200
        assert patch_root.json()["zotero_collection_key"] == "COLL_AI"

        # Disconnect Zotero: clears zotero_collection_key, preserves references.bib and its content, and unlocks manual editing
        disc_res = client.post(
            f"/api/projects/{project_id}/zotero/disconnect",
            json={},
            headers=headers,
        )
        assert disc_res.status_code == 200
        assert disc_res.json()["zotero_collection_key"] is None

        bib_after = client.get(f"/api/projects/{project_id}/files/references.bib")
        assert bib_after.status_code == 200
        assert bib_after.json()["content"] == bib_content_before

        unlocked_put = client.put(
            f"/api/projects/{project_id}/files/references.bib",
            json={
                "content": bib_content_before + "\n@misc{manual2026, title={Manual Entry}}\n",
                "expected_etag": bib_after.json()["etag"],
            },
            headers=headers,
        )
        assert unlocked_put.status_code == 200
        assert "manual2026" in client.get(f"/api/projects/{project_id}/files/references.bib").json()["content"]

        # Status after disconnect still retains saved credentials
        after_disc_status = client.get(f"/api/projects/{project_id}/zotero")
        assert after_disc_status.status_code == 200
        assert after_disc_status.json()["connected"] is False
        assert after_disc_status.json()["has_credentials"] is True
        assert after_disc_status.json()["library_id"] == "98765"

        # Can re-verify / list collections using saved credentials without re-sending api_key
        reuse_verify = client.post("/api/zotero/verify-key", json={}, headers=headers)
        assert reuse_verify.status_code == 200
        assert reuse_verify.json()["valid"] is True
        assert len(reuse_verify.json()["collections"]) == 1

        # Clearing credentials removes them
        clear_res = client.delete("/api/zotero/credentials", headers=headers)
        assert clear_res.status_code == 200
        assert clear_res.json()["has_credentials"] is False
        assert client.get(f"/api/projects/{project_id}/zotero").json()["has_credentials"] is False

        # Named Zotero key flow via /api/settings/keys and key_name
        add_key = client.post(
            "/api/settings/keys",
            json={"provider": "zotero", "name": "my-zotero", "key": "zot-test-key"},
            headers=headers,
        )
        assert add_key.status_code == 201
        assert add_key.json()["zotero"] == [{"name": "my-zotero"}]

        verify_named = client.post(
            "/api/zotero/verify-key",
            json={
                "key_name": "my-zotero",
                "project_id": project_id,
                "library_id": "98765",
                "library_type": "user",
                "push_enabled": False,
            },
            headers=headers,
        )
        assert verify_named.status_code == 200
        assert verify_named.json()["valid"] is True

        status_named = client.get(f"/api/projects/{project_id}/zotero")
        assert status_named.status_code == 200
        assert status_named.json()["has_credentials"] is True
        assert status_named.json()["key_name"] == "my-zotero"

