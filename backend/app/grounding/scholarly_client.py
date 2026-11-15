from __future__ import annotations

import asyncio
import json
import re
import time
import xml.etree.ElementTree as ET
import urllib.parse
from contextlib import closing
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from app.config_secrets import SecretsStore
from app.db import DatabaseManager
from app.zotero.staging_repo import normalize_doi

_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(?:all\s+)?(?:previous|prior)", re.IGNORECASE),
    re.compile(r"system\s+prompt\s*:", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"</?\s*untrusted_source_excerpt\b", re.IGNORECASE),
)

_NORM_CHARS_RE = re.compile(r"[^a-z0-9]+")


def scan_for_prompt_injection(text: str | None) -> list[str]:
    if not text:
        return []
    matches: list[str] = []
    for pattern in _INJECTION_PATTERNS:
        found = pattern.findall(text)
        if found:
            matches.extend(found)
    return matches


def wrap_untrusted_excerpt(text: str, cite_key: str = "", page: int | str = "") -> str:
    cleaned = re.sub(
        r"<\s*/\s*untrusted_source_excerpt\s*>",
        "&lt;/untrusted_source_excerpt&gt;",
        text or "",
        flags=re.IGNORECASE,
    )
    clean_key = re.sub(r"[^\w\.\-\:]", "", str(cite_key)) if cite_key else ""
    attrs = []
    if clean_key:
        attrs.append(f'cite_key="{clean_key}"')
    if page:
        clean_page = re.sub(r"[^\w\-]", "", str(page))
        attrs.append(f'page="{clean_page}"')
    attr_str = f" {' '.join(attrs)}" if attrs else ""
    return f"<untrusted_source_excerpt{attr_str}>{cleaned}</untrusted_source_excerpt>"


@dataclass(frozen=True)
class ScholarlyWork:
    title: str
    authors: list[str]
    year: int | None
    doi: str | None
    arxiv_id: str | None
    abstract: str | None
    oa_pdf_url: str | None
    source_api: str
    metadata_verified: bool
    injection_flagged: bool


def _tokenize_norm(text: str | None) -> set[str]:
    if not text:
        return set()
    cleaned = _NORM_CHARS_RE.sub(" ", text.lower()).strip()
    return set(cleaned.split())


def _check_title_match(actual_title: str, expected_title: str) -> bool:
    act_tokens = _tokenize_norm(actual_title)
    exp_tokens = _tokenize_norm(expected_title)
    if not exp_tokens:
        return True
    overlap = act_tokens.intersection(exp_tokens)
    return len(overlap) / len(exp_tokens) >= 0.6


def _check_author_match(actual_authors: list[str], expected_author: str) -> bool:
    exp_tokens = _tokenize_norm(expected_author)
    if not exp_tokens:
        return True
    for a in actual_authors:
        a_tokens = _tokenize_norm(a)
        if exp_tokens.intersection(a_tokens):
            return True
    return False


class ScholarlyClient:
    """Cached scholarly search client querying Semantic Scholar, Crossref, arXiv, and Unpaywall."""

    def __init__(
        self,
        db: DatabaseManager,
        secrets: SecretsStore,
        transport: httpx.AsyncBaseTransport | None = None,
        cache_ttl_seconds: int = 86400 * 7,
        settings: Any | None = None,
    ) -> None:
        self.db = db
        self.secrets = secrets
        self._custom_transport = transport
        self.cache_ttl_seconds = cache_ttl_seconds
        self.settings = settings

    async def aclose(self) -> None:
        pass

    def _create_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._custom_transport,
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "Lemma/1.0 (mailto:scholar@example.com)"},
        )

    def _get_cached_payload(self, cache_key: str) -> dict[str, Any] | list[Any] | None:
        with closing(self.db.get_connection()) as conn:
            row = conn.execute(
                "SELECT payload_json, created_at FROM scholarly_cache WHERE cache_key = ?;",
                (cache_key,),
            ).fetchone()
            if not row:
                return None
            created_at = int(row["created_at"])
            if time.time() - created_at > self.cache_ttl_seconds:
                return None
            try:
                return json.loads(str(row["payload_json"]))
            except Exception:
                return None

    def _set_cached_payload(
        self, cache_key: str, source_api: str, query_or_doi: str, payload: Any
    ) -> None:
        with closing(self.db.get_connection()) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO scholarly_cache (cache_key, source_api, query_or_doi, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?);
                """,
                (cache_key, source_api, query_or_doi, json.dumps(payload), int(time.time())),
            )

    async def search_works(self, query: str, limit: int = 10) -> list[ScholarlyWork]:
        clean_q = query.strip()
        if not clean_q:
            return []

        cache_key = f"search:all:{clean_q.lower()}:{limit}"
        cached = self._get_cached_payload(cache_key)
        if cached is not None and isinstance(cached, list):
            return [ScholarlyWork(**item) for item in cached]

        tasks = [
            self._search_semantic_scholar(clean_q, limit),
            self._search_crossref(clean_q, limit),
            self._search_arxiv(clean_q, limit),
        ]
        results_nested = await asyncio.gather(*tasks, return_exceptions=True)

        combined: list[ScholarlyWork] = []
        seen_keys: set[str] = set()

        for res in results_nested:
            if isinstance(res, list):
                for work in res:
                    dedup_key = normalize_doi(work.doi) or work.title.lower().strip()
                    if dedup_key not in seen_keys:
                        seen_keys.add(dedup_key)
                        combined.append(work)

        final_list = combined[:limit]
        if final_list:
            self._set_cached_payload(
                cache_key=cache_key,
                source_api="aggregated",
                query_or_doi=clean_q,
                payload=[asdict(w) for w in final_list],
            )
        return final_list

    async def _search_semantic_scholar(self, query: str, limit: int) -> list[ScholarlyWork]:
        cache_key = f"search:s2:{query.lower()}:{limit}"
        cached = self._get_cached_payload(cache_key)
        if cached is not None and isinstance(cached, list):
            return [ScholarlyWork(**item) for item in cached]

        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {
            "query": query,
            "limit": str(limit),
            "fields": "title,authors,year,externalIds,abstract,openAccessPdf",
        }
        works: list[ScholarlyWork] = []
        try:
            async with self._create_http_client() as http:
                resp = await http.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json().get("data") or []
                    for item in data:
                        ext_ids = item.get("externalIds") or {}
                        doi = normalize_doi(ext_ids.get("DOI"))
                        arxiv_id = ext_ids.get("ArXiv")
                        authors = [a.get("name") for a in (item.get("authors") or []) if a.get("name")]
                        oa_pdf = (item.get("openAccessPdf") or {}).get("url")
                        title = item.get("title") or "Untitled"
                        abstract = item.get("abstract")
                        flagged = bool(scan_for_prompt_injection(title) or scan_for_prompt_injection(abstract))
                        works.append(
                            ScholarlyWork(
                                title=title,
                                authors=authors,
                                year=item.get("year"),
                                doi=doi,
                                arxiv_id=arxiv_id,
                                abstract=abstract,
                                oa_pdf_url=oa_pdf,
                                source_api="semantic_scholar",
                                metadata_verified=bool(doi),
                                injection_flagged=flagged,
                            )
                        )
                    if works:
                        self._set_cached_payload(cache_key, "semantic_scholar", query, [asdict(w) for w in works])
        except Exception:
            pass
        return works

    async def _search_crossref(self, query: str, limit: int) -> list[ScholarlyWork]:
        cache_key = f"search:crossref:{query.lower()}:{limit}"
        cached = self._get_cached_payload(cache_key)
        if cached is not None and isinstance(cached, list):
            return [ScholarlyWork(**item) for item in cached]

        url = "https://api.crossref.org/works"
        params = {"query": query, "rows": str(limit)}
        works: list[ScholarlyWork] = []
        try:
            async with self._create_http_client() as http:
                resp = await http.get(url, params=params)
                if resp.status_code == 200:
                    items = (resp.json().get("message") or {}).get("items") or []
                    for item in items:
                        titles = item.get("title") or []
                        title = titles[0] if titles else "Untitled"
                        doi = normalize_doi(item.get("DOI"))
                        raw_authors = item.get("author") or []
                        authors: list[str] = []
                        for a in raw_authors:
                            fam = a.get("family", "")
                            given = a.get("given", "")
                            if fam and given:
                                authors.append(f"{given} {fam}")
                            elif fam:
                                authors.append(fam)
                        year = None
                        created = item.get("created") or {}
                        date_parts = created.get("date-parts") or []
                        if date_parts and date_parts[0]:
                            year = int(date_parts[0][0])
                        abstract = item.get("abstract")
                        flagged = bool(scan_for_prompt_injection(title) or scan_for_prompt_injection(abstract))
                        works.append(
                            ScholarlyWork(
                                title=title,
                                authors=authors,
                                year=year,
                                doi=doi,
                                arxiv_id=None,
                                abstract=abstract,
                                oa_pdf_url=None,
                                source_api="crossref",
                                metadata_verified=bool(doi),
                                injection_flagged=flagged,
                            )
                        )
                    if works:
                        self._set_cached_payload(cache_key, "crossref", query, [asdict(w) for w in works])
        except Exception:
            pass
        return works

    async def _search_arxiv(self, query: str, limit: int) -> list[ScholarlyWork]:
        cache_key = f"search:arxiv:{query.lower()}:{limit}"
        cached = self._get_cached_payload(cache_key)
        if cached is not None and isinstance(cached, list):
            return [ScholarlyWork(**item) for item in cached]

        url = "https://export.arxiv.org/api/query"
        params = {"search_query": f"all:{query}", "max_results": str(limit)}
        works: list[ScholarlyWork] = []
        try:
            async with self._create_http_client() as http:
                resp = await http.get(url, params=params)
                if resp.status_code == 200:
                    root = ET.fromstring(resp.text)
                    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
                    for entry in root.findall("atom:entry", ns):
                        title_el = entry.find("atom:title", ns)
                        title = " ".join((title_el.text or "").split()) if title_el is not None else "Untitled"
                        summary_el = entry.find("atom:summary", ns)
                        abstract = " ".join((summary_el.text or "").split()) if summary_el is not None else None
                        authors = [
                            (a.find("atom:name", ns).text or "").strip()
                            for a in entry.findall("atom:author", ns)
                            if a.find("atom:name", ns) is not None
                        ]
                        doi_el = entry.find("arxiv:doi", ns)
                        doi = normalize_doi(doi_el.text) if doi_el is not None else None
                        id_el = entry.find("atom:id", ns)
                        raw_id = id_el.text if id_el is not None else ""
                        arxiv_id = raw_id.split("/abs/")[-1] if "/abs/" in raw_id else None
                        oa_pdf = None
                        for link in entry.findall("atom:link", ns):
                            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                                oa_pdf = link.get("href")
                        pub_el = entry.find("atom:published", ns)
                        year = int(pub_el.text[:4]) if pub_el is not None and pub_el.text and len(pub_el.text) >= 4 else None
                        flagged = bool(scan_for_prompt_injection(title) or scan_for_prompt_injection(abstract))
                        works.append(
                            ScholarlyWork(
                                title=title,
                                authors=authors,
                                year=year,
                                doi=doi,
                                arxiv_id=arxiv_id,
                                abstract=abstract,
                                oa_pdf_url=oa_pdf,
                                source_api="arxiv",
                                metadata_verified=bool(doi),
                                injection_flagged=flagged,
                            )
                        )
                    if works:
                        self._set_cached_payload(cache_key, "arxiv", query, [asdict(w) for w in works])
        except Exception:
            pass
        return works

    async def verify_doi_metadata(
        self,
        doi: str,
        expected_title: str | None = None,
        expected_author: str | None = None,
    ) -> ScholarlyWork | None:
        norm_d = normalize_doi(doi)
        if not norm_d:
            return None

        cache_key = f"doi:{norm_d}"
        cached = self._get_cached_payload(cache_key)
        work_dict: dict[str, Any] | None = None

        if cached is not None and isinstance(cached, dict):
            work_dict = cached
        else:
            # Query Crossref for authoritative metadata
            quoted_doi = urllib.parse.quote(norm_d, safe="")
            cr_url = f"https://api.crossref.org/works/{quoted_doi}"
            try:
                async with self._create_http_client() as http:
                    cr_resp = await http.get(cr_url)
                    if cr_resp.status_code == 200:
                        msg = cr_resp.json().get("message") or {}
                        titles = msg.get("title") or []
                        title = titles[0] if titles else "Untitled"
                        raw_authors = msg.get("author") or []
                        authors: list[str] = []
                        for a in raw_authors:
                            fam = a.get("family", "")
                            given = a.get("given", "")
                            if fam and given:
                                authors.append(f"{given} {fam}")
                            elif fam:
                                authors.append(fam)
                        year = None
                        created = msg.get("created") or {}
                        date_parts = created.get("date-parts") or []
                        if date_parts and date_parts[0]:
                            year = int(date_parts[0][0])
                        abstract = msg.get("abstract")

                        # Try to resolve OA PDF via links or Unpaywall
                        oa_pdf = None
                        for link in msg.get("link") or []:
                            if link.get("content-type") == "application/pdf":
                                oa_pdf = link.get("URL")
                                break

                        unpaywall_email = self.secrets.get("unpaywall_email") or "scholar@example.com"
                        up_url = f"https://api.unpaywall.org/v2/{norm_d}"
                        try:
                            up_resp = await http.get(up_url, params={"email": unpaywall_email})
                            if up_resp.status_code == 200:
                                up_data = up_resp.json()
                                best_oa = up_data.get("best_oa_location") or {}
                                if best_oa.get("url_for_pdf"):
                                    oa_pdf = best_oa.get("url_for_pdf")
                        except Exception:
                            pass

                        flagged = bool(scan_for_prompt_injection(title) or scan_for_prompt_injection(abstract))
                        work_dict = asdict(
                            ScholarlyWork(
                                title=title,
                                authors=authors,
                                year=year,
                                doi=norm_d,
                                arxiv_id=None,
                                abstract=abstract,
                                oa_pdf_url=oa_pdf,
                                source_api="crossref",
                                metadata_verified=True,
                                injection_flagged=flagged,
                            )
                        )
                        self._set_cached_payload(cache_key, "crossref", norm_d, work_dict)
            except Exception:
                pass

        if not work_dict:
            return None

        work = ScholarlyWork(**work_dict)
        verified = True
        if expected_title and not _check_title_match(work.title, expected_title):
            verified = False
        if expected_author and not _check_author_match(work.authors, expected_author):
            verified = False

        return ScholarlyWork(
            title=work.title,
            authors=work.authors,
            year=work.year,
            doi=work.doi,
            arxiv_id=work.arxiv_id,
            abstract=work.abstract,
            oa_pdf_url=work.oa_pdf_url,
            source_api=work.source_api,
            metadata_verified=verified,
            injection_flagged=work.injection_flagged,
        )
