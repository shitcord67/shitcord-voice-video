const WebSocket = require('ws')
global.features = { declareSupported() { } } // discord_voice/index.js crashes if this is not defined
const VoiceEngine = require('discord_voice')
const { randomUUID } = require('crypto')
const fs = require('fs')
const path = require('path')

const CLIENT_CODECS = [{
    type: "audio",
    name: "opus",
    priority: 1000,
    payload_type: 120
}]

// Native device initialization can segfault in headless/minimal environments.
// Opt in only when explicitly requested, or when explicit device names are provided.
const configuredInputDevice = process.env.VOICE_INPUT_DEVICE
const configuredOutputDevice = process.env.VOICE_OUTPUT_DEVICE
if (process.env.VOICE_SET_DEVICES === '1' || configuredInputDevice || configuredOutputDevice) {
    VoiceEngine.setInputDevice(configuredInputDevice || 'default')
    VoiceEngine.setOutputDevice(configuredOutputDevice || 'default')
}

const ws = new WebSocket('wss://gateway.discord.gg/?encoding=json&v=9')
ws.on('message', onMessage)
let wsOpen = false
const pendingGatewaySends = []

ws.on('open', () => {
    wsOpen = true
    while (pendingGatewaySends.length > 0) {
        ws.send(pendingGatewaySends.shift())
    }
})

function sendGateway(payload) {
    const data = JSON.stringify(payload)
    if (wsOpen && ws.readyState === WebSocket.OPEN) {
        ws.send(data)
        return
    }
    pendingGatewaySends.push(data)
}

function login(token) { // [1]
    sendGateway({
        op: 2,
        d: {
            token,
            capabilities: 509,
            properties: {
                os: "Linux",
                browser: "Discord Client",
                release_channel: "stable",
                client_version: "0.0.18",
                os_version: "5.18.13-200.fc36.x86_64",
                os_arch: "x64",
                system_locale: "en-US",
                window_manager: "GNOME,gnome-xorg",
                client_build_number: 138734,
                client_event_source: null
            },
            presence: { status: "online", since: 0, activities: [], afk: false },
            compress: false,
            client_state: { guild_hashes: {}, highest_last_message_id: "0", read_state_version: 0, user_guild_settings_version: -1 },
        }
    })
}


let seq = 0
function onMessage(msg) {
    msg = JSON.parse(msg)
    switch (msg.op) {
        case 0:
            onEvent(msg)
            break
        case 10: // Hello
            setInterval(() => ws.send(JSON.stringify({ op: 1, d: seq })), msg.d.heartbeat_interval)
    }
}

let voiceState, voiceGateway, userId
function onEvent(msg) {
    seq = msg.s

    switch (msg.t) {
        case 'READY': //[2]
            userId = msg.d.user.id
            break
        case 'VOICE_STATE_UPDATE': // [4]
            if (msg.d.user_id == userId) voiceState = msg.d
            break
        case 'VOICE_SERVER_UPDATE': // [5]
            const { endpoint, token, guild_id, channel_id } = msg.d
            voiceGateway = new WebSocket('wss://' + endpoint + '?v=7')
            if (process.env.DEBUG) voiceGateway.on('message', msg => console.log('VoiceGateway recv', JSON.parse(msg)))
            voiceGateway.on('message', onVoiceMessage)
            voiceGateway.on('open', e => voiceGateway.send(JSON.stringify({
                op: 0,
                d: {
                    server_id: guild_id || channel_id,
                    session_id: voiceState.session_id,
                    token,
                    user_id: voiceState.user_id,
                    video: false
                }
            }))
            )
    }
}


let voiceInstance
let captureState
let speakingKeepalive

function toBuffer(value) {
    if (Buffer.isBuffer(value)) return value
    if (value instanceof Uint8Array) return Buffer.from(value)
    if (ArrayBuffer.isView(value)) return Buffer.from(value.buffer, value.byteOffset, value.byteLength)
    if (value instanceof ArrayBuffer) return Buffer.from(value)
    return null
}

function findBinaryPayload(value, visited = new Set()) {
    if (value == null) return null
    if (typeof value !== 'object') return null
    if (visited.has(value)) return null
    visited.add(value)

    const direct = toBuffer(value)
    if (direct) return direct

    if (Array.isArray(value)) {
        if (value.length > 0 && value.every((n) => typeof n === 'number' && Number.isFinite(n))) {
            // Native callbacks may provide raw samples as numeric arrays instead of TypedArrays.
            const asBytes = value.every((n) => Number.isInteger(n) && n >= 0 && n <= 255)
            if (asBytes) return Buffer.from(value)

            const pcm = Buffer.allocUnsafe(value.length * 2)
            for (let i = 0; i < value.length; i++) {
                let s = Math.round(value[i])
                if (s > 32767) s = 32767
                if (s < -32768) s = -32768
                pcm.writeInt16LE(s, i * 2)
            }
            return pcm
        }
        for (const item of value) {
            const found = findBinaryPayload(item, visited)
            if (found) return found
        }
        return null
    }

    for (const key of Object.keys(value)) {
        const found = findBinaryPayload(value[key], visited)
        if (found) return found
    }
    return null
}

function initReceiveCapture() {
    if (captureState || process.env.VOICE_CAPTURE !== '1') return

    if (process.env.VOICE_CAPTURE_BACKEND !== 'native') {
        console.warn('VOICE_CAPTURE requested, but native capture is disabled by default (it crashes in discord_voice on this setup).')
        console.warn('Use external capture (ffmpeg/pulse monitor) or set VOICE_CAPTURE_BACKEND=native to force (unsafe).')
        return
    }

    const outputPath = path.resolve(process.env.VOICE_CAPTURE_FILE || `voice-recv-${Date.now()}.ogg`)
    captureState = { outputPath, started: false }
    console.log('Receive capture enabled:', outputPath)

    try {
        VoiceEngine.startLocalAudioRecording({ filePath: outputPath }, () => {
            if (process.env.VOICE_CAPTURE_DEBUG === '1') console.log('startLocalAudioRecording callback')
        })
        captureState.started = true
    } catch (err) {
        console.error('startLocalAudioRecording failed', err)
    }

    process.on('exit', () => {
        if (!captureState.started) return
        try {
            VoiceEngine.stopLocalAudioRecording(() => {
                if (process.env.VOICE_CAPTURE_DEBUG === '1') console.log('stopLocalAudioRecording callback')
            })
        } catch (err) {
            console.error('stopLocalAudioRecording failed', err)
        }
    })
}

function onVoiceMessage(msg) {
    msg = JSON.parse(msg)
    switch (msg.op) {
        case 2: // Ready [7]
            const { ip: address, port, ssrc, modes, experiments, streams: streamParameters } = msg.d
            voiceInstance = VoiceEngine.createVoiceConnectionWithOptions(voiceState.user_id, { address, port, ssrc, modes, experiments: experiments.concat(['connection_log']), streamParameters, qosEnabled: false }, createVoiceConnectionCallback)
            voiceInstance.setTransportOptions({
                "inputMode": 1,
                "inputModeOptions": {
                    "vadThreshold": -60,
                    "vadAutoThreshold": 3,
                    "vadUseKrisp": false,
                    "vadLeading": 5,
                    "vadTrailing": 25
                }
            })
            voiceInstance.setOnSpeakingCallback((_userId, speaking) => {
                if (_userId == userId)
                    voiceGateway.send(JSON.stringify({ op: 5, d: { ssrc, speaking, delay: 0 } }))
            })
            if (process.env.VOICE_FORCE_SPEAKING === '1') {
                voiceGateway.send(JSON.stringify({ op: 5, d: { ssrc, speaking: 1, delay: 0 } }))
                if (speakingKeepalive) clearInterval(speakingKeepalive)
                speakingKeepalive = setInterval(() => {
                    if (voiceGateway?.readyState === WebSocket.OPEN) {
                        voiceGateway.send(JSON.stringify({ op: 5, d: { ssrc, speaking: 1, delay: 0 } }))
                    }
                }, 2000)
            }

            break
        case 4: //Session Description [9]
            const { mode, secret_key: secretKey } = msg.d
            voiceInstance.setTransportOptions({ encryptionSettings: { mode, secretKey } })
            initReceiveCapture()
            break
        case 8: // Hello [6]
            setInterval(() => voiceGateway.send(JSON.stringify({ op: 3, d: Date.now() })), msg.d.heartbeat_interval)
            break
        case 5: // Speaking //This seems to only be sent once per speaker when using the native client.
            if (voiceInstance && msg?.d?.user_id && msg?.d?.ssrc) {
                voiceInstance.mergeUsers([{ id: msg.d.user_id, ssrc: msg.d.ssrc, videoSsrc: 0, rtxSsrc: 0, mute: false, volume: 1 }])
                try { voiceInstance.setRemoteUserSpeakingStatus(msg.d.user_id, Boolean(msg.d.speaking)) } catch { }
                if (process.env.VOICE_CAPTURE_DEBUG === '1') console.log('VOICE user merged from SPEAKING', msg.d.user_id, msg.d.ssrc)
            }
            break
        case 11: // Clients Connect
            if (voiceInstance && msg?.d?.user_id && msg?.d?.audio_ssrc) {
                voiceInstance.mergeUsers([{ id: msg.d.user_id, ssrc: msg.d.audio_ssrc, videoSsrc: 0, rtxSsrc: 0, mute: false, volume: 1 }])
                if (process.env.VOICE_CAPTURE_DEBUG === '1') console.log('VOICE user merged from CLIENT_CONNECT', msg.d.user_id, msg.d.audio_ssrc)
            }
            break
        case 13: // Client Disconnect
            if (voiceInstance && msg?.d?.user_id) {
                try { voiceInstance.destroyUser(msg.d.user_id) } catch { }
                if (process.env.VOICE_CAPTURE_DEBUG === '1') console.log('VOICE user removed', msg.d.user_id)
            }
            break
    }

}

function createVoiceConnectionCallback(err, { protocol, address, port }) { // [8]
    if (err) return console.error('createVoiceConnection error', err)

    console.log('Voice Connected', protocol, address, port)
    voiceInstance.getEncryptionModes(([mode]) => voiceGateway.send(JSON.stringify({
        op: 1,
        d: {
            protocol, address, port, mode,
            rtc_connection_id: randomUUID(), //this is a client-side generated (in javascript) random UUID4
            data: { address, port, mode },
            codecs: CLIENT_CODECS
        }
    })))
}


function connectVoice(guild_id, channel_id) { // [3]
    //guildId null if DM channel
    sendGateway({
        op: 4,
        d: {
            guild_id, channel_id,
            self_mute: false,
            self_deaf: false,
            self_video: false
        }
    })
}

module.exports = {
    login,
    connectVoice
}

if (process.env.DEBUG) {
    WebSocket.prototype._send = WebSocket.prototype.send
    WebSocket.prototype.send = function (data) {
        console.log('WS send', JSON.parse(data))
        WebSocket.prototype._send.call(this, data)
    }
    
    //ws.on('message', msg => console.debug('RGateway recv', JSON.parse(msg)))
    global.login = login
    global.connectVoice = connectVoice
    global.voiceInstance = voiceInstance
    global.VoiceEngine = VoiceEngine
}
