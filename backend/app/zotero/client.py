from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from app.grounding.citation_verifier import parse_bib_file
from app.projects.service import ProjectService
from app.zotero.staging_repo import (
    CandidateSource,
    disambiguate_cite_key,
    normalize_doi,
)

ZOTERO_API_BASE_URL = "https://api.zotero.org"

_CREATE_ITEMS_PATH_RE = re.compile(r"^/(?:users|groups)/[^/]+/items/?$")
_TITLE_NORM_RE = re.compile(r"[^a-z0-9]+")
_YEAR_RE = re.compile(r"\b(18\d{2}|19\d{2}|20\d{2})\b")
_SAFE_CITEKEY_RE = re.compile(r"[^a-zA-Z0-9_:-]+")
_ZOTERO_TO_BIBTEX_TYPE = {
    "journalArticle": "article",
    "magazineArticle": "article",
    "newspaperArticle": "article",
    "conferencePaper": "inproceedings",
    "book": "book",
    "bookSection": "incollection",
    "thesis": "phdthesis",
    "report": "techreport",
    "manuscript": "unpublished",
}
# Zotero's BibTeX export key "%a_%t_%y" skips these title words, so cites written against a Zotero export match.
_ZOTERO_TITLE_BANNED_RE = re.compile(
    r"\b(?:a|an|the|some|from|on|in|to|of|do|with|der|die|das|ein|eine|einer|eines|einem|einen"
    r"|un|une|la|le|l'|les|el|las|los|al|uno|una|unos|unas|de|des|del|d')(?:\s+|\b)"
)
_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(?:all\s+)?(?:previous|prior)", re.IGNORECASE),
    re.compile(r"system\s+prompt\s*:", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"</?\s*untrusted_source_excerpt\b", re.IGNORECASE),
)


class NonDestructivePolicyError(PermissionError):
    """Raised when any code attempts a destructive or non-whitelisted HTTP method/path against Zotero."""


class NonDestructiveZoteroTransport(httpx.AsyncBaseTransport):
    """HTTP transport wrapper that strictly permits ONLY GET requests and create-only POST /items."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        path = request.url.path
        host = (request.url.host or "").lower()

        if host and host not in {"api.zotero.org", "localhost", "127.0.0.1", "testserver"}:
            raise NonDestructivePolicyError(f"Zotero transport blocked request to untrusted host: {host}")

        if method == "GET":
            return await self._inner.handle_async_request(request)

        if method == "POST" and _CREATE_ITEMS_PATH_RE.match(path):
            return await self._inner.handle_async_request(request)

        raise NonDestructivePolicyError(
            f"Zotero non-destructive policy blocked {method} {path}. "
            "Only GET requests and create-only POST /{users|groups}/{id}/items are permitted."
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass(frozen=True)
class ZoteroCollectionInfo:
    key: str
    name: str
    num_items: int
    injection_warning: bool = False


@dataclass(frozen=True)
class ZoteroKeyCheckResult:
    valid: bool
    has_read_scope: bool
    has_write_scope: bool
    collections: list[ZoteroCollectionInfo] = field(default_factory=list)
    injection_warnings: list[str] = field(default_factory=list)
    warning_message: str | None = None


@dataclass(frozen=True)
class ZoteroSyncResult:
    collection_key: str
    synced_bib_count: int
    synced_pdf_count: int
    key_remapping: dict[str, str]
    injection_warnings: list[str]
    etag: str
    added_bib_count: int = 0
    updated_bib_count: int = 0


def _normalize_title(title: str | None) -> str:
    if not title:
        return ""
    return _TITLE_NORM_RE.sub(" ", title.strip().lower()).strip()


def _has_prompt_injection(text: str | None) -> bool:
    if not text:
        return False
    return any(pattern.search(text) for pattern in _INJECTION_PATTERNS)


def _extract_year(raw_date: str | None) -> str | None:
    if not raw_date:
        return None
    match = _YEAR_RE.search(str(raw_date))
    return match.group(1) if match else None


def _derive_zotero_cite_key(data: dict[str, Any]) -> str:
    explicit = str(data.get("citationKey") or "").strip()
    if explicit:
        cleaned = _SAFE_CITEKEY_RE.sub("", explicit)
        if cleaned:
            return cleaned

    extra = str(data.get("extra") or "")
    for line in extra.splitlines():
        if line.lower().startswith("citation key:"):
            candidate = _SAFE_CITEKEY_RE.sub("", line.split(":", 1)[1].strip())
            if candidate:
                return candidate

    creators = data.get("creators") or []
    first_author = "noauthor"
    if isinstance(creators, list) and creators:
        first_c = creators[0]
        if isinstance(first_c, dict):
            raw_last = str(first_c.get("lastName") or first_c.get("name") or "")
            slug_last = _zotero_key_part(raw_last.lower().replace(" ", "_").replace(",", ""))
            if slug_last:
                first_author = slug_last

    title_words = _ZOTERO_TITLE_BANNED_RE.sub("", str(data.get("title") or "").lower()).split()
    title_part = _zotero_key_part(title_words[0]) if title_words else ""

    year = _extract_year(str(data.get("date") or "")) or "nodate"
    return f"{first_author}_{title_part or 'notitle'}_{year}"


def _zotero_key_part(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9_-]+", "", ascii_text)


def _bib_escape(value: str) -> str:
    return re.sub(r"(?<!\\)([&%#])", r"\\\1", value)


def _zotero_item_to_bibtex(cite_key: str, data: dict[str, Any]) -> str:
    item_type = str(data.get("itemType") or "")
    bib_type = _ZOTERO_TO_BIBTEX_TYPE.get(item_type, "misc")

    def text(name: str) -> str:
        return str(data.get(name) or "").strip()

    authors: list[str] = []
    editors: list[str] = []
    creators = data.get("creators") or []
    if isinstance(creators, list):
        for c in creators:
            if not isinstance(c, dict):
                continue
            last = str(c.get("lastName") or c.get("name") or "").strip()
            first = str(c.get("firstName") or "").strip()
            if not last:
                continue
            name = f"{last}, {first}" if first else last
            (editors if c.get("creatorType") in {"editor", "seriesEditor"} else authors).append(name)

    container = ""
    if bib_type == "article":
        container = text("publicationTitle")
    elif bib_type == "inproceedings":
        container = text("proceedingsTitle") or text("conferenceName") or text("publicationTitle")
    elif bib_type == "incollection":
        container = text("bookTitle") or text("publicationTitle")

    fields: list[tuple[str, str]] = [
        ("title", text("title") or "Untitled"),
        ("author", " and ".join(authors)),
        ("editor", " and ".join(editors)),
        ("journal" if bib_type == "article" else "booktitle", container),
        ("volume", text("volume")),
        ("number", text("issue") or text("reportNumber")),
        ("pages", text("pages")),
        ("edition", text("edition")),
        ("series", text("series")),
        ("publisher", text("publisher") or text("repository")),
        ("institution" if bib_type == "techreport" else "school", text("institution") or text("university")),
        ("address", text("place")),
        ("isbn", text("ISBN")),
        ("issn", text("ISSN")),
        ("doi", text("DOI")),
        ("url", text("url")),
        ("urldate", text("accessDate")[:10]),
        ("note", text("archiveID")),
        ("year", _extract_year(text("date")) or ""),
    ]
    verbatim = {"doi", "url", "urldate"}
    body = ",\n".join(
        f"  {name} = {{{value if name in verbatim else _bib_escape(value)}}}"
        for name, value in fields
        if value
    )
    return f"@{bib_type}{{{cite_key},\n{body}\n}}"


def _find_bib_entry_span(bib_text: str, cite_key: str) -> tuple[int, int] | None:
    match = re.search(r"@\w+\s*\{\s*" + re.escape(cite_key) + r"\s*,", bib_text)
    if not match:
        return None
    depth = 0
    for pos in range(bib_text.index("{", match.start()), len(bib_text)):
        if bib_text[pos] == "{":
            depth += 1
        elif bib_text[pos] == "}":
            depth -= 1
            if depth == 0:
                return match.start(), pos + 1
    return None


def _candidate_to_zotero_payload(
    candidate: CandidateSource,
    collection_key: str | None,
) -> dict[str, Any]:
    creators: list[dict[str, str]] = []
    for author in candidate.authors:
        if "," in author:
            last, first = [part.strip() for part in author.split(",", 1)]
            creators.append(
                {"creatorType": "author", "lastName": last, "firstName": first}
            )
        else:
            creators.append({"creatorType": "author", "name": author.strip()})

    payload: dict[str, Any] = {
        "itemType": "journalArticle",
        "title": candidate.title,
        "creators": creators,
        "extra": f"Citation Key: {candidate.cite_key}",
    }
    if candidate.year is not None:
        payload["date"] = str(candidate.year)
    if candidate.doi:
        payload["DOI"] = candidate.doi
    if candidate.abstract:
        payload["abstractNote"] = candidate.abstract
    if collection_key:
        payload["collections"] = [collection_key]
    return payload


class ZoteroClient:
    """Non-destructive Zotero API client (Read Sync & Create-Only Deduplicated Push).

    By architectural invariant, no DELETE, PUT, or PATCH method exists on this client,
    and all HTTP requests pass through `NonDestructiveZoteroTransport`.
    """

    def __init__(
        self,
        api_key: str,
        library_id: str,
        library_type: Literal["user", "group"] = "user",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.library_id = library_id.strip()
        self.library_type: Literal["user", "group"] = (
            "group" if library_type == "group" else "user"
        )
        self._custom_transport = transport
        self.last_status_warning: str | None = None

    @property
    def _library_prefix(self) -> str:
        prefix = "groups" if self.library_type == "group" else "users"
        return f"/{prefix}/{self.library_id}"

    def _create_http_client(self) -> httpx.AsyncClient:
        inner = (
            self._custom_transport
            if self._custom_transport is not None
            else httpx.AsyncHTTPTransport()
        )
        guarded_transport = NonDestructiveZoteroTransport(inner)
        headers = {
            "Zotero-API-Version": "3",
            "Zotero-API-Key": self.api_key,
        }
        return httpx.AsyncClient(
            base_url=ZOTERO_API_BASE_URL,
            headers=headers,
            transport=guarded_transport,
            timeout=20.0,
            follow_redirects=True,
        )

    async def verify_key_and_list_collections(self) -> ZoteroKeyCheckResult:
        if not self.api_key or not self.library_id:
            return ZoteroKeyCheckResult(
                valid=False,
                has_read_scope=False,
                has_write_scope=False,
                warning_message="Zotero API key and Library ID are required.",
            )

        async with self._create_http_client() as http:
            key_resp = await http.get(f"/keys/{self.api_key}")
            if key_resp.status_code != 200:
                return ZoteroKeyCheckResult(
                    valid=False,
                    has_read_scope=False,
                    has_write_scope=False,
                    warning_message=f"Zotero key verification failed (HTTP {key_resp.status_code}).",
                )

            key_data = key_resp.json() if key_resp.content else {}
            access = key_data.get("access") or {}
            scope_bucket: dict[str, Any] = {}
            if self.library_type == "group":
                groups_access = access.get("groups") or {}
                scope_bucket = (
                    groups_access.get(self.library_id)
                    or groups_access.get("all")
                    or {}
                )
            else:
                scope_bucket = access.get("user") or {}

            has_read = bool(scope_bucket.get("library", True))
            has_write = bool(scope_bucket.get("write", False))

            collections: list[ZoteroCollectionInfo] = []
            injection_warnings: list[str] = []

            if has_read:
                coll_resp = await http.get(f"{self._library_prefix}/collections")
                if coll_resp.status_code == 200:
                    raw_colls = coll_resp.json()
                    if isinstance(raw_colls, list):
                        for item in raw_colls:
                            if not isinstance(item, dict):
                                continue
                            data = item.get("data") or {}
                            meta = item.get("meta") or {}
                            c_key = str(
                                item.get("key") or data.get("key") or ""
                            ).strip()
                            c_name = str(data.get("name") or "Untitled Collection")
                            num_items = int(meta.get("numItems") or 0)
                            flagged = _has_prompt_injection(c_name)
                            if flagged:
                                injection_warnings.append(
                                    f"Collection '{c_key}' contains suspicious instruction-override text."
                                )
                            if c_key:
                                collections.append(
                                    ZoteroCollectionInfo(
                                        key=c_key,
                                        name=c_name,
                                        num_items=num_items,
                                        injection_warning=flagged,
                                    )
                                )

            warning_msg: str | None = None
            if not has_write:
                warning_msg = (
                    "Connected with read-only Zotero key (recommended default). "
                    "Push to Zotero is disabled unless write access is granted."
                )

            return ZoteroKeyCheckResult(
                valid=True,
                has_read_scope=has_read,
                has_write_scope=has_write,
                collections=collections,
                injection_warnings=injection_warnings,
                warning_message=warning_msg,
            )

    async def sync_collection_to_project(
        self,
        collection_key: str,
        project_id: str,
        project_service: ProjectService,
        cache_dir: Path,
    ) -> ZoteroSyncResult:
        clean_coll = collection_key.strip()
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)

        try:
            bib_file = project_service.read_file(project_id, "references.bib")
            current_bib_text = bib_file.text
            current_etag: str | None = bib_file.etag
        except FileNotFoundError:
            current_bib_text = ""
            current_etag = None

        existing_entries = parse_bib_file(current_bib_text)
        taken_keys: set[str] = set(existing_entries.keys())
        existing_doi_to_key: dict[str, str] = {}
        for k, entry in existing_entries.items():
            norm_d = normalize_doi(entry.doi)
            if norm_d:
                existing_doi_to_key[norm_d] = k

        key_remapping: dict[str, str] = {}
        injection_warnings: list[str] = []
        appended_blocks: list[str] = []
        working_bib_text = current_bib_text
        refreshed_keys: set[str] = set()
        synced_bib_count = 0
        updated_bib_count = 0
        synced_pdf_count = 0

        async with self._create_http_client() as http:
            items_resp = await http.get(
                f"{self._library_prefix}/collections/{clean_coll}/items"
            )
            items_resp.raise_for_status()
            raw_items = items_resp.json()
            if not isinstance(raw_items, list):
                raw_items = []

            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                data = raw.get("data") or {}
                item_type = str(data.get("itemType") or "")
                item_key = str(raw.get("key") or data.get("key") or "").strip()

                if item_type == "attachment":
                    content_type = str(data.get("contentType") or "").lower()
                    if content_type == "application/pdf" and item_key:
                        try:
                            file_resp = await http.get(
                                f"{self._library_prefix}/items/{item_key}/file"
                            )
                            if file_resp.status_code == 200 and file_resp.content:
                                raw_fname = str(
                                    data.get("filename") or f"{item_key}.pdf"
                                )
                                safe_fname = Path(raw_fname).name
                                safe_item_key = Path(item_key).name
                                if not safe_fname.lower().endswith(".pdf"):
                                    safe_fname = f"{safe_item_key}.pdf"
                                target_pdf = (cache_dir / f"{safe_item_key}_{safe_fname}").resolve()
                                if not target_pdf.is_relative_to(cache_dir.resolve()):
                                    raise PermissionError(f"PDF cache path escapes cache directory: {target_pdf}")
                                target_pdf.write_bytes(file_resp.content)
                                synced_pdf_count += 1
                        except Exception as exc:
                            injection_warnings.append(
                                f"Failed to download PDF attachment for item '{item_key}': {exc}"
                            )
                    continue

                if item_type in {"note", "annotation"}:
                    continue

                title_text = str(data.get("title") or "")
                abstract_text = str(data.get("abstractNote") or "")
                if _has_prompt_injection(title_text) or _has_prompt_injection(
                    abstract_text
                ):
                    injection_warnings.append(
                        f"Zotero item '{item_key}' flagged for potential prompt injection in metadata."
                    )

                desired_key = _derive_zotero_cite_key(data)
                item_doi = normalize_doi(str(data.get("DOI") or ""))

                # The same paper already in references.bib (same DOI, or same key and title) is refreshed
                # from Zotero under its existing key, so \cite commands keep resolving.
                local_key: str | None = None
                if item_doi and item_doi in existing_doi_to_key:
                    local_key = existing_doi_to_key[item_doi]
                elif desired_key in existing_entries and _normalize_title(
                    existing_entries[desired_key].title
                ) == _normalize_title(title_text):
                    local_key = desired_key
                if local_key is not None:
                    if local_key != desired_key:
                        key_remapping[desired_key] = local_key
                    synced_bib_count += 1
                    span = (
                        _find_bib_entry_span(working_bib_text, local_key)
                        if local_key in existing_entries and local_key not in refreshed_keys
                        else None
                    )
                    if span is not None:
                        refreshed_keys.add(local_key)
                        new_block = _zotero_item_to_bibtex(local_key, data)
                        old_block = working_bib_text[span[0] : span[1]]
                        if " ".join(old_block.split()) != " ".join(new_block.split()):
                            working_bib_text = (
                                working_bib_text[: span[0]] + new_block + working_bib_text[span[1] :]
                            )
                            updated_bib_count += 1
                    continue

                resolved_key = disambiguate_cite_key(desired_key, taken_keys)
                if resolved_key != desired_key:
                    key_remapping[desired_key] = resolved_key

                taken_keys.add(resolved_key)
                if item_doi:
                    existing_doi_to_key[item_doi] = resolved_key

                bib_block = _zotero_item_to_bibtex(resolved_key, data)
                appended_blocks.append(bib_block)
                synced_bib_count += 1

        if appended_blocks:
            base = working_bib_text.rstrip()
            prefix = f"{base}\n\n" if base else ""
            merged_bib = prefix + "\n\n".join(appended_blocks) + "\n"
        else:
            merged_bib = working_bib_text

        write_res = project_service.write_file(
            project_id=project_id,
            rel_path="references.bib",
            content=merged_bib,
            expected_etag=current_etag,
        )
        project_service.update_project_settings(
            project_id=project_id,
            zotero_collection_key=clean_coll,
        )

        return ZoteroSyncResult(
            collection_key=clean_coll,
            synced_bib_count=synced_bib_count,
            synced_pdf_count=synced_pdf_count,
            key_remapping=key_remapping,
            injection_warnings=injection_warnings,
            etag=write_res.etag,
            added_bib_count=len(appended_blocks),
            updated_bib_count=updated_bib_count,
        )

    async def create_accepted_item(
        self,
        candidate: CandidateSource,
        collection_key: str | None = None,
        push_enabled: bool = False,
    ) -> str | None:
        if not push_enabled:
            return None

        key_check = await self.verify_key_and_list_collections()
        if not key_check.valid or not key_check.has_write_scope:
            self.last_status_warning = (
                "Zotero API key is read-only or lacks write scope; skipped pushing accepted citation."
            )
            return None

        cand_doi = normalize_doi(candidate.doi)
        cand_title_norm = _normalize_title(candidate.title)

        async with self._create_http_client() as http:
            items_resp = await http.get(f"{self._library_prefix}/items")
            if items_resp.status_code == 200:
                existing_items = items_resp.json()
                if isinstance(existing_items, list):
                    for item in existing_items:
                        if not isinstance(item, dict):
                            continue
                        data = item.get("data") or {}
                        existing_key = str(
                            item.get("key") or data.get("key") or ""
                        ).strip()
                        existing_doi = normalize_doi(str(data.get("DOI") or ""))
                        existing_title_norm = _normalize_title(
                            str(data.get("title") or "")
                        )

                        if cand_doi and existing_doi and cand_doi == existing_doi:
                            return existing_key or None
                        if (
                            cand_title_norm
                            and existing_title_norm
                            and cand_title_norm == existing_title_norm
                        ):
                            return existing_key or None

            payload = [_candidate_to_zotero_payload(candidate, collection_key)]
            post_resp = await http.post(
                f"{self._library_prefix}/items",
                json=payload,
            )
            post_resp.raise_for_status()
            resp_json = post_resp.json() if post_resp.content else {}
            successful = resp_json.get("successful") or {}
            first_hit = successful.get("0") or {}
            created_key = str(first_hit.get("key") or "").strip()
            return created_key or None
