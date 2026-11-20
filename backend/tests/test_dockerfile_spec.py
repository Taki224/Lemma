from __future__ import annotations

import json
from pathlib import Path
import re

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_dockerfile_uses_upstream_install_tl_gnupg_and_texrunner_wrapper() -> None:
    dockerfile_path = REPO_ROOT / "Dockerfile"
    compose_path = REPO_ROOT / "docker-compose.yml"

    assert dockerfile_path.is_file(), "Dockerfile must exist at repository root"
    assert compose_path.is_file(), "docker-compose.yml must exist at repository root"

    dockerfile_text = dockerfile_path.read_text(encoding="utf-8")

    # Stage 1: Node 22 Alpine frontend builder
    assert "FROM node:22-alpine" in dockerfile_text
    assert "frontend-build" in dockerfile_text

    # Stage 2: python:3.12-slim-bookworm runtime with required OS tools
    assert "FROM python:3.12-slim-bookworm" in dockerfile_text
    for pkg in ("git", "bubblewrap", "gnupg", "wget", "perl", "xz-utils", "fontconfig", "sudo"):
        assert pkg in dockerfile_text, f"Missing system package {pkg} in Dockerfile"

    # Upstream TeX Live install-tl with scheme-basic and tlmgr
    assert "install-tl" in dockerfile_text
    assert "--scheme=scheme-basic" in dockerfile_text
    assert "--no-doc-install" in dockerfile_text
    assert "--no-src-install" in dockerfile_text
    assert "tlmgr install" in dockerfile_text
    assert "latexmk" in dockerfile_text
    assert "biber" in dockerfile_text
    assert "xetex" in dockerfile_text
    assert "fontspec" in dockerfile_text

    # Dedicated UIDs (app 1000, texrunner 1001) and run-as-texrunner wrapper
    assert "1000" in dockerfile_text and "app" in dockerfile_text
    assert "1001" in dockerfile_text and "texrunner" in dockerfile_text
    assert "/usr/local/bin/run-as-texrunner" in dockerfile_text
    assert "/etc/sudoers.d/texrunner" in dockerfile_text
    assert "USER app" in dockerfile_text
    assert "VOLUME [\"/data\"]" in dockerfile_text or "VOLUME /data" in dockerfile_text

    # Python runtime dependencies installed from backend/pyproject.toml
    pyproject_text = (REPO_ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    for py_dep in ("python-multipart", "pymupdf", "pymupdf4llm", "argon2-cffi", "watchfiles"):
        assert py_dep in pyproject_text or py_dep in dockerfile_text

    # Validate docker-compose.yml
    compose_data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    assert "services" in compose_data
    service = next(iter(compose_data["services"].values()))
    volumes = service.get("volumes", [])
    ports = service.get("ports", [])

    assert any("/data" in str(v) for v in volumes), "docker-compose.yml must mount /data volume"
    assert any("8080" in str(p) for p in ports), "docker-compose.yml must expose port 8080"


def test_frontend_package_json_codemirror_lang_latex_version_exists() -> None:
    package_json_path = REPO_ROOT / "frontend" / "package.json"
    assert package_json_path.is_file(), "frontend/package.json must exist"

    pkg_data = json.loads(package_json_path.read_text(encoding="utf-8"))
    deps = pkg_data.get("dependencies", {})
    assert "codemirror-lang-latex" in deps, "codemirror-lang-latex must be in dependencies"

    spec = deps["codemirror-lang-latex"]

    # npm registry for codemirror-lang-latex published versions start at 0.2.0 (latest is 0.6.1).
    # ^0.1.0 does not exist and causes ETARGET failure during Docker frontend-build stage.
    assert spec != "^0.1.0", (
        f"codemirror-lang-latex version '{spec}' does not exist on npm (published versions start at 0.2.0; latest is 0.6.1) "
        "and causes 'npm error code ETARGET: No matching version found for codemirror-lang-latex@^0.1.0' during docker build"
    )

    version_match = re.search(r"(\d+)\.(\d+)\.(\d+)", spec)
    assert version_match, f"Invalid version specification: {spec}"
    major, minor, _ = map(int, version_match.groups())
    assert (major, minor) >= (0, 2), (
        f"codemirror-lang-latex version '{spec}' is below 0.2.0. "
        "Published npm versions for codemirror-lang-latex start at 0.2.0."
    )

