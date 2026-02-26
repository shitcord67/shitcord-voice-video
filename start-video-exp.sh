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
TOKEN_ARG=()
if [[ -n "${TOKEN:-}" ]]; then
  TOKEN_ARG=(--token "$TOKEN")
fi

exec "$VENV_PY" selfbot_voice.py "${TOKEN_ARG[@]}" \
  --log-file "$LOG_FILE" \
  ctui \
  --dave-debug \
  --dave-wait-timeout 20 \
  --exp-self-video \
  --exp-video-opcode \
  "$@"
