# syntax=docker/dockerfile:1

# ==============================================================================
# Stage 1: Frontend build (Node 22 Alpine)
# ==============================================================================
FROM node:22-alpine AS frontend-build
WORKDIR /workspace/frontend
COPY frontend/package*.json ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install --no-audit --no-fund; fi
COPY frontend/ ./
RUN npm run build

# ==============================================================================
# Stage 2: Production runtime (Python 3.12 + upstream TeX Live scheme-basic)
# ==============================================================================
FROM python:3.12-slim-bookworm AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PATH="/usr/local/texlive/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Install system utilities and sandbox primitives
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        bubblewrap \
        gnupg \
        wget \
        perl \
        xz-utils \
        fontconfig \
        sudo \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

# Install upstream TeX Live (scheme-basic, no docs/src) with live TLPDB for tlmgr --usermode
RUN set -eux; \
    mkdir -p /tmp/install-tl; \
    cd /tmp/install-tl; \
    wget -qO install-tl-unx.tar.gz https://mirror.ctan.org/systems/texlive/tlnet/install-tl-unx.tar.gz; \
    tar -xzf install-tl-unx.tar.gz --strip-components=1; \
    ./install-tl --scheme=scheme-basic --no-doc-install --no-src-install --no-interaction; \
    TL_BIN="$(dirname "$(find /usr/local/texlive -name tlmgr -type f -o -name tlmgr -type l | head -n 1)")"; \
    ln -s "${TL_BIN}" /usr/local/texlive/bin; \
    /usr/local/texlive/bin/tlmgr option autobackup 0; \
    /usr/local/texlive/bin/tlmgr install latexmk biber synctex graphics-cfg helvetic; \
    rm -rf /tmp/install-tl

# Create dedicated unprivileged users:
# - app (UID 1000): runs FastAPI server and tlmgr usermode helper
# - texrunner (UID 1001): executes untrusted LaTeX compilations in sandbox
RUN groupadd -g 1000 app \
    && useradd -u 1000 -g 1000 -m -d /home/app -s /bin/sh app \
    && groupadd -g 1001 texrunner \
    && useradd -u 1001 -g 1001 -M -d /nonexistent -s /usr/sbin/nologin texrunner \
    && printf 'app ALL=(texrunner) NOPASSWD: SETENV: ALL\nDefaults:app env_keep += "PATH"\n' > /etc/sudoers.d/texrunner \
    && chmod 0440 /etc/sudoers.d/texrunner \
    && printf '#!/bin/sh\nexec sudo -n -E -u texrunner -- env PATH="$PATH" "$@"\n' > /usr/local/bin/run-as-texrunner \
    && chown root:root /usr/local/bin/run-as-texrunner \
    && chmod 0755 /usr/local/bin/run-as-texrunner

WORKDIR /opt/lemma

# Install Python backend dependencies
COPY backend/pyproject.toml /opt/lemma/backend/pyproject.toml
COPY backend/app /opt/lemma/backend/app
COPY --from=frontend-build /workspace/frontend/dist /opt/lemma/frontend/dist

RUN pip install --no-cache-dir /opt/lemma/backend \
    && mkdir -p /data \
    && chown -R app:app /data /opt/lemma

VOLUME ["/data"]
EXPOSE 8080

USER app
WORKDIR /opt/lemma/backend

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
