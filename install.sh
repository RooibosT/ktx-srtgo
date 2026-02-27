#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHOICE_FILE="${ROOT_DIR}/.install_manager"
CONDA_ENV_FILE="${ROOT_DIR}/.install_conda_env"
PYTHON_VERSION="3.11"
CONDA_ENV_NAME="srtgo-env"
MANAGER=""
RECONFIGURE=0

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Options:
  --uv                 Use uv (skip prompt)
  --conda              Use conda (skip prompt)
  --env-name NAME      Conda env name (default: srtgo-env)
  --reconfigure        Re-select package manager and overwrite saved choice
  -h, --help           Show this help
EOF
}

log() {
  printf '[install] %s\n' "$*"
}

fail() {
  printf '[install][error] %s\n' "$*" >&2
  exit 1
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

select_manager() {
  local answer

  if [[ "${RECONFIGURE}" -eq 0 && -f "${CHOICE_FILE}" ]]; then
    MANAGER="$(tr -d '[:space:]' < "${CHOICE_FILE}")"
    if [[ "${MANAGER}" == "uv" || "${MANAGER}" == "conda" ]]; then
      log "Saved package manager: ${MANAGER}"
      return
    fi
  fi

  if [[ ! -t 0 ]]; then
    if has_cmd uv && ! has_cmd conda; then
      MANAGER="uv"
    elif has_cmd conda && ! has_cmd uv; then
      MANAGER="conda"
    else
      fail "Non-interactive shell: pass --uv or --conda"
    fi
    printf '%s\n' "${MANAGER}" > "${CHOICE_FILE}"
    log "Saved package manager: ${MANAGER}"
    return
  fi

  echo "Choose environment manager for first-time setup:"
  echo "  1) uv (recommended)"
  echo "  2) conda"
  while true; do
    read -r -p "Select [1/2]: " answer
    case "${answer}" in
      1|uv|UV)
        MANAGER="uv"
        break
        ;;
      2|conda|CONDA)
        MANAGER="conda"
        break
        ;;
      *)
        echo "Please type 1 or 2."
        ;;
    esac
  done

  printf '%s\n' "${MANAGER}" > "${CHOICE_FILE}"
  log "Saved package manager: ${MANAGER}"
}

setup_uv() {
  has_cmd uv || fail "uv not found. Install uv first: https://docs.astral.sh/uv/"

  log "Creating/updating uv virtual environment (.venv)"
  uv venv --python "${PYTHON_VERSION}" "${ROOT_DIR}/.venv"

  log "Installing Python dependencies for srtgo + ktxgo"
  uv pip install --python "${ROOT_DIR}/.venv/bin/python" -e "${ROOT_DIR}" playwright

  log "Installing Playwright Firefox browser"
  uv run --python "${ROOT_DIR}/.venv/bin/python" playwright install firefox

  cat <<EOF

Setup complete with uv.

Activate:
  source "${ROOT_DIR}/.venv/bin/activate"

Run:
  srtgo
  python -m ktxgo
EOF
}

conda_env_exists() {
  conda env list | awk 'NR>2 {print $1}' | grep -Fxq "${CONDA_ENV_NAME}"
}

setup_conda() {
  has_cmd conda || fail "conda not found. Install Miniconda/Anaconda first."

  if conda_env_exists; then
    log "Conda environment '${CONDA_ENV_NAME}' already exists"
  else
    log "Creating conda environment '${CONDA_ENV_NAME}'"
    conda create -y -n "${CONDA_ENV_NAME}" "python=${PYTHON_VERSION}" pip
  fi

  log "Installing Python dependencies for srtgo + ktxgo"
  conda run -n "${CONDA_ENV_NAME}" pip install -e "${ROOT_DIR}" playwright

  log "Installing Playwright Firefox browser"
  conda run -n "${CONDA_ENV_NAME}" python -m playwright install firefox

  printf '%s\n' "${CONDA_ENV_NAME}" > "${CONDA_ENV_FILE}"
  log "Saved conda environment name: ${CONDA_ENV_NAME}"

  cat <<EOF

Setup complete with conda.

Activate:
  conda activate "${CONDA_ENV_NAME}"

Run:
  srtgo
  python -m ktxgo
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --uv)
      [[ -z "${MANAGER}" ]] || fail "Use only one of --uv or --conda"
      MANAGER="uv"
      shift
      ;;
    --conda)
      [[ -z "${MANAGER}" ]] || fail "Use only one of --uv or --conda"
      MANAGER="conda"
      shift
      ;;
    --env-name)
      [[ $# -ge 2 ]] || fail "--env-name requires a value"
      CONDA_ENV_NAME="$2"
      shift 2
      ;;
    --reconfigure)
      RECONFIGURE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1 (use --help)"
      ;;
  esac
done

if [[ -z "${MANAGER}" ]]; then
  select_manager
else
  printf '%s\n' "${MANAGER}" > "${CHOICE_FILE}"
  log "Using package manager from option: ${MANAGER}"
fi

case "${MANAGER}" in
  uv) setup_uv ;;
  conda) setup_conda ;;
  *) fail "Unsupported manager: ${MANAGER}" ;;
esac
