#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_PY="/home/duda/discord-voice-cli2/.venv/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
  echo "Missing /home/duda/discord-voice-cli2/.venv. Set up the main venv first." >&2
  exit 1
fi

LOG_FILE="${VIDEO_EXP_LOG:-./video-exp.log}"
GLOBAL_ARGS=()
CTUI_EXTRA_ARGS=()

if [[ -n "${TOKEN:-}" ]]; then
  GLOBAL_ARGS+=(--token "$TOKEN")
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config|--token|--log-file)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for $1" >&2
        exit 1
      fi
      GLOBAL_ARGS+=("$1" "$2")
      shift 2
      ;;
    --)
      shift
      CTUI_EXTRA_ARGS+=("$@")
      break
      ;;
    *)
      CTUI_EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done

exec "$VENV_PY" selfbot_voice.py "${GLOBAL_ARGS[@]}" \
  --log-file "$LOG_FILE" \
  ctui \
  --dave-debug \
  --dave-wait-timeout 20 \
  --exp-self-video \
  --exp-video-opcode \
  "${CTUI_EXTRA_ARGS[@]}"
