#!/usr/bin/env bash
set -euo pipefail

if ! command -v pactl >/dev/null 2>&1; then
  echo "pactl not found"
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found"
  exit 1
fi

MODE="${1:-noise}"              # noise | file
INPUT_FILE="${2:-rickroll.ogg}" # used when MODE=file

SINK_NAME="${VOICE_TX_SINK_NAME:-discord_tx_sink}"
SOURCE_NAME="${VOICE_TX_SOURCE_NAME:-discord_tx_source}"
HAS_FFMPEG_PULSE=0
if ffmpeg -hide_banner -formats 2>/dev/null | rg -q "DE.*pulse"; then
  HAS_FFMPEG_PULSE=1
fi

cleanup() {
  if [[ -n "${SOURCE_MODULE_ID:-}" ]]; then
    pactl unload-module "$SOURCE_MODULE_ID" >/dev/null 2>&1 || true
  fi
  if [[ -n "${SINK_MODULE_ID:-}" ]]; then
    pactl unload-module "$SINK_MODULE_ID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

SINK_MODULE_ID="$(pactl load-module module-null-sink sink_name="$SINK_NAME" sink_properties=device.description=DiscordTxSink)"
SOURCE_MODULE_ID="$(pactl load-module module-remap-source source_name="$SOURCE_NAME" master="${SINK_NAME}.monitor" source_properties=device.description=DiscordTxSource)"

echo "Virtual mic ready: $SOURCE_NAME"
echo "Run bot with:"
echo "VOICE_SET_DEVICES=1 VOICE_INPUT_DEVICE=$SOURCE_NAME node example.js <token> <guild_id> <channel_id>"

play_raw_to_sink() {
  # ffmpeg raw s16le mono 48k -> sink playback
  if [[ "$HAS_FFMPEG_PULSE" -eq 1 ]]; then
    ffmpeg -hide_banner -loglevel warning -re \
      -f s16le -ar 48000 -ac 1 -i - \
      -f pulse "$SINK_NAME"
  else
    if ! command -v pw-play >/dev/null 2>&1; then
      echo "ffmpeg lacks pulse output and pw-play is missing"
      exit 1
    fi
    pw-play --raw --rate 48000 --channels 1 --format s16 --target "$SINK_NAME" -
  fi
}

if [[ "$MODE" == "noise" ]]; then
  echo "Sending generated noise. Ctrl+C to stop."
  exec ffmpeg -hide_banner -loglevel warning -re \
    -f lavfi -i "anoisesrc=color=white:amplitude=0.10:sample_rate=48000" \
    -ac 1 -ar 48000 -f s16le - | play_raw_to_sink
elif [[ "$MODE" == "file" ]]; then
  if [[ ! -f "$INPUT_FILE" ]]; then
    echo "File not found: $INPUT_FILE"
    exit 1
  fi
  echo "Looping file: $INPUT_FILE. Ctrl+C to stop."
  exec ffmpeg -hide_banner -loglevel warning -re -stream_loop -1 \
    -i "$INPUT_FILE" -ac 1 -ar 48000 -f s16le - | play_raw_to_sink
else
  echo "Usage: $0 [noise|file] [path-to-audio-file]"
  exit 1
fi
