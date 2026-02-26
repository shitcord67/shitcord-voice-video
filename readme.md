# status
receiving and sending audio works (tested on linux and macos)
# requirements
relies on discord's custom voice engine built atop WebRTC, which can be fetched using `node downloadVoiceModule.js`. extract `discord_voice.zip` into `node_modules`.
# usage
`node example.js <token> <guild id> <channel id>`

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

# send audio (pipewire/pulseaudio)
use a virtual microphone and feed it with generated noise or a file:

terminal 1:
`./send-audio.sh noise`

or:
`./send-audio.sh file ./rickroll.ogg`

terminal 2 (join + transmit):
`VOICE_SET_DEVICES=1 VOICE_INPUT_DEVICE=discord_tx_source node example.js <token> <guild id> <channel id>`

# record received audio
native capture in `discord_voice` is unstable on some setups and can segfault.

recommended (stable): record the system output monitor with ffmpeg while the bot is connected.

example:
1) start bot normally
`node example.js <token> <guild id> <channel id>`
2) in another terminal, record default output monitor
`ffmpeg -f pulse -i default -ac 2 -ar 48000 recv.wav`

if you still want to force native capture (unsafe):
`VOICE_CAPTURE=1 VOICE_CAPTURE_BACKEND=native VOICE_CAPTURE_FILE=./recv.ogg node example.js <token> <guild id> <channel id>`
