#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHOICE_FILE="${ROOT_DIR}/.install_manager"
CONDA_ENV_FILE="${ROOT_DIR}/.install_conda_env"
DEFAULT_CONDA_ENV="srtgo-env"
MANAGER=""
TARGET=""

usage() {
  cat <<'EOF'
Usage: ./run.sh [options]

Options:
  --ktx          Run KTXgo directly
  --srt          Run SRTgo directly
  -h, --help     Show this help
EOF
}

log() {
  printf '[run] %s\n' "$*"
}

fail() {
  printf '[run][error] %s\n' "$*" >&2
  exit 1
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

load_manager() {
  if [[ ! -f "${CHOICE_FILE}" ]]; then
    fail "No setup choice found. Run ./install.sh first."
  fi

  MANAGER="$(tr -d '[:space:]' < "${CHOICE_FILE}")"
  case "${MANAGER}" in
    uv|conda) ;;
    *)
      fail "Unknown manager in ${CHOICE_FILE}: ${MANAGER}"
      ;;
  esac
  log "Using package manager: ${MANAGER}"
}

activate_uv() {
  local venv_activate="${ROOT_DIR}/.venv/bin/activate"
  [[ -f "${venv_activate}" ]] || fail "uv environment not found. Run ./install.sh --uv first."
  # shellcheck disable=SC1090
  source "${venv_activate}"
}

resolve_conda_env_name() {
  local env_name="${DEFAULT_CONDA_ENV}"
  if [[ -f "${CONDA_ENV_FILE}" ]]; then
    env_name="$(tr -d '[:space:]' < "${CONDA_ENV_FILE}")"
  fi
  if [[ -n "${CONDA_ENV_NAME:-}" ]]; then
    env_name="${CONDA_ENV_NAME}"
  fi
  printf '%s' "${env_name}"
}

activate_conda() {
  has_cmd conda || fail "conda command not found. Install/initialize conda first."

  local conda_base
  conda_base="$(conda info --base 2>/dev/null)" || fail "Failed to resolve conda base path."
  local conda_sh="${conda_base}/etc/profile.d/conda.sh"
  [[ -f "${conda_sh}" ]] || fail "Cannot find conda init script: ${conda_sh}"

  # shellcheck disable=SC1090
  source "${conda_sh}"

  local env_name
  env_name="$(resolve_conda_env_name)"
  conda env list | awk 'NR>2 {print $1}' | grep -Fxq "${env_name}" || fail "Conda env '${env_name}' not found. Run ./install.sh --conda${CONDA_ENV_NAME:+ --env-name ${CONDA_ENV_NAME}} first."

  conda activate "${env_name}"
}

select_target() {
  if [[ -n "${TARGET}" ]]; then
    return
  fi

  if [[ ! -t 0 ]]; then
    fail "Non-interactive shell: pass --ktx or --srt"
  fi

  # Prefer arrow-key selection via python inquirer (installed by install.sh).
  # Do not use command substitution here; that breaks TTY for inquirer.
  if has_cmd python && [[ -t 1 ]]; then
    local choice_file
    choice_file="$(mktemp)"
    local py_status=0

    set +e
    RUN_SH_CHOICE_FILE="${choice_file}" python -c '
import os
import sys

try:
    import inquirer
except Exception:
    sys.exit(2)

out_path = os.environ.get("RUN_SH_CHOICE_FILE", "").strip()
if not out_path:
    sys.exit(3)

try:
    choice = inquirer.list_input(
        message="예약 서비스 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
        choices=[("KTX", "ktx"), ("SRT", "srt")],
    )
except KeyboardInterrupt:
    sys.exit(130)
except Exception:
    # Fallback to numeric prompt without noisy traceback.
    sys.exit(3)

if choice is None:
    sys.exit(130)

with open(out_path, "w", encoding="utf-8") as fp:
    fp.write(str(choice).strip())
'
    py_status=$?
    set -e

    if [[ ${py_status} -eq 0 ]]; then
      local picked
      picked="$(tr -d '[:space:]' < "${choice_file}")"
      rm -f "${choice_file}"
      if [[ "${picked}" == "ktx" || "${picked}" == "srt" ]]; then
        TARGET="${picked}"
        return
      fi
    else
      rm -f "${choice_file}"
      if [[ ${py_status} -eq 130 ]]; then
        fail "Selection cancelled."
      fi
    fi
  fi

  # Fallback to numeric prompt if inquirer is unavailable.
  local answer
  echo "예약 서비스 선택:"
  echo "  1) KTX"
  echo "  2) SRT"
  while true; do
    read -r -p "Select [1/2]: " answer
    case "${answer}" in
      1|ktx|KTX)
        TARGET="ktx"
        break
        ;;
      2|srt|SRT)
        TARGET="srt"
        break
        ;;
      *)
        echo "Please type 1 or 2."
        ;;
    esac
  done
}

run_target() {
  cd "${ROOT_DIR}"
  case "${TARGET}" in
    ktx)
      exec python -m ktxgo
      ;;
    srt)
      exec env SRTGO_RAIL_TYPE="SRT" srtgo
      ;;
    *)
      fail "Unknown target: ${TARGET}"
      ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ktx)
      [[ -z "${TARGET}" ]] || fail "Use only one of --ktx or --srt"
      TARGET="ktx"
      shift
      ;;
    --srt)
      [[ -z "${TARGET}" ]] || fail "Use only one of --ktx or --srt"
      TARGET="srt"
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

load_manager
case "${MANAGER}" in
  uv) activate_uv ;;
  conda) activate_conda ;;
  *) fail "Unsupported manager: ${MANAGER}" ;;
esac

select_target
run_target
