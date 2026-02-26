# quick start
preferred: start CTUI via the launcher script (auto-creates/uses `.venv`, installs requirements if needed):
`./start-selfbot.sh`

quick targets:
`./start-selfbot.sh --dm <user_id> --mode connect`
`./start-selfbot.sh --guild <guild_id> --channel <channel_id> --mode connect`
`./start-selfbot.sh --list`
(`--dm/--guild/--channel` now start CTUI and auto-connect. use `--direct` to skip CTUI.)

token handling:
- reads token from `.voice-config.json` or `DISCORD_USER_TOKEN` by default
- optional override: `./start-selfbot.sh --token <token>`

# python selfbot prototype (discord.py-self)
this is a separate path from the node native module.

local config file (already git-ignored):
`.voice-config.json` with:
`{"token":"...","guild_id":123,"channel_id":456,"dm_user_id":789}`

install:
`python -m venv .venv && source .venv/bin/activate && pip install -r requirements-selfbot.txt`

list guilds + voice channels:
`python selfbot_voice.py list`

play file:
`python selfbot_voice.py play --mode file --file ./rickroll.ogg --loop`

play generated noise:
`python selfbot_voice.py play --mode noise`

play in a dm call:
`python selfbot_voice.py dm-play --user-id <user_id> --mode file --file ./rickroll.ogg --loop`

ring user when starting dm call:
`python selfbot_voice.py dm-play --user-id <user_id> --ring --mode noise`

print DAVE negotiation status:
`python selfbot_voice.py play --mode file --file ./rickroll.ogg --dave-debug`

require DAVE (abort if not encrypted/active):
`python selfbot_voice.py play --mode file --file ./rickroll.ogg --require-dave`

interactive TUI (DM/Guild -> Input ID/List -> connect):
`python selfbot_voice.py tui --dave-debug --dave-wait-timeout 20`

once connected in TUI, use `session>` commands without restarting:
`help`, `mode file|noise|mic|connect`, `file <path>`, `source <pulse_source>`, `sink <pulse_sink>`, `switch`, `leave`

curses full-screen TUI:
`python selfbot_voice.py ctui --dave-debug --dave-wait-timeout 20`

ctui features:
type to search in lists (fuzzy in-order; prefix matches ranked first), press `p` in DM/Guild lists for SIXEL preview (run with `--sixel`, requires `chafa`).
guild list shows `vc_users` + `active_vc`.
in voice-channel lists press `u` to open a searchable member list.
main menu has `Find User in Voice` for global searchable user-in-voice lookup with quick join.
`Ctrl+K` quick jump, mouse click selection (if terminal supports it), incoming call notifications, missed-call list, and connected-user talk/mic/spk panel.
press `p` in voice-user/member lists for SIXEL user avatar preview.

# OLD (node prototype)
status: receiving and sending audio works (tested on linux and macos)

requirements:
relies on discord's custom voice engine built atop WebRTC, which can be fetched using `node downloadVoiceModule.js`. extract `discord_voice.zip` into `node_modules`.

usage:
`node example.js <token> <guild id> <channel id>`

send audio (pipewire/pulseaudio):
use a virtual microphone and feed it with generated noise or a file:

terminal 1:
`./send-audio.sh noise`

or:
`./send-audio.sh file ./rickroll.ogg`

terminal 2 (join + transmit):
`VOICE_SET_DEVICES=1 VOICE_INPUT_DEVICE=discord_tx_source node example.js <token> <guild id> <channel id>`

record received audio:
native capture in `discord_voice` is unstable on some setups and can segfault.

recommended (stable): record the system output monitor with ffmpeg while the bot is connected.

example:
1) start bot normally
`node example.js <token> <guild id> <channel id>`
2) in another terminal, record default output monitor
`ffmpeg -f pulse -i default -ac 2 -ar 48000 recv.wav`

if you still want to force native capture (unsafe):
`VOICE_CAPTURE=1 VOICE_CAPTURE_BACKEND=native VOICE_CAPTURE_FILE=./recv.ogg node example.js <token> <guild id> <channel id>`
