#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_PY="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
  python3 -m venv .venv
  "$ROOT_DIR/.venv/bin/pip" install -r requirements-selfbot.txt
fi

if ! "$VENV_PY" -c "import discord" >/dev/null 2>&1; then
  "$ROOT_DIR/.venv/bin/pip" install -r requirements-selfbot.txt
fi

TOKEN=""
TARGET_MODE=""
DM_USER_ID=""
GUILD_ID=""
CHANNEL_ID=""
RING=0
AUDIO_MODE="connect"
DIRECT=0
EXTRA_ARGS=()

print_help() {
  cat <<'EOF'
Usage:
  ./start-selfbot.sh [options] [-- <extra args for selfbot_voice.py>]

Options:
  --token <token>         Override token (otherwise .voice-config.json / DISCORD_USER_TOKEN)
  --ctui                  Start curses UI (default if no target is given)
  --tui                   Start line UI
  --list                  List guilds and voice channels
  --dm <user_id>          Preferred: start CTUI and auto-connect this DM call
  --guild <guild_id>      Preferred: start CTUI and auto-connect this guild voice channel
  --channel <channel_id>  Channel ID used with --guild
  --direct                Use direct dm-play/play (no CTUI)
  --ring                  Ring when starting DM call
  --mode <connect|file|noise|mic>
                          Audio mode for direct play/dm-play targets (default: connect)
  -h, --help              Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)
      TOKEN="${2:-}"; shift 2 ;;
    --ctui)
      TARGET_MODE="ctui"; shift ;;
    --tui)
      TARGET_MODE="tui"; shift ;;
    --list)
      TARGET_MODE="list"; shift ;;
    --dm)
      DM_USER_ID="${2:-}"; shift 2 ;;
    --guild)
      GUILD_ID="${2:-}"; shift 2 ;;
    --channel)
      CHANNEL_ID="${2:-}"; shift 2 ;;
    --ring)
      RING=1; shift ;;
    --mode)
      AUDIO_MODE="${2:-}"; shift 2 ;;
    --direct)
      DIRECT=1; shift ;;
    -h|--help)
      print_help; exit 0 ;;
    --)
      shift
      EXTRA_ARGS+=("$@")
      break ;;
    *)
      EXTRA_ARGS+=("$1")
      shift ;;
  esac
done

CMD=("$VENV_PY" "$ROOT_DIR/selfbot_voice.py")
if [[ -n "$TOKEN" ]]; then
  CMD+=("--token" "$TOKEN")
fi

if [[ -n "$GUILD_ID" || -n "$CHANNEL_ID" ]]; then
  if [[ -z "$GUILD_ID" || -z "$CHANNEL_ID" ]]; then
    echo "Error: --guild and --channel must be provided together." >&2
    exit 1
  fi
fi

if [[ "$DIRECT" -eq 1 ]]; then
  if [[ -n "$DM_USER_ID" ]]; then
    CMD+=("dm-play" "--user-id" "$DM_USER_ID" "--mode" "$AUDIO_MODE")
    if [[ "$RING" -eq 1 ]]; then
      CMD+=("--ring")
    fi
  elif [[ -n "$GUILD_ID" ]]; then
    CMD+=("play" "--guild-id" "$GUILD_ID" "--channel-id" "$CHANNEL_ID" "--mode" "$AUDIO_MODE")
  else
    case "${TARGET_MODE:-ctui}" in
      ctui) CMD+=("ctui") ;;
      tui) CMD+=("tui") ;;
      list) CMD+=("list") ;;
      *)
        echo "Error: unsupported mode '$TARGET_MODE'" >&2
        exit 1
        ;;
    esac
  fi
else
  case "${TARGET_MODE:-ctui}" in
    ctui)
      CMD+=("ctui")
      if [[ -n "$DM_USER_ID" ]]; then
        CMD+=("--start-dm-user-id" "$DM_USER_ID")
      elif [[ -n "$GUILD_ID" ]]; then
        CMD+=("--start-guild-id" "$GUILD_ID" "--start-channel-id" "$CHANNEL_ID")
      fi
      ;;
    tui|list)
      CMD+=("${TARGET_MODE}")
      ;;
    *)
      echo "Error: unsupported mode '$TARGET_MODE'" >&2
      exit 1
      ;;
  esac
fi

if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
  CMD+=("${EXTRA_ARGS[@]}")
fi

exec "${CMD[@]}"
