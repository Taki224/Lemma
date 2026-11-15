#!/usr/bin/env bash
# ==============================================================================
# run-local.sh: Run Lemma locally for development
# ==============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${REPO_ROOT}/backend"
FRONTEND_DIR="${REPO_ROOT}/frontend"
VENV_DIR="${REPO_ROOT}/.venv"

# Load local environment files if present
if [ -f "${REPO_ROOT}/.env.local" ]; then
  # shellcheck disable=SC1091
  set -a; source "${REPO_ROOT}/.env.local"; set +a
elif [ -f "${REPO_ROOT}/.env" ]; then
  # shellcheck disable=SC1091
  set -a; source "${REPO_ROOT}/.env"; set +a
fi

export DATA_DIR="${DATA_DIR:-${REPO_ROOT}/data}"
export WORKSPACE_PASSWORD="${WORKSPACE_PASSWORD:-password}"

# ------------------------------------------------------------------------------
# 1. Detect Python & Environment
# ------------------------------------------------------------------------------
find_python() {
  if command -v uv >/dev/null 2>&1; then
    echo "uv"
    return 0
  fi
  for cmd in python3.12 python3.11 python3; do
    if command -v "$cmd" >/dev/null 2>&1; then
      local ver
      ver=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
      local major minor
      major=$(echo "$ver" | cut -d. -f1)
      minor=$(echo "$ver" | cut -d. -f2)
      if [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; then
        echo "$cmd"
        return 0
      fi
    fi
  done
  echo ""
}

setup_backend() {
  local py_tool
  py_tool=$(find_python)

  if [ -z "$py_tool" ]; then
    echo "❌ Error: Python >= 3.11 is required. Found none in PATH." >&2
    echo "   Please install Python 3.11+ or install uv (https://docs.astral.sh/uv/)." >&2
    exit 1
  fi

  if [ "$py_tool" = "uv" ]; then
    if [ ! -d "${VENV_DIR}" ]; then
      echo "📦 Creating virtual environment with uv at ${VENV_DIR}..."
      uv venv "${VENV_DIR}" --python 3.12 2>/dev/null || uv venv "${VENV_DIR}"
    fi
    if [ ! -f "${VENV_DIR}/bin/uvicorn" ]; then
      echo "📥 Installing backend dependencies with uv..."
      uv pip install --directory "${BACKEND_DIR}" -e "${BACKEND_DIR}[dev]"
    fi
  else
    if [ ! -d "${VENV_DIR}" ]; then
      echo "📦 Creating virtual environment with ${py_tool} at ${VENV_DIR}..."
      "$py_tool" -m venv "${VENV_DIR}"
    fi
    if [ ! -f "${VENV_DIR}/bin/uvicorn" ]; then
      echo "📥 Installing backend dependencies with pip..."
      "${VENV_DIR}/bin/pip" install --upgrade pip
      "${VENV_DIR}/bin/pip" install -e "${BACKEND_DIR}[dev]"
    fi
  fi
}

setup_frontend() {
  if ! command -v npm >/dev/null 2>&1; then
    echo "❌ Error: Node.js and npm are required for frontend development." >&2
    exit 1
  fi

  if [ ! -d "${FRONTEND_DIR}/node_modules" ]; then
    echo "📥 Installing frontend dependencies..."
    (cd "${FRONTEND_DIR}" && npm install)
  fi
}

# ------------------------------------------------------------------------------
# 2. Command Handlers
# ------------------------------------------------------------------------------
run_backend() {
  setup_backend
  echo "🚀 Starting backend with auto-reload on http://127.0.0.1:8080 ..."
  cd "${BACKEND_DIR}"
  if command -v uv >/dev/null 2>&1; then
    exec uv run --directory "${BACKEND_DIR}" uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8080 --reload --reload-dir app
  else
    exec "${VENV_DIR}/bin/uvicorn" app.main:create_app --factory --host 127.0.0.1 --port 8080 --reload --reload-dir app
  fi
}

run_frontend() {
  setup_frontend
  echo "⚡ Starting Vite dev server with HMR on http://localhost:3000 ..."
  cd "${FRONTEND_DIR}"
  exec npm run dev
}

run_build() {
  setup_frontend
  echo "🏗️  Building frontend production assets..."
  cd "${FRONTEND_DIR}"
  npm run build
}

run_start() {
  setup_backend
  if [ ! -d "${FRONTEND_DIR}/dist" ]; then
    run_build
  fi
  echo "🚀 Starting production server on http://127.0.0.1:8080 (serving frontend dist)..."
  cd "${BACKEND_DIR}"
  if command -v uv >/dev/null 2>&1; then
    exec uv run --directory "${BACKEND_DIR}" uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8080
  else
    exec "${VENV_DIR}/bin/uvicorn" app.main:create_app --factory --host 0.0.0.0 --port 8080
  fi
}

run_dev() {
  setup_backend
  setup_frontend

  echo ""
  echo "=========================================================================="
  echo "  Lemma — Local Live Development"
  echo "=========================================================================="
  echo "  🌐 Frontend (HMR Live Reload): http://localhost:3000"
  echo "  ⚙️  Backend API (Auto Reload):   http://127.0.0.1:8080"
  echo "  📁 Persisted Data:             ${DATA_DIR}"
  echo "  🔑 Default Password:           ${WORKSPACE_PASSWORD}"
  echo "=========================================================================="
  echo "  💡 Edits in frontend/src hot-reload immediately in the browser."
  echo "  💡 Edits in backend/app automatically reload the FastAPI server."
  echo "  🛑 Press Ctrl+C to stop both servers."
  echo "=========================================================================="
  echo ""

  # Track child PIDs for clean termination
  BACKEND_PID=""
  FRONTEND_PID=""

  cleanup() {
    echo ""
    echo "Shutting down local development servers..."
    if [ -n "${BACKEND_PID}" ] && kill -0 "${BACKEND_PID}" 2>/dev/null; then
      kill "${BACKEND_PID}" 2>/dev/null || true
    fi
    if [ -n "${FRONTEND_PID}" ] && kill -0 "${FRONTEND_PID}" 2>/dev/null; then
      kill "${FRONTEND_PID}" 2>/dev/null || true
    fi
    wait 2>/dev/null || true
    echo "Done."
  }
  trap cleanup SIGINT SIGTERM EXIT

  # Start backend in background
  if command -v uv >/dev/null 2>&1; then
    (cd "${BACKEND_DIR}" && uv run --directory "${BACKEND_DIR}" uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8080 --reload --reload-dir app) &
    BACKEND_PID=$!
  else
    (cd "${BACKEND_DIR}" && "${VENV_DIR}/bin/uvicorn" app.main:create_app --factory --host 127.0.0.1 --port 8080 --reload --reload-dir app) &
    BACKEND_PID=$!
  fi

  # Start frontend in background
  (cd "${FRONTEND_DIR}" && npm run dev) &
  FRONTEND_PID=$!

  # Wait for both processes
  wait "${BACKEND_PID}" "${FRONTEND_PID}"
}

run_test() {
  setup_backend
  setup_frontend
  echo "🧪 Running backend tests..."
  if command -v uv >/dev/null 2>&1; then
    (cd "${BACKEND_DIR}" && uv run --directory "${BACKEND_DIR}" --extra dev pytest -m "not requires_tex")
  else
    (cd "${BACKEND_DIR}" && "${VENV_DIR}/bin/pytest" -m "not requires_tex")
  fi

  echo "🧪 Running frontend tests..."
  (cd "${FRONTEND_DIR}" && npm test)
}

# ------------------------------------------------------------------------------
# 3. Main Dispatcher
# ------------------------------------------------------------------------------
COMMAND="${1:-dev}"

case "$COMMAND" in
  dev)
    run_dev
    ;;
  backend)
    run_backend
    ;;
  frontend)
    run_frontend
    ;;
  build)
    run_build
    ;;
  start)
    run_start
    ;;
  test)
    run_test
    ;;
  test-backend)
    setup_backend
    if command -v uv >/dev/null 2>&1; then
      exec uv run --directory "${BACKEND_DIR}" --extra dev pytest -m "not requires_tex"
    else
      exec "${VENV_DIR}/bin/pytest" -m "not requires_tex"
    fi
    ;;
  test-frontend)
    setup_frontend
    exec npm --prefix "${FRONTEND_DIR}" test
    ;;
  *)
    echo "Usage: $0 [dev|backend|frontend|build|start|test|test-backend|test-frontend]"
    exit 1
    ;;
esac
