#!/usr/bin/env python3
import argparse
import asyncio
import curses
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from typing import Any, Callable, Optional

import discord

DEFAULT_CONFIG_PATH = ".voice-config.json"


def load_local_config(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class VoiceSelfClient(discord.Client):
    def __init__(self, args: argparse.Namespace):
        super().__init__()
        self.args = args
        self._done = asyncio.Event()
        self._last_dave_status = "DAVE: not checked"
        self._debug_lines: list[str] = []
        self._recent_targets: list[dict] = []
        self._log_file: Optional[str] = args.log_file
        self._speaking_activity: dict[int, float] = {}
        self._active_notice: Optional[dict[str, Any]] = None
        self._call_history: list[dict[str, Any]] = []
        self._active_call_records: dict[int, int] = {}
        self._video_probe_counts: dict[str, int] = {
            "voice_ws_op_2_ready": 0,
            "voice_ws_op_11_clients_connect": 0,
            "voice_ws_op_12_video": 0,
            "voice_ws_op_13_client_disconnect": 0,
            "voice_state_video_toggles": 0,
            "voice_state_stream_toggles": 0,
            "self_video_set_calls": 0,
            "video_opcode_sent_calls": 0,
            "fake_video_packet_sent_calls": 0,
        }
        self._video_probe_events: list[str] = []
        self._video_loop_task: Optional[asyncio.Task] = None
        self._fake_video_loop_task: Optional[asyncio.Task] = None
        self._video_seq: Optional[int] = None
        self._video_ts: Optional[int] = None
        self._video_ssrc: Optional[int] = None
        self._ffmpeg_demuxers_cache: dict[str, set[str]] = {}

    async def on_ready(self):
        try:
            if self.args.command == "list":
                self._print_voice_channels()
            elif self.args.command == "play":
                await self._play_audio()
            elif self.args.command == "dm-play":
                await self._play_dm_audio()
            elif self.args.command == "tui":
                await self._run_tui()
            elif self.args.command == "ctui":
                await self._run_ctui()
            else:
                print(f"Unknown command: {self.args.command}")
        finally:
            self._done.set()

    async def on_call_create(self, call) -> None:
        self._handle_call_event(call, "create")

    async def on_call_update(self, old_call, call) -> None:
        was_ringing = self._is_ringing_me(old_call)
        now_ringing = self._is_ringing_me(call)
        if now_ringing and not was_ringing:
            self._handle_call_event(call, "incoming")
        if self._is_connected_me(call) and not self._is_connected_me(old_call):
            self._mark_call_answered(call)

    async def on_call_delete(self, call) -> None:
        self._finalize_call(call)
        self._dbg(f"call ended in channel {getattr(call.channel, 'id', '?')}")

    async def on_voice_state_update(self, member, before, after) -> None:
        try:
            if bool(getattr(before, "self_video", False)) != bool(getattr(after, "self_video", False)):
                self._video_probe_counts["voice_state_video_toggles"] += 1
                self._dbg(
                    f"voice_state video: user={member} ({member.id}) "
                    f"{getattr(before, 'self_video', False)} -> {getattr(after, 'self_video', False)}"
                )
                self._video_probe_event(
                    f"voice_state video {member} ({member.id}) "
                    f"{getattr(before, 'self_video', False)} -> {getattr(after, 'self_video', False)}"
                )
            if bool(getattr(before, "self_stream", False)) != bool(getattr(after, "self_stream", False)):
                self._video_probe_counts["voice_state_stream_toggles"] += 1
                self._dbg(
                    f"voice_state stream: user={member} ({member.id}) "
                    f"{getattr(before, 'self_stream', False)} -> {getattr(after, 'self_stream', False)}"
                )
                self._video_probe_event(
                    f"voice_state stream {member} ({member.id}) "
                    f"{getattr(before, 'self_stream', False)} -> {getattr(after, 'self_stream', False)}"
                )
        except Exception as e:
            self._dbg(f"on_voice_state_update hook error: {e!r}")

    def _video_probe_event(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._video_probe_events.append(f"[{ts}] {msg}")
        if len(self._video_probe_events) > 120:
            self._video_probe_events = self._video_probe_events[-120:]

    def _is_ringing_me(self, call) -> bool:
        me = self.user
        if me is None:
            return False
        try:
            return any(int(u.id) == int(me.id) for u in getattr(call, "ringing", []))
        except Exception:
            return False

    def _handle_call_event(self, call, event_name: str) -> None:
        if not self._is_ringing_me(call):
            return
        peer = getattr(getattr(call, "channel", None), "recipient", None)
        peer_label = str(peer) if peer is not None else f"channel:{getattr(getattr(call, 'channel', None), 'id', '?')}"
        initiator = getattr(call, "initiator", None)
        from_label = str(initiator) if initiator is not None else "unknown"
        msg = f"Incoming call from {from_label} in DM:{peer_label}"
        self._record_incoming_call(call, from_label, peer_label)
        self._push_notice(msg)
        self._emit_call_sound()
        print(msg)
        self._dbg(f"call {event_name}: {msg}")

    def _is_connected_me(self, call) -> bool:
        try:
            return bool(getattr(call, "connected", False))
        except Exception:
            return False

    def _record_incoming_call(self, call, from_label: str, peer_label: str) -> None:
        channel = getattr(call, "channel", None)
        channel_id = int(getattr(channel, "id", 0) or 0)
        now = datetime.now().isoformat(timespec="seconds")
        entry = {
            "ts": now,
            "channel_id": channel_id,
            "peer": peer_label,
            "from": from_label,
            "status": "ringing",
            "answered": False,
            "missed": False,
        }
        for key in list(self._active_call_records.keys()):
            self._active_call_records[key] = int(self._active_call_records[key]) + 1
        self._call_history.insert(0, entry)
        self._call_history = self._call_history[:100]
        max_idx = len(self._call_history) - 1
        for key, idx in list(self._active_call_records.items()):
            if idx > max_idx:
                self._active_call_records.pop(key, None)
        self._active_call_records[channel_id] = 0

    def _mark_call_answered(self, call) -> None:
        channel_id = int(getattr(getattr(call, "channel", None), "id", 0) or 0)
        idx = self._active_call_records.get(channel_id)
        if idx is None:
            return
        if idx < 0 or idx >= len(self._call_history):
            return
        entry = self._call_history[idx]
        entry["answered"] = True
        entry["status"] = "answered"
        entry["missed"] = False
        self._dbg(f"call answered: {entry.get('peer')} channel={channel_id}")

    def _finalize_call(self, call) -> None:
        channel_id = int(getattr(getattr(call, "channel", None), "id", 0) or 0)
        idx = self._active_call_records.pop(channel_id, None)
        if idx is None or idx < 0 or idx >= len(self._call_history):
            return
        entry = self._call_history[idx]
        if entry.get("answered"):
            entry["status"] = "ended"
        else:
            entry["status"] = "missed"
            entry["missed"] = True
            self._push_notice(f"Missed call: {entry.get('peer')}")
        self._dbg(f"call finalized: status={entry.get('status')} channel={channel_id}")

    def _missed_call_entries(self) -> list[dict[str, Any]]:
        return [e for e in self._call_history if e.get("missed")]

    def _recent_call_lines(self, limit: int = 3) -> list[str]:
        if limit <= 0:
            return []
        if not self._call_history:
            return ["  (no recent calls)"][:limit]
        lines = []
        for e in self._call_history[:limit]:
            lines.append(f"  [{e.get('ts')}] {e.get('status')} {e.get('peer')}")
        return lines

    def _push_notice(self, message: str) -> None:
        persistent = bool(getattr(self.args, "call_notify_persistent", False))
        seconds = max(0.0, float(getattr(self.args, "call_notify_seconds", 15.0)))
        expires_at = None if persistent else (time.monotonic() + seconds)
        self._active_notice = {"message": message, "expires_at": expires_at}

    def _current_notice(self) -> Optional[str]:
        if not self._active_notice:
            return None
        exp = self._active_notice.get("expires_at")
        if exp is not None and time.monotonic() > exp:
            self._active_notice = None
            return None
        return str(self._active_notice.get("message") or "")

    def _emit_call_sound(self) -> None:
        cmd = getattr(self.args, "call_notify_cmd", None)
        if cmd:
            try:
                subprocess.Popen(cmd, shell=True)
            except Exception as e:
                self._dbg(f"call notify cmd failed: {e!r}")
        if getattr(self.args, "call_notify_sound", False):
            try:
                print("\a", end="", flush=True)
            except Exception:
                pass

    def _print_voice_channels(self) -> None:
        print(f"Logged in as: {self.user} ({self.user.id})")
        for guild in sorted(self.guilds, key=lambda g: g.name.lower()):
            print(f"Guild: {guild.name} ({guild.id})")
            if not guild.voice_channels:
                print("  - no voice channels")
                continue
            for ch in sorted(guild.voice_channels, key=lambda c: c.position):
                members = len(ch.members)
                print(f"  - {ch.name} (channel_id={ch.id}, members={members})")

    async def _play_audio(self) -> None:
        guild = self.get_guild(self.args.guild_id)
        if guild is None:
            raise RuntimeError(f"Guild not found: {self.args.guild_id}")

        channel = guild.get_channel(self.args.channel_id)
        if channel is None or not isinstance(channel, discord.VoiceChannel):
            raise RuntimeError(f"Voice channel not found: {self.args.channel_id}")

        print(f"Connecting to {guild.name}/{channel.name}...")
        voice = await channel.connect(reconnect=True, self_deaf=False, self_mute=False)
        if self.args.dave_debug or self.args.require_dave:
            await self._wait_for_dave_status(voice, timeout=self.args.dave_wait_timeout)
        if self.args.require_dave:
            self._enforce_dave_or_raise(voice)
        await self._play_to_voice_client(voice, f"{guild.name}/{channel.name}")

    async def _play_dm_audio(self) -> None:
        user = self.get_user(self.args.user_id) or await self.fetch_user(self.args.user_id)
        if user is None:
            raise RuntimeError(f"User not found: {self.args.user_id}")

        dm = user.dm_channel or await user.create_dm()
        if dm is None:
            raise RuntimeError(f"Could not open DM channel with user {self.args.user_id}")

        print(f"Connecting to DM call with {user} (ring={self.args.ring})...")
        voice = await dm.connect(reconnect=True, ring=self.args.ring)
        if self.args.dave_debug or self.args.require_dave:
            await self._wait_for_dave_status(voice, timeout=self.args.dave_wait_timeout)
        if self.args.require_dave:
            self._enforce_dave_or_raise(voice)
        await self._play_to_voice_client(voice, f"DM:{user}")

    async def _run_tui(self) -> None:
        print(f"Logged in as: {self.user} ({self.user.id})")
        self._tui_configure_audio()
        while True:
            root = self._select_menu("Choose target type", ["DM", "Guild", "Quit"])
            if root == 2:
                return

            if root == 0:
                while True:
                    mode = self._select_menu(
                        "DM mode",
                        ["Input ID", "List", f"Toggle ring (currently {'ON' if self.args.ring else 'OFF'})", "Back"],
                    )
                    if mode == 3:
                        break
                    if mode == 2:
                        self.args.ring = not self.args.ring
                        print(f"DM ring is now {'ON' if self.args.ring else 'OFF'}.")
                        continue
                    if mode == 0:
                        user_id = self._prompt_int("User ID")
                        await self._connect_dm_by_user_id(user_id)
                    else:
                        await self._connect_dm_from_list()
            else:
                mode = self._select_menu("Guild mode", ["Input ID", "List", "Back"])
                if mode == 2:
                    continue
                if mode == 0:
                    guild_id = self._prompt_int("Guild ID")
                    channel_id = self._prompt_int("Voice Channel ID")
                    await self._connect_guild_voice(guild_id, channel_id)
                else:
                    await self._connect_guild_from_list()

    async def _run_ctui(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.to_thread(self._ctui_thread_main, loop)
                return
            except KeyboardInterrupt:
                return
            except Exception as e:
                self._dbg(f"ctui crash: {e!r}")
                print(f"CTUI crashed: {e}")
                await asyncio.sleep(0.5)

    def _ctui_thread_main(self, loop: asyncio.AbstractEventLoop) -> None:
        curses.wrapper(self._ctui_main, loop)

    def _ctui_main(self, stdscr, loop: asyncio.AbstractEventLoop) -> None:
        curses.curs_set(0)
        stdscr.keypad(True)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        except Exception:
            pass
        voice = None
        label = ""

        def run(coro):
            return asyncio.run_coroutine_threadsafe(coro, loop).result()

        while True:
            stdscr.clear()
            self._safe_addstr(stdscr, 0, 0, f"discord.py-self ctui  user={self.user} ({self.user.id})")
            if voice is None:
                choice = self._curses_menu(
                    stdscr,
                    "Main",
                    [
                        "Join/Accept DM Call",
                        "Connect DM",
                        "Connect Guild",
                        "Recent",
                        "Find User in Voice",
                        "Quick Jump",
                        "Audio Settings",
                        "Missed Calls",
                        "Quit",
                    ],
                    allow_ctrl_k=True,
                )
                if choice == -1:
                    conn = self._ctui_quick_jump(stdscr, loop)
                    if conn is not None:
                        voice, label = conn
                    continue
                if choice == 0:
                    conn = self._ctui_quick_dm_call(stdscr, loop)
                    if conn is not None:
                        voice, label = conn
                elif choice == 1:
                    target = self._curses_menu(stdscr, "DM", ["Input ID", "List", f"Toggle Ring ({'ON' if self.args.ring else 'OFF'})", "Back"])
                    if target == 0:
                        raw = self._curses_prompt(stdscr, "User ID")
                        if raw.isdigit():
                            try:
                                voice, label = run(self._open_dm_connection_by_id(int(raw)))
                            except Exception as e:
                                self._curses_message(stdscr, f"Error: {e}")
                    elif target == 1:
                        dm_channels = [ch for ch in self.private_channels if isinstance(ch, discord.DMChannel)]
                        if not dm_channels:
                            self._curses_message(stdscr, "No DM channels found.")
                            continue
                        dm_channels = sorted(dm_channels, key=lambda c: str(c.recipient).lower() if c.recipient else "")
                        labels = [
                            (
                                f"{ch.recipient} ({ch.recipient.id}) [{self._dm_call_status(ch)}]{self._dm_media_suffix(ch)}"
                                if ch.recipient
                                else f"unknown ({ch.id}) [{self._dm_call_status(ch)}]"
                            )
                            for ch in dm_channels
                        ]
                        pick = self._curses_menu(
                            stdscr,
                            "Select DM (type to search, p=preview avatar)",
                            labels + ["Back"],
                            preview_callback=lambda i: self._ctui_preview_dm(stdscr, dm_channels[i]),
                        )
                        if pick < len(dm_channels):
                            chosen = dm_channels[pick]
                            uid = chosen.recipient.id if chosen.recipient else None
                            if uid is None:
                                self._curses_message(stdscr, "DM has no recipient id.")
                            else:
                                try:
                                    voice, label = run(self._open_dm_connection_by_id(uid))
                                except Exception as e:
                                    self._curses_message(stdscr, f"Error: {e}")
                    elif target == 2:
                        self.args.ring = not self.args.ring
                elif choice == 2:
                    target = self._curses_menu(stdscr, "Guild", ["Input IDs", "List", "Back"])
                    if target == 0:
                        g = self._curses_prompt(stdscr, "Guild ID")
                        c = self._curses_prompt(stdscr, "Channel ID")
                        if g.isdigit() and c.isdigit():
                            try:
                                voice, label = run(self._open_guild_connection(int(g), int(c)))
                            except Exception as e:
                                self._curses_message(stdscr, f"Error: {e}")
                    elif target == 1:
                        guilds = sorted(self.guilds, key=lambda gg: gg.name.lower())
                        if not guilds:
                            self._curses_message(stdscr, "No guilds found.")
                            continue
                        guild_labels = []
                        for g in guilds:
                            voice_users = sum(len(vc.members) for vc in g.voice_channels)
                            active_channels = sum(1 for vc in g.voice_channels if len(vc.members) > 0)
                            guild_labels.append(
                                f"{g.name} ({g.id}) vc_users={voice_users} active_vc={active_channels}"
                            )
                        gpick = self._curses_menu(
                            stdscr,
                            "Select Guild (type to search, p=preview icon)",
                            guild_labels + ["Back"],
                            preview_callback=lambda i: self._ctui_preview_guild(stdscr, guilds[i]),
                        )
                        if gpick < len(guilds):
                            guild = guilds[gpick]
                            chans = sorted(guild.voice_channels, key=lambda cc: cc.position)
                            if not chans:
                                self._curses_message(stdscr, "Selected guild has no voice channels.")
                                continue
                            cpick = self._curses_menu(
                                stdscr,
                                "Select Voice Channel (u=list users)",
                                [f"{ch.name} ({ch.id}) members={len(ch.members)}" for ch in chans] + ["Back"],
                                key_actions={
                                    ord("u"): lambda i: self._ctui_show_voice_members(stdscr, chans[i]) if i < len(chans) else None
                                },
                            )
                            if cpick < len(chans):
                                ch = chans[cpick]
                                try:
                                    voice, label = run(self._open_guild_connection(guild.id, ch.id))
                                except Exception as e:
                                    self._curses_message(stdscr, f"Error: {e}")
                elif choice == 3:
                    conn = self._ctui_connect_recent(stdscr, loop)
                    if conn is not None:
                        voice, label = conn
                elif choice == 4:
                    conn = self._ctui_find_user_in_voice(stdscr, loop)
                    if conn is not None:
                        voice, label = conn
                elif choice == 5:
                    conn = self._ctui_quick_jump(stdscr, loop)
                    if conn is not None:
                        voice, label = conn
                elif choice == 6:
                    self._ctui_audio_settings(stdscr)
                elif choice == 7:
                    self._ctui_show_missed_calls(stdscr)
                else:
                    return
            else:
                choice = self._curses_menu(
                    stdscr,
                    f"Connected: {label}",
                    [
                        "Restart / Apply audio mode",
                        "Audio settings",
                        "Toggle self camera flag (exp)",
                        "Send VIDEO opcode (exp)",
                        "Toggle VIDEO opcode loop (exp)",
                        "Send fake video RTP packet (exp)",
                        "Toggle fake video RTP loop (exp)",
                        "Show video probe status (exp)",
                        "Show DAVE status",
                        "Show debug log",
                        "Show missed calls",
                        "Show call log",
                        "Join/Accept DM Call",
                        "Quick Jump (Ctrl+K)",
                        "Switch target (disconnect)",
                        "Disconnect",
                        "Quit",
                    ],
                    voice=voice,
                    allow_ctrl_k=True,
                )
                if choice == -1:
                    try:
                        run(self._disconnect_voice(voice))
                    except Exception:
                        pass
                    voice = None
                    label = ""
                    conn = self._ctui_quick_jump(stdscr, loop)
                    if conn is not None:
                        voice, label = conn
                    continue
                if choice == 0:
                    try:
                        run(self._restart_playback(voice, label))
                        self._curses_message(stdscr, "Playback restarted/applied.")
                    except Exception as e:
                        self._curses_message(stdscr, f"Error: {e}")
                elif choice == 1:
                    self._ctui_audio_settings(stdscr)
                elif choice == 2:
                    try:
                        new_state = not bool(getattr(self.args, "exp_self_video", False))
                        run(self._apply_self_video_flag(voice, new_state))
                        self._curses_message(stdscr, f"Experimental self camera flag set to {new_state}.")
                    except Exception as e:
                        self._curses_message(stdscr, f"Error: {e}")
                elif choice == 3:
                    try:
                        run(self._exp_send_video_opcode(voice))
                        self._curses_message(stdscr, "Sent VIDEO opcode.")
                    except Exception as e:
                        self._curses_message(stdscr, f"Error: {e}")
                elif choice == 4:
                    try:
                        enabled = run(self._toggle_video_opcode_loop(voice))
                        self._curses_message(stdscr, f"VIDEO opcode loop is now {'ON' if enabled else 'OFF'}.")
                    except Exception as e:
                        self._curses_message(stdscr, f"Error: {e}")
                elif choice == 5:
                    try:
                        run(self._exp_send_fake_video_packet(voice))
                        self._curses_message(stdscr, "Sent fake video RTP packet.")
                    except Exception as e:
                        self._curses_message(stdscr, f"Error: {e}")
                elif choice == 6:
                    try:
                        enabled = run(self._toggle_fake_video_loop(voice))
                        self._curses_message(stdscr, f"Fake video RTP loop is now {'ON' if enabled else 'OFF'}.")
                    except Exception as e:
                        self._curses_message(stdscr, f"Error: {e}")
                elif choice == 7:
                    self._ctui_show_video_probe(stdscr)
                elif choice == 8:
                    try:
                        run(self._wait_for_dave_status(voice, timeout=max(0.5, self.args.dave_wait_timeout)))
                        self._curses_message(stdscr, self._last_dave_status)
                    except Exception as e:
                        self._curses_message(stdscr, f"Error: {e}")
                elif choice == 9:
                    self._curses_show_debug_log(stdscr)
                elif choice == 10:
                    self._ctui_show_missed_calls(stdscr)
                elif choice == 11:
                    self._ctui_show_call_log(stdscr)
                elif choice == 12:
                    try:
                        run(self._disconnect_voice(voice))
                    except Exception:
                        pass
                    voice = None
                    label = ""
                    conn = self._ctui_quick_dm_call(stdscr, loop)
                    if conn is not None:
                        voice, label = conn
                elif choice == 13:
                    try:
                        run(self._disconnect_voice(voice))
                    except Exception:
                        pass
                    voice = None
                    label = ""
                    conn = self._ctui_quick_jump(stdscr, loop)
                    if conn is not None:
                        voice, label = conn
                elif choice in (14, 15):
                    try:
                        run(self._disconnect_voice(voice))
                    except Exception:
                        pass
                    voice = None
                    label = ""
                else:
                    try:
                        run(self._disconnect_voice(voice))
                    except Exception:
                        pass
                    return

    def _ctui_audio_settings(self, stdscr) -> None:
        mode = self._curses_menu(stdscr, "Audio mode", ["File", "Noise", "Microphone", "Connect only", "Back"])
        if mode == 0:
            self.args.mode = "file"
            raw = self._curses_prompt(stdscr, f"File path [{self.args.file}]")
            if raw:
                self.args.file = raw
            self.args.loop = self._curses_menu(stdscr, "Loop file playback?", ["Yes", "No"]) == 0
        elif mode == 1:
            self.args.mode = "noise"
            raw = self._curses_prompt(stdscr, f"Noise amplitude [{self.args.noise_amp}]")
            if raw:
                try:
                    self.args.noise_amp = float(raw)
                except ValueError:
                    pass
            self.args.loop = False
        elif mode == 2:
            self.args.mode = "mic"
            source_entries = self._pulse_device_entries("sources")
            source_labels = [f"{desc} [{name}]" for name, desc in source_entries]
            if source_entries:
                idx = self._curses_menu(stdscr, "Select Pulse source", source_labels + ["Manual", "Back"])
                if idx < len(source_entries):
                    self.args.pulse_source = source_entries[idx][0]
                elif idx == len(source_entries):
                    manual = self._curses_prompt(stdscr, "Pulse source name")
                    if manual:
                        self.args.pulse_source = manual
            else:
                manual = self._curses_prompt(stdscr, "Pulse source name")
                if manual:
                    self.args.pulse_source = manual
            self.args.loop = False
        elif mode == 3:
            self.args.mode = "connect"
            self.args.loop = False

        sink_entries = self._pulse_device_entries("sinks")
        sink_labels = [f"{desc} [{name}]" for name, desc in sink_entries]
        if sink_entries:
            idx = self._curses_menu(stdscr, "Output sink", ["Keep current"] + sink_labels + ["Manual"])
            if idx >= 1 and idx <= len(sink_entries):
                self.args.pulse_sink = sink_entries[idx - 1][0]
                self._set_default_pulse_device("sink", self.args.pulse_sink)
            elif idx == len(sink_entries) + 1:
                manual = self._curses_prompt(stdscr, "Pulse sink name")
                if manual:
                    self.args.pulse_sink = manual
                    self._set_default_pulse_device("sink", self.args.pulse_sink)

        if self.args.mode == "mic" and self.args.pulse_source:
            self._set_default_pulse_device("source", self.args.pulse_source)

    def _curses_menu(
        self,
        stdscr,
        title: str,
        items: list[str],
        voice: Optional[discord.VoiceClient] = None,
        preview_callback: Optional[Callable[[int], None]] = None,
        key_actions: Optional[dict[int, Callable[[int], None]]] = None,
        allow_ctrl_k: bool = False,
    ) -> int:
        idx = 0
        query = ""
        while True:
            filtered = self._filter_menu_items(items, query)
            if filtered:
                idx = max(0, min(idx, len(filtered) - 1))
            else:
                idx = 0
            stdscr.clear()
            self._safe_addstr(stdscr, 0, 0, title)
            header_start = 1
            notice = self._current_notice()
            if notice:
                self._safe_addstr(stdscr, 1, 0, f"NOTIFY: {notice}")
                header_start = 2
            if title == "Main":
                dm_lines = self._dm_voice_front_lines(limit=4)
                for i, line in enumerate(dm_lines):
                    self._safe_addstr(stdscr, header_start + i, 0, line)
                header_start += len(dm_lines)
            if title.startswith("Connected:"):
                status = self._collect_voice_status(voice)
                self._curses_add_wrapped(stdscr, header_start + 0, 0, status)
                self._curses_add_wrapped(stdscr, header_start + 1, 0, self._last_dave_status)
                self._curses_add_wrapped(stdscr, header_start + 2, 0, self._collect_audio_status())
                base = header_start + 5
            else:
                base = header_start + 2
            self._safe_addstr(stdscr, base - 1, 0, f"Search: {query}")
            render_items = [items[i] for i in filtered] if filtered else ["(no results)"]
            for i, item in enumerate(render_items):
                prefix = "> " if i == idx else "  "
                self._safe_addstr(stdscr, base + i, 0, f"{prefix}{item}")

            if title.startswith("Connected:"):
                users_header_y = base + len(render_items) + 1
                self._safe_addstr(stdscr, users_header_y, 0, "Connected users (talk/mic/spk):")
                max_y, _ = stdscr.getmaxyx()
                rows_left = max(0, max_y - users_header_y - 1)
                user_lines = self._collect_connected_user_lines(voice, limit=max(0, rows_left - 5))
                end_row = users_header_y + 1
                for row, line in enumerate(user_lines, start=end_row):
                    self._safe_addstr(stdscr, row, 0, line)
                    end_row = row + 1
                self._safe_addstr(stdscr, end_row, 0, "Recent calls:")
                call_lines = self._recent_call_lines(limit=max(0, max_y - end_row - 1))
                for row, line in enumerate(call_lines, start=end_row + 1):
                    self._safe_addstr(stdscr, row, 0, line)
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                if filtered:
                    idx = (idx - 1) % len(filtered)
            elif ch in (curses.KEY_DOWN, ord("j")):
                if filtered:
                    idx = (idx + 1) % len(filtered)
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                query = query[:-1]
            elif allow_ctrl_k and ch == 11:  # Ctrl+K
                return -1
            elif ch in (ord("p"), ord("P")):
                if preview_callback and filtered:
                    preview_callback(filtered[idx])
            elif ch == curses.KEY_MOUSE:
                try:
                    _, _, my, _, bstate = curses.getmouse()
                except Exception:
                    continue
                if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED):
                    pick = my - base
                    if 0 <= pick < len(render_items) and filtered:
                        return filtered[pick]
            elif key_actions and ch in key_actions:
                if filtered:
                    key_actions[ch](filtered[idx])
            elif ch in (10, 13, curses.KEY_ENTER):
                if filtered:
                    return filtered[idx]
            elif 32 <= ch <= 126:
                query += chr(ch)

    def _curses_add_wrapped(self, stdscr, y: int, x: int, text: str) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if y >= max_y:
            return
        safe = text or ""
        while safe and y < max_y:
            chunk = safe[: max_x - x - 1] if max_x - x - 1 > 0 else ""
            self._safe_addstr(stdscr, y, x, chunk)
            safe = safe[len(chunk):]
            y += 1

    def _safe_addstr(self, stdscr, y: int, x: int, text: str) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if y < 0 or y >= max_y or x < 0 or x >= max_x:
            return
        raw = text or ""
        limit = max_x - x - 1
        if limit <= 0:
            return
        clipped = raw[:limit]
        try:
            stdscr.addstr(y, x, clipped)
        except curses.error:
            return

    def _curses_prompt(self, stdscr, label: str) -> str:
        curses.echo()
        stdscr.clear()
        self._safe_addstr(stdscr, 0, 0, f"{label}: ")
        stdscr.refresh()
        data = stdscr.getstr(0, len(label) + 2).decode("utf-8", errors="ignore").strip()
        curses.noecho()
        return data

    def _curses_message(self, stdscr, msg: str) -> None:
        stdscr.clear()
        self._safe_addstr(stdscr, 0, 0, msg)
        self._safe_addstr(stdscr, 2, 0, "Press any key...")
        stdscr.refresh()
        stdscr.getch()

    def _filter_menu_items(self, items: list[str], query: str) -> list[int]:
        if not query:
            return list(range(len(items)))
        q = query.lower()
        candidates: list[tuple[int, str]] = []
        for i, item in enumerate(items):
            v = item.lower()
            if self._fuzzy_in_order(v, q):
                candidates.append((i, v))
        candidates.sort(key=lambda x: (0 if x[1].startswith(q) else 1, x[1]))
        return [i for i, _ in candidates]

    def _fuzzy_in_order(self, haystack: str, needle: str) -> bool:
        it = iter(haystack)
        return all(ch in it for ch in needle)

    def _ctui_preview_dm(self, stdscr, dm: discord.DMChannel) -> None:
        recipient = dm.recipient
        if recipient is None:
            self._curses_message(stdscr, "No recipient/avatar for this DM.")
            return
        url = str(recipient.display_avatar.url)
        self._show_sixel_from_url(stdscr, url, f"DM Avatar: {recipient}")

    def _ctui_preview_guild(self, stdscr, guild: discord.Guild) -> None:
        if guild.icon is None:
            self._curses_message(stdscr, f"Guild '{guild.name}' has no icon.")
            return
        self._show_sixel_from_url(stdscr, str(guild.icon.url), f"Guild Icon: {guild.name}")

    def _ctui_preview_user(self, stdscr, user: discord.abc.User) -> None:
        self._show_sixel_from_url(stdscr, str(user.display_avatar.url), f"User Avatar: {user}")

    def _ctui_show_voice_members(self, stdscr, channel: discord.VoiceChannel) -> None:
        members = sorted(channel.members, key=lambda m: str(m).lower())
        if not members:
            self._curses_message(stdscr, f"No users in {channel.name}.")
            return
        labels = [f"{m} ({m.id})" for m in members]
        self._curses_menu(
            stdscr,
            f"Users in {channel.name} (search, p=avatar)",
            labels + ["Back"],
            preview_callback=lambda i: self._ctui_preview_user(stdscr, members[i]) if i < len(members) else None,
        )

    def _ctui_find_user_in_voice(self, stdscr, loop: asyncio.AbstractEventLoop):
        entries = []
        for guild in sorted(self.guilds, key=lambda g: g.name.lower()):
            for ch in sorted(guild.voice_channels, key=lambda c: c.position):
                for m in ch.members:
                    entries.append((m, guild, ch))
        if not entries:
            self._curses_message(stdscr, "No users currently in voice channels.")
            return None

        labels = [f"{m} ({m.id}) -> {g.name}/{ch.name}" for m, g, ch in entries]
        idx = self._curses_menu(
            stdscr,
            "Find user in voice (type name/id, p=avatar)",
            labels + ["Back"],
            preview_callback=lambda i: self._ctui_preview_user(stdscr, entries[i][0]) if i < len(entries) else None,
        )
        if idx >= len(entries):
            return None

        member, guild, channel = entries[idx]
        action = self._curses_menu(
            stdscr,
            f"{member} in {guild.name}/{channel.name}",
            ["Join this voice channel", "Back"],
        )
        if action == 0:
            try:
                voice, label = asyncio.run_coroutine_threadsafe(
                    self._open_guild_connection(guild.id, channel.id), loop
                ).result()
            except Exception as e:
                self._curses_message(stdscr, f"Error: {e}")
                return None
            return (voice, label)
        return None

    def _show_sixel_from_url(self, stdscr, url: str, title: str) -> None:
        if not self.args.sixel:
            self._curses_message(stdscr, "SIXEL preview is disabled. Run ctui with --sixel.")
            return
        if not self._supports_sixel():
            self._curses_message(stdscr, "Terminal SIXEL support not detected.")
            return
        if shutil.which("chafa") is None:
            self._curses_message(stdscr, "chafa not found. Install chafa for SIXEL image preview.")
            return
        tmp_path = None
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "image/*,*/*;q=0.8",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = resp.read()
            except Exception:
                retry_url = url.replace(".webp", ".png") if ".webp" in url else url
                req2 = urllib.request.Request(
                    retry_url,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "image/*,*/*;q=0.8",
                        "Referer": "https://discord.com/",
                    },
                )
                with urllib.request.urlopen(req2, timeout=10) as resp:
                    data = resp.read()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".img") as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            curses.endwin()
            print(f"\n{title}\n")
            subprocess.run(["chafa", "-f", "sixel", tmp_path], check=False)
            input("\nPress Enter to return to CTUI...")
        except Exception as e:
            self._curses_message(stdscr, f"SIXEL preview failed: {e}")
        finally:
            try:
                if tmp_path:
                    os.unlink(tmp_path)
            except Exception:
                pass

    def _supports_sixel(self) -> bool:
        term = (os.environ.get("TERM") or "").lower()
        return "sixel" in term or "xterm" in term or "mlterm" in term or "wezterm" in term

    def _curses_show_debug_log(self, stdscr) -> None:
        lines = self._debug_lines[-100:] if self._debug_lines else ["(no debug lines yet)"]
        pos = max(0, len(lines) - 1)
        while True:
            stdscr.clear()
            self._safe_addstr(stdscr, 0, 0, "Debug log (j/k scroll, q exit)")
            max_y, max_x = stdscr.getmaxyx()
            view_h = max(1, max_y - 2)
            start = max(0, min(pos - view_h + 1, len(lines) - view_h))
            for row, line in enumerate(lines[start : start + view_h], start=1):
                self._safe_addstr(stdscr, row, 0, line[: max_x - 1])
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (ord("q"), 27):
                return
            if ch in (curses.KEY_UP, ord("k")):
                pos = max(0, pos - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                pos = min(len(lines) - 1, pos + 1)

    def _ctui_show_missed_calls(self, stdscr) -> None:
        missed = self._missed_call_entries()
        if not missed:
            self._curses_message(stdscr, "No missed calls.")
            return
        labels = [f"[{e.get('ts')}] {e.get('peer')} (from {e.get('from')})" for e in missed]
        self._curses_menu(stdscr, "Missed calls (search enabled)", labels + ["Back"])

    def _ctui_show_call_log(self, stdscr) -> None:
        if not self._call_history:
            self._curses_message(stdscr, "No call history yet.")
            return
        labels = [
            f"[{e.get('ts')}] {e.get('status')} {e.get('peer')} (from {e.get('from')})"
            for e in self._call_history
        ]
        self._curses_menu(stdscr, "Call history (search enabled)", labels + ["Back"])

    def _ctui_show_video_probe(self, stdscr) -> None:
        lines = [
            "Video Probe Status",
            "",
            "Counters:",
            f"  voice_ws op=2 (READY): {self._video_probe_counts.get('voice_ws_op_2_ready', 0)}",
            f"  voice_ws op=11 (CLIENTS_CONNECT): {self._video_probe_counts.get('voice_ws_op_11_clients_connect', 0)}",
            f"  voice_ws op=12 (VIDEO): {self._video_probe_counts.get('voice_ws_op_12_video', 0)}",
            f"  voice_ws op=13 (CLIENT_DISCONNECT): {self._video_probe_counts.get('voice_ws_op_13_client_disconnect', 0)}",
            f"  voice_state video toggles: {self._video_probe_counts.get('voice_state_video_toggles', 0)}",
            f"  voice_state stream toggles: {self._video_probe_counts.get('voice_state_stream_toggles', 0)}",
            f"  self_video set calls: {self._video_probe_counts.get('self_video_set_calls', 0)}",
            f"  VIDEO opcode sent calls: {self._video_probe_counts.get('video_opcode_sent_calls', 0)}",
            f"  fake video RTP packets sent: {self._video_probe_counts.get('fake_video_packet_sent_calls', 0)}",
            "",
            "Recent probe events:",
        ]
        tail = self._video_probe_events[-20:] if self._video_probe_events else ["(none yet)"]
        lines.extend([f"  {x}" for x in tail])
        self._curses_message(stdscr, "\n".join(lines))

    def _collect_voice_status(self, voice: Optional[discord.VoiceClient]) -> str:
        if voice is None:
            return "Status: disconnected"
        conn = getattr(voice, "_connection", None)
        dave_proto = getattr(conn, "dave_protocol_version", None) if conn else None
        dave_encrypt = getattr(conn, "can_encrypt", None) if conn else None
        ws = getattr(conn, "ws", None) if conn else None
        voice_ver = getattr(ws, "voice_version", None) if ws else None
        rtc_ver = getattr(ws, "rtc_worker_version", None) if ws else None
        self_vs = getattr(conn, "self_voice_state", None) if conn else None
        self_video = bool(getattr(self_vs, "self_video", False))
        self_stream = bool(getattr(self_vs, "self_stream", False))
        return (
            "Status: "
            f"connected={voice.is_connected()} "
            f"playing={voice.is_playing()} "
            f"mode={self.args.mode} "
            f"dave_protocol={dave_proto} "
            f"dave_encrypt={dave_encrypt} "
            f"self_video={self_video} "
            f"self_stream={self_stream} "
            f"voice_backend={voice_ver} "
            f"rtc_worker={rtc_ver}"
        )

    def _collect_audio_status(self) -> str:
        mode = getattr(self.args, "mode", "connect")
        if mode == "file":
            return (
                "Audio: "
                f"mode=file file={getattr(self.args, 'file', '')} "
                f"loop={getattr(self.args, 'loop', False)} "
                f"sink={getattr(self.args, 'pulse_sink', None) or 'current'}"
            )
        if mode == "noise":
            return (
                "Audio: "
                f"mode=noise amp={getattr(self.args, 'noise_amp', 0.08)} "
                f"sink={getattr(self.args, 'pulse_sink', None) or 'current'}"
            )
        if mode == "mic":
            return (
                "Audio: "
                f"mode=mic source={getattr(self.args, 'pulse_source', None) or 'default'} "
                f"sink={getattr(self.args, 'pulse_sink', None) or 'current'}"
            )
        return (
            "Audio: "
            f"mode=connect sink={getattr(self.args, 'pulse_sink', None) or 'current'}"
        )

    def _collect_connected_user_lines(self, voice: Optional[discord.VoiceClient], *, limit: int) -> list[str]:
        if limit <= 0:
            return []
        if voice is None:
            return ["  (disconnected)"][:limit]
        channel = getattr(voice, "channel", None)
        members = getattr(channel, "members", None)
        if not members:
            return ["  (no members visible)"][:limit]

        lines: list[str] = []
        for member in sorted(members, key=lambda m: str(m).lower()):
            vs = getattr(member, "voice", None)
            mic_muted = bool(getattr(vs, "self_mute", False) or getattr(vs, "mute", False) or getattr(vs, "suppress", False))
            spk_deaf = bool(getattr(vs, "self_deaf", False) or getattr(vs, "deaf", False))
            cam_on = bool(getattr(vs, "self_video", False))
            stream_on = bool(getattr(vs, "self_stream", False))
            talking = self._is_user_talking(member.id)
            talk_mark = "*" if talking else "."
            mic_mark = "MUTED" if mic_muted else "open"
            spk_mark = "DEAF" if spk_deaf else "on"
            cam_mark = "on" if cam_on else "off"
            stream_mark = "on" if stream_on else "off"
            lines.append(
                f"  {talk_mark} mic={mic_mark:<5} spk={spk_mark:<4} cam={cam_mark:<3} scr={stream_mark:<3} {member} ({member.id})"
            )
            if len(lines) >= limit:
                break
        if not lines:
            lines.append("  (no members visible)")
        return lines[:limit]

    def _is_user_talking(self, user_id: int) -> bool:
        ts = self._speaking_activity.get(int(user_id))
        if ts is None:
            return False
        return (time.monotonic() - ts) <= 1.8

    def _dm_call_status(self, dm: discord.DMChannel) -> str:
        call = getattr(dm, "call", None)
        if call is None:
            return "idle"
        if bool(getattr(call, "unavailable", False)):
            return "unavailable"
        recipient = getattr(dm, "recipient", None)
        me = self.user
        try:
            voice_states = getattr(call, "voice_states", {}) or {}
        except Exception:
            voice_states = {}
        recipient_in = bool(recipient and int(recipient.id) in voice_states)
        me_in = bool(me and int(me.id) in voice_states) or bool(getattr(call, "connected", False))
        ringing = list(getattr(call, "ringing", []) or [])
        me_ringing = bool(me and any(int(u.id) == int(me.id) for u in ringing))
        rec_ringing = bool(recipient and any(int(u.id) == int(recipient.id) for u in ringing))
        if recipient_in and me_in:
            return "both-in-call"
        if recipient_in:
            return "friend-in-call"
        if me_in:
            return "you-in-call"
        if me_ringing:
            return "incoming-ring"
        if rec_ringing:
            return "outgoing-ring"
        if voice_states:
            return "call-active"
        return "call-open"

    def _dm_media_suffix(self, dm: discord.DMChannel) -> str:
        call = getattr(dm, "call", None)
        recipient = getattr(dm, "recipient", None)
        if call is None or recipient is None:
            return ""
        try:
            vs_map = getattr(call, "voice_states", {}) or {}
            vs = vs_map.get(int(recipient.id))
        except Exception:
            vs = None
        if vs is None:
            return ""
        cam_on = bool(getattr(vs, "self_video", False))
        stream_on = bool(getattr(vs, "self_stream", False))
        if not cam_on and not stream_on:
            return ""
        return f" cam={'on' if cam_on else 'off'} scr={'on' if stream_on else 'off'}"

    def _dm_voice_front_lines(self, limit: int = 4) -> list[str]:
        dms = [ch for ch in self.private_channels if isinstance(ch, discord.DMChannel)]
        active: list[tuple[str, str]] = []
        for dm in dms:
            status = self._dm_call_status(dm)
            if status not in ("idle", "unavailable"):
                who = (str(dm.recipient) if dm.recipient else f"dm:{dm.id}") + self._dm_media_suffix(dm)
                active.append((who, status))
        active.sort(key=lambda x: x[0].lower())
        if not active:
            return ["DM voice: no active DM calls"]
        out = [f"DM voice active: {len(active)}"]
        for who, status in active[: max(0, limit - 1)]:
            out.append(f"  {who}: {status}")
        return out

    def _dbg(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self._debug_lines.append(line)
        if len(self._debug_lines) > 500:
            self._debug_lines = self._debug_lines[-500:]
        self._append_log_line(line)

    def _append_log_line(self, line: str) -> None:
        if not self._log_file:
            return
        try:
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            return

    async def _connect_dm_by_user_id(self, user_id: int) -> None:
        voice, label = await self._open_dm_connection_by_id(user_id)
        await self._handle_connected_voice(voice, label)

    async def _open_dm_connection_by_id(self, user_id: int):
        user = self.get_user(user_id) or await self.fetch_user(user_id)
        if user is None:
            raise RuntimeError(f"User not found: {user_id}")
        dm = user.dm_channel or await user.create_dm()
        if dm is None:
            raise RuntimeError(f"Could not open DM channel with user {user_id}")
        print(f"Connecting to DM call with {user} (ring={self.args.ring})...")
        voice = await dm.connect(reconnect=True, ring=self.args.ring)
        if getattr(self.args, "exp_self_video", False):
            await self._apply_self_video_flag(voice, True)
        if getattr(self.args, "exp_video_opcode", False):
            await self._exp_send_video_opcode(voice)
        if getattr(self.args, "exp_self_stream", False):
            self._dbg("exp_self_stream requested but not implemented in library signaling path")
        self._attach_voice_ws_hook(voice)
        await self._after_connect_dave_checks(voice)
        self._remember_recent(
            {
                "kind": "dm",
                "user_id": user.id,
                "label": f"DM:{user} ({user.id})",
            }
        )
        return voice, f"DM:{user}"
        await self._handle_connected_voice(voice, label)

    async def _connect_dm_from_list(self) -> None:
        dm_channels = [ch for ch in self.private_channels if isinstance(ch, discord.DMChannel)]
        if not dm_channels:
            print("No DM channels found.")
            return
        dm_channels = sorted(dm_channels, key=lambda c: str(c.recipient).lower() if c.recipient else "")
        labels = []
        for ch in dm_channels:
            if ch.recipient:
                labels.append(f"{ch.recipient} (user_id={ch.recipient.id})")
            else:
                labels.append(f"Unknown recipient (channel_id={ch.id})")
        idx = self._select_menu("Select DM", labels + ["Back"])
        if idx == len(labels):
            return
        chosen = dm_channels[idx]
        recipient_label = str(chosen.recipient) if chosen.recipient else f"channel:{chosen.id}"
        print(f"Connecting to DM call with {recipient_label} (ring={self.args.ring})...")
        voice = await chosen.connect(reconnect=True, ring=self.args.ring)
        await self._after_connect_dave_checks(voice)
        await self._handle_connected_voice(voice, f"DM:{recipient_label}")

    async def _connect_guild_voice(self, guild_id: int, channel_id: int) -> None:
        voice, label = await self._open_guild_connection(guild_id, channel_id)
        await self._handle_connected_voice(voice, label)

    async def _open_guild_connection(self, guild_id: int, channel_id: int):
        guild = self.get_guild(guild_id)
        if guild is None:
            raise RuntimeError(f"Guild not found: {guild_id}")
        channel = guild.get_channel(channel_id)
        if channel is None or not isinstance(channel, discord.VoiceChannel):
            raise RuntimeError(f"Voice channel not found: {channel_id}")
        print(f"Connecting to {guild.name}/{channel.name}...")
        voice = await channel.connect(reconnect=True, self_deaf=False, self_mute=False)
        if getattr(self.args, "exp_self_video", False):
            await self._apply_self_video_flag(voice, True)
        if getattr(self.args, "exp_video_opcode", False):
            await self._exp_send_video_opcode(voice)
        if getattr(self.args, "exp_self_stream", False):
            self._dbg("exp_self_stream requested but not implemented in library signaling path")
        self._attach_voice_ws_hook(voice)
        await self._after_connect_dave_checks(voice)
        self._remember_recent(
            {
                "kind": "guild",
                "guild_id": guild.id,
                "channel_id": channel.id,
                "label": f"{guild.name}/{channel.name} ({guild.id}/{channel.id})",
            }
        )
        return voice, f"{guild.name}/{channel.name}"

    def _remember_recent(self, entry: dict) -> None:
        entry = dict(entry)
        entry["ts"] = datetime.now().isoformat(timespec="seconds")
        self._recent_targets = [
            e
            for e in self._recent_targets
            if not (
                e.get("kind") == entry.get("kind")
                and e.get("user_id") == entry.get("user_id")
                and e.get("guild_id") == entry.get("guild_id")
                and e.get("channel_id") == entry.get("channel_id")
            )
        ]
        self._recent_targets.insert(0, entry)
        self._recent_targets = self._recent_targets[:30]
        self._dbg(f"recent add: {entry.get('label')}")

    def _attach_voice_ws_hook(self, voice: discord.VoiceClient) -> None:
        conn = getattr(voice, "_connection", None)
        ws = getattr(voice, "ws", None)
        if conn is not None:
            conn.hook = self._voice_ws_hook
        if ws is not None:
            ws._hook = self._voice_ws_hook  # type: ignore[attr-defined]

    async def _voice_ws_hook(self, _ws, msg) -> None:
        try:
            op = msg.get("op")
            data = msg.get("d") or {}
            if op == 2:
                self._video_probe_counts["voice_ws_op_2_ready"] += 1
            elif op == 11:
                self._video_probe_counts["voice_ws_op_11_clients_connect"] += 1
            elif op == 12:
                self._video_probe_counts["voice_ws_op_12_video"] += 1
            elif op == 13:
                self._video_probe_counts["voice_ws_op_13_client_disconnect"] += 1
            if op in (2, 11, 12, 13):
                self._dbg(f"voice ws op={op} data={data}")
                self._video_probe_event(f"voice_ws op={op} data={data}")
            if op == 5:  # SPEAKING
                raw_uid = data.get("user_id")
                if raw_uid is None:
                    return
                uid = int(raw_uid)
                speaking_val = int(data.get("speaking", 0))
                if speaking_val != 0:
                    self._speaking_activity[uid] = time.monotonic()
                else:
                    self._speaking_activity.pop(uid, None)
            elif op == 13:  # CLIENT_DISCONNECT
                raw_uid = data.get("user_id")
                if raw_uid is not None:
                    self._speaking_activity.pop(int(raw_uid), None)
        except Exception as e:
            self._dbg(f"voice ws hook error: {e!r}")

    def _ctui_connect_recent(self, stdscr, loop: asyncio.AbstractEventLoop):
        if not self._recent_targets:
            self._curses_message(stdscr, "No recent targets yet.")
            return None
        labels = [f"[{e.get('ts')}] {e.get('label')}" for e in self._recent_targets]
        idx = self._curses_menu(stdscr, "Recent targets (type to search)", labels + ["Back"])
        if idx >= len(self._recent_targets):
            return None
        entry = self._recent_targets[idx]
        try:
            if entry.get("kind") == "dm":
                return asyncio.run_coroutine_threadsafe(
                    self._open_dm_connection_by_id(int(entry["user_id"])), loop
                ).result()
            if entry.get("kind") == "guild":
                return asyncio.run_coroutine_threadsafe(
                    self._open_guild_connection(int(entry["guild_id"]), int(entry["channel_id"])), loop
                ).result()
        except Exception as e:
            self._curses_message(stdscr, f"Error: {e}")
            return None
        self._curses_message(stdscr, "Unknown recent target type.")
        return None

    def _ctui_quick_jump(self, stdscr, loop: asyncio.AbstractEventLoop):
        targets: list[dict] = []
        for e in self._recent_targets:
            targets.append(dict(e, recent=True))

        for guild in sorted(self.guilds, key=lambda g: g.name.lower()):
            for ch in sorted(guild.voice_channels, key=lambda c: c.position):
                targets.append(
                    {
                        "kind": "guild",
                        "guild_id": guild.id,
                        "channel_id": ch.id,
                        "label": f"{guild.name}/{ch.name} ({guild.id}/{ch.id}) members={len(ch.members)}",
                        "recent": False,
                    }
                )

        for dm in sorted(
            [ch for ch in self.private_channels if isinstance(ch, discord.DMChannel)],
            key=lambda c: str(c.recipient).lower() if c.recipient else "",
        ):
            if dm.recipient is None:
                continue
            targets.append(
                {
                    "kind": "dm",
                    "user_id": dm.recipient.id,
                    "label": f"DM:{dm.recipient} ({dm.recipient.id})",
                    "recent": False,
                }
            )

        deduped: list[dict] = []
        seen: set[tuple] = set()
        for t in targets:
            key = (t.get("kind"), t.get("user_id"), t.get("guild_id"), t.get("channel_id"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(t)
        targets = deduped
        if not targets:
            self._curses_message(stdscr, "No quick-jump targets available.")
            return None

        labels = [f"{'[Recent] ' if t.get('recent') else ''}{t.get('label')}" for t in targets]
        idx = self._curses_menu(stdscr, "Quick Jump (Ctrl+K)", labels + ["Back"])
        if idx >= len(targets):
            return None
        t = targets[idx]
        try:
            if t.get("kind") == "dm":
                return asyncio.run_coroutine_threadsafe(
                    self._open_dm_connection_by_id(int(t["user_id"])), loop
                ).result()
            if t.get("kind") == "guild":
                return asyncio.run_coroutine_threadsafe(
                    self._open_guild_connection(int(t["guild_id"]), int(t["channel_id"])), loop
                ).result()
        except Exception as e:
            self._curses_message(stdscr, f"Error: {e}")
            return None
        self._curses_message(stdscr, "Unknown quick-jump target type.")
        return None

    def _ctui_quick_dm_call(self, stdscr, loop: asyncio.AbstractEventLoop):
        dm_channels = [ch for ch in self.private_channels if isinstance(ch, discord.DMChannel)]
        if not dm_channels:
            self._curses_message(stdscr, "No DM channels found.")
            return None

        prio = {
            "incoming-ring": 0,
            "friend-in-call": 1,
            "both-in-call": 2,
            "call-active": 3,
            "outgoing-ring": 4,
            "you-in-call": 5,
            "call-open": 6,
            "idle": 9,
            "unavailable": 10,
        }

        rows = []
        for dm in dm_channels:
            status = self._dm_call_status(dm)
            if status in ("idle", "unavailable"):
                continue
            who = str(dm.recipient) if dm.recipient else f"unknown ({dm.id})"
            rows.append((prio.get(status, 99), who.lower(), dm, status))

        if not rows:
            self._curses_message(stdscr, "No active/incoming DM voice calls right now.")
            return None

        rows.sort(key=lambda x: (x[0], x[1]))
        labels = [f"{who} [{status}]" for _, who, _, status in rows]
        idx = self._curses_menu(stdscr, "Join/Accept DM Call (incoming first)", labels + ["Back"])
        if idx >= len(rows):
            return None
        dm = rows[idx][2]
        uid = dm.recipient.id if dm.recipient else None
        if uid is None:
            self._curses_message(stdscr, "Selected DM has no recipient id.")
            return None
        try:
            return asyncio.run_coroutine_threadsafe(self._open_dm_connection_by_id(int(uid)), loop).result()
        except Exception as e:
            self._curses_message(stdscr, f"Error: {e}")
            return None

    async def _connect_guild_from_list(self) -> None:
        guilds = sorted(self.guilds, key=lambda g: g.name.lower())
        if not guilds:
            print("No guilds found.")
            return
        g_idx = self._select_menu("Select Guild", [f"{g.name} ({g.id})" for g in guilds] + ["Back"])
        if g_idx == len(guilds):
            return
        guild = guilds[g_idx]
        channels = sorted(guild.voice_channels, key=lambda c: c.position)
        if not channels:
            print("Selected guild has no voice channels.")
            return
        c_idx = self._select_menu(
            "Select Voice Channel",
            [f"{ch.name} ({ch.id}) members={len(ch.members)}" for ch in channels] + ["Back"],
        )
        if c_idx == len(channels):
            return
        channel = channels[c_idx]
        print(f"Connecting to {guild.name}/{channel.name}...")
        voice = await channel.connect(reconnect=True, self_deaf=False, self_mute=False)
        await self._after_connect_dave_checks(voice)
        await self._handle_connected_voice(voice, f"{guild.name}/{channel.name}")

    async def _after_connect_dave_checks(self, voice: discord.VoiceClient) -> None:
        if self.args.dave_debug or self.args.require_dave:
            await self._wait_for_dave_status(voice, timeout=self.args.dave_wait_timeout)
        if self.args.require_dave:
            self._enforce_dave_or_raise(voice)

    async def _disconnect_voice(self, voice: discord.VoiceClient) -> None:
        if self._video_loop_task is not None:
            self._video_loop_task.cancel()
            self._video_loop_task = None
        if self._fake_video_loop_task is not None:
            self._fake_video_loop_task.cancel()
            self._fake_video_loop_task = None
        if voice.is_playing():
            voice.stop()
        await voice.disconnect(force=True)

    async def _apply_self_video_flag(self, voice: discord.VoiceClient, enabled: bool) -> None:
        ws = getattr(self, "ws", None)
        if ws is None:
            raise RuntimeError("Gateway websocket is not available for self_video update.")
        channel = getattr(voice, "channel", None)
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            raise RuntimeError("Voice channel id not available.")
        guild_id = getattr(getattr(channel, "guild", None), "id", None)
        conn = getattr(voice, "_connection", None)
        self_vs = getattr(conn, "self_voice_state", None) if conn else None
        self_mute = bool(getattr(self_vs, "self_mute", False))
        self_deaf = bool(getattr(self_vs, "self_deaf", False))
        await ws.voice_state(
            guild_id=guild_id,
            channel_id=channel_id,
            self_mute=self_mute,
            self_deaf=self_deaf,
            self_video=bool(enabled),
        )
        self.args.exp_self_video = bool(enabled)
        self._video_probe_counts["self_video_set_calls"] += 1
        self._dbg(f"exp self_video set to {enabled} on channel={channel_id}")
        self._video_probe_event(f"set self_video={enabled} channel={channel_id}")

    async def _exp_send_video_opcode(self, voice: discord.VoiceClient) -> None:
        ws = getattr(voice, "ws", None)
        if ws is None:
            raise RuntimeError("Voice websocket unavailable for VIDEO opcode.")
        await ws.client_connect()
        self._video_probe_counts["video_opcode_sent_calls"] += 1
        self._dbg("exp VIDEO opcode sent via voice websocket")
        self._video_probe_event("sent VOICE VIDEO opcode")

    async def _toggle_video_opcode_loop(self, voice: discord.VoiceClient) -> bool:
        if self._video_loop_task is not None:
            self._video_loop_task.cancel()
            self._video_loop_task = None
            self._video_probe_event("stopped VIDEO opcode loop")
            return False

        interval = max(0.5, float(getattr(self.args, "exp_video_loop_interval", 2.0)))

        async def _loop():
            try:
                while True:
                    await self._exp_send_video_opcode(voice)
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                pass

        self._video_loop_task = asyncio.create_task(_loop())
        self._video_probe_event(f"started VIDEO opcode loop interval={interval}s")
        return True

    async def _exp_send_fake_video_packet(self, voice: discord.VoiceClient) -> None:
        conn = getattr(voice, "_connection", None)
        if conn is None:
            raise RuntimeError("No voice connection internals available.")

        if self._video_seq is None:
            self._video_seq = int(getattr(voice, "sequence", 0)) & 0xFFFF
        if self._video_ts is None:
            self._video_ts = int(getattr(voice, "timestamp", 0)) & 0xFFFFFFFF
        if self._video_ssrc is None:
            self._video_ssrc = int(getattr(voice, "ssrc", 0)) & 0xFFFFFFFF

        pt = max(0, min(127, int(getattr(self.args, "exp_fake_video_pt", 96))))
        payload_size = max(12, int(getattr(self.args, "exp_fake_video_payload", 900)))
        second = 0x80 | (pt & 0x7F)

        header = bytearray(12)
        header[0] = 0x80
        header[1] = second
        struct.pack_into(">H", header, 2, self._video_seq)
        struct.pack_into(">I", header, 4, self._video_ts)
        struct.pack_into(">I", header, 8, self._video_ssrc)

        payload = b"\x00\x00\x00\x01\x65" + bytes(max(0, payload_size - 5))
        encrypt_packet = getattr(voice, "_encrypt_" + voice.mode)
        packet = encrypt_packet(header, payload)
        conn.send_packet(packet)

        self._video_seq = (self._video_seq + 1) & 0xFFFF
        self._video_ts = (self._video_ts + 3000) & 0xFFFFFFFF
        self._video_probe_counts["fake_video_packet_sent_calls"] += 1
        self._dbg(f"exp fake video RTP sent pt={pt} payload={payload_size}")
        self._video_probe_event(f"sent fake video RTP pt={pt} payload={payload_size}")

    async def _toggle_fake_video_loop(self, voice: discord.VoiceClient) -> bool:
        if self._fake_video_loop_task is not None:
            self._fake_video_loop_task.cancel()
            self._fake_video_loop_task = None
            self._video_probe_event("stopped fake video RTP loop")
            return False

        interval = max(0.05, float(getattr(self.args, "exp_fake_video_interval", 0.2)))

        async def _loop():
            try:
                while True:
                    await self._exp_send_fake_video_packet(voice)
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                pass

        self._fake_video_loop_task = asyncio.create_task(_loop())
        self._video_probe_event(f"started fake video RTP loop interval={interval}s")
        return True

    async def _hold_connection(self, voice: discord.VoiceClient, label: str) -> None:
        print(f"Connected to {label}. Press Ctrl+C to disconnect.")
        try:
            while True:
                await asyncio.sleep(1.0)
        except KeyboardInterrupt:
            print("Disconnecting...")
        finally:
            await voice.disconnect(force=True)

    async def _handle_connected_voice(self, voice: discord.VoiceClient, label: str) -> None:
        if self.args.command == "tui":
            await self._session_control_loop(voice, label)
        elif self.args.mode in ("file", "noise", "mic"):
            await self._play_to_voice_client(voice, label)
        else:
            await self._hold_connection(voice, label)

    async def _session_control_loop(self, voice: discord.VoiceClient, label: str) -> None:
        print(f"Connected to {label}.")
        await self._restart_playback(voice, label)
        self._print_session_help()

        while True:
            try:
                raw = await asyncio.to_thread(input, "session> ")
            except (EOFError, KeyboardInterrupt):
                raw = "leave"
            cmd = raw.strip()
            if not cmd:
                continue

            parts = cmd.split()
            head = parts[0].lower()

            if head in ("help", "?"):
                self._print_session_help()
            elif head == "status":
                self._print_session_status()
            elif head == "dave":
                await self._wait_for_dave_status(voice, timeout=max(0.5, self.args.dave_wait_timeout))
            elif head == "mode":
                if len(parts) < 2 or parts[1] not in ("file", "noise", "mic", "connect"):
                    print("Usage: mode <file|noise|mic|connect>")
                    continue
                self.args.mode = parts[1]
                await self._restart_playback(voice, label)
            elif head == "file":
                if len(parts) < 2:
                    print("Usage: file <path>")
                    continue
                self.args.file = " ".join(parts[1:])
                self.args.mode = "file"
                await self._restart_playback(voice, label)
            elif head == "loop":
                if len(parts) < 2 or parts[1] not in ("on", "off"):
                    print("Usage: loop <on|off>")
                    continue
                self.args.loop = parts[1] == "on"
                if self.args.mode == "file":
                    await self._restart_playback(voice, label)
            elif head == "amp":
                if len(parts) < 2:
                    print("Usage: amp <0..1>")
                    continue
                try:
                    self.args.noise_amp = float(parts[1])
                except ValueError:
                    print("Invalid amplitude.")
                    continue
                if self.args.mode == "noise":
                    await self._restart_playback(voice, label)
            elif head == "sources":
                self._print_pulse_devices("sources")
            elif head == "sinks":
                self._print_pulse_devices("sinks")
            elif head == "source":
                if len(parts) < 2:
                    print("Usage: source <pulse-source-name>")
                    continue
                self.args.pulse_source = " ".join(parts[1:])
                self._set_default_pulse_device("source", self.args.pulse_source)
                print(f"Pulse default source set to: {self.args.pulse_source}")
                if self.args.mode == "mic":
                    await self._restart_playback(voice, label)
            elif head == "sink":
                if len(parts) < 2:
                    print("Usage: sink <pulse-sink-name>")
                    continue
                self.args.pulse_sink = " ".join(parts[1:])
                self._set_default_pulse_device("sink", self.args.pulse_sink)
                print(f"Pulse default sink set to: {self.args.pulse_sink}")
            elif head == "restart":
                await self._restart_playback(voice, label)
            elif head == "switch":
                print("Disconnecting and returning to target selection...")
                if voice.is_playing():
                    voice.stop()
                await voice.disconnect(force=True)
                return
            elif head == "leave":
                print("Disconnecting...")
                if voice.is_playing():
                    voice.stop()
                await voice.disconnect(force=True)
                return
            elif head in ("quit", "exit"):
                print("Disconnecting and exiting...")
                if voice.is_playing():
                    voice.stop()
                await voice.disconnect(force=True)
                raise SystemExit(0)
            else:
                print("Unknown command. Type 'help'.")

    async def _restart_playback(self, voice: discord.VoiceClient, label: str) -> None:
        if voice.is_playing():
            voice.stop()
            await asyncio.sleep(0.05)

        if self.args.mode == "connect":
            print(f"Connected to {label} (no audio playback).")
            self._dbg(f"connect-only mode in {label}")
            return

        ffmpeg = self.args.ffmpeg_path or shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("ffmpeg not found. Install ffmpeg or pass --ffmpeg-path")
        source = self._make_audio_source(ffmpeg)
        voice.play(source, after=lambda err: print(f"Playback error: {err}") if err else None)
        if self.args.mode == "file":
            print(f"Now playing file (loop={self.args.loop}): {self.args.file}")
            self._dbg(f"playing file: {self.args.file} loop={self.args.loop}")
        elif self.args.mode == "noise":
            print(f"Now playing noise (amp={self.args.noise_amp})")
            self._dbg(f"playing noise: amp={self.args.noise_amp}")
        elif self.args.mode == "mic":
            print(f"Now streaming microphone source: {self.args.pulse_source or 'default'}")
            self._dbg(f"streaming mic: source={self.args.pulse_source or 'default'}")

    def _print_session_help(self) -> None:
        print("Commands:")
        print("  help                 show this help")
        print("  status               show current playback/device settings")
        print("  dave                 print current DAVE status")
        print("  mode <file|noise|mic|connect>")
        print("  file <path>          set file path and switch to file mode")
        print("  loop <on|off>        toggle file looping")
        print("  amp <0..1>           set noise amplitude")
        print("  sources              list PulseAudio sources")
        print("  sinks                list PulseAudio sinks")
        print("  source <name>        set PulseAudio source (for mic mode)")
        print("  sink <name>          set PulseAudio sink")
        print("  restart              restart current playback mode")
        print("  switch               disconnect and select another target")
        print("  leave                disconnect and return to menu")
        print("  quit                 disconnect and exit app")

    def _print_session_status(self) -> None:
        print(
            "Status:",
            f"mode={self.args.mode}",
            f"file={self.args.file}",
            f"loop={self.args.loop}",
            f"noise_amp={self.args.noise_amp}",
            f"pulse_source={self.args.pulse_source or 'default'}",
            f"pulse_sink={self.args.pulse_sink or '(unchanged)'}",
        )

    def _select_menu(self, title: str, items: list[str]) -> int:
        print(f"\n{title}:")
        for i, item in enumerate(items, start=1):
            print(f"  {i}. {item}")
        while True:
            raw = input("> ").strip()
            if raw.isdigit():
                idx = int(raw) - 1
                if 0 <= idx < len(items):
                    return idx
            print("Invalid selection. Enter a number from the list.")

    def _prompt_int(self, label: str) -> int:
        while True:
            raw = input(f"{label}: ").strip()
            if raw.isdigit():
                return int(raw)
            print("Invalid number.")

    def _tui_configure_audio(self) -> None:
        print("\nAudio setup:")
        mode_idx = self._select_menu("Select audio mode", ["File", "Noise", "Microphone", "Connect only"])
        if mode_idx == 0:
            self.args.mode = "file"
            path = input(f"File path [{self.args.file}]: ").strip()
            if path:
                self.args.file = path
            loop_idx = self._select_menu("Loop file playback?", ["Yes", "No"])
            self.args.loop = loop_idx == 0
        elif mode_idx == 1:
            self.args.mode = "noise"
            amp = input(f"Noise amplitude 0..1 [{self.args.noise_amp}]: ").strip()
            if amp:
                try:
                    self.args.noise_amp = float(amp)
                except ValueError:
                    print("Invalid amplitude, keeping default.")
            self.args.loop = False
        elif mode_idx == 2:
            self.args.mode = "mic"
            self.args.loop = False
            self.args.pulse_source = self._select_pulse_device("sources", "input source", self.args.pulse_source)
        else:
            self.args.mode = "connect"
            self.args.loop = False

        sink_idx = self._select_menu("Set PulseAudio output sink?", ["Keep current", "Choose sink"])
        if sink_idx == 1:
            sink = self._select_pulse_device("sinks", "output sink", self.args.pulse_sink)
            if sink:
                self.args.pulse_sink = sink
                self._set_default_pulse_device("sink", sink)
                print(f"Pulse default sink set to: {sink}")

        if self.args.mode == "mic" and self.args.pulse_source:
            self._set_default_pulse_device("source", self.args.pulse_source)
            print(f"Pulse default source set to: {self.args.pulse_source}")

    def _pulse_device_entries(self, kind: str) -> list[tuple[str, str]]:
        if kind not in ("sources", "sinks"):
            return []
        try:
            out = subprocess.check_output(["pactl", "list", kind], text=True, stderr=subprocess.DEVNULL)
        except Exception:
            return self._pulse_device_entries_short(kind)
        entries: list[tuple[str, str]] = []
        current_name: Optional[str] = None
        current_desc: Optional[str] = None
        for raw in out.splitlines():
            line = raw.strip()
            if line.startswith(("Source #", "Sink #")):
                if current_name:
                    entries.append((current_name, current_desc or current_name))
                current_name = None
                current_desc = None
                continue
            if line.startswith("Name:"):
                current_name = line.split(":", 1)[1].strip()
                continue
            if line.startswith("Description:"):
                current_desc = line.split(":", 1)[1].strip()
                continue
        if current_name:
            entries.append((current_name, current_desc or current_name))

        # De-duplicate by device name while preserving first occurrence.
        seen = set()
        deduped: list[tuple[str, str]] = []
        for name, desc in entries:
            if name in seen:
                continue
            seen.add(name)
            deduped.append((name, desc))
        if deduped:
            return deduped
        return self._pulse_device_entries_short(kind)

    def _pulse_device_entries_short(self, kind: str) -> list[tuple[str, str]]:
        try:
            out = subprocess.check_output(["pactl", "list", "short", kind], text=True, stderr=subprocess.DEVNULL)
        except Exception:
            return []
        entries: list[tuple[str, str]] = []
        seen = set()
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                name = parts[1].strip()
                if name in seen:
                    continue
                seen.add(name)
                entries.append((name, name))
        return entries

    def _pulse_devices(self, kind: str) -> list[str]:
        return [name for name, _ in self._pulse_device_entries(kind)]

    def _select_pulse_device(self, kind: str, label: str, current: Optional[str]) -> Optional[str]:
        entries = self._pulse_device_entries(kind)
        if not entries:
            manual = input(f"No PulseAudio {label}s found. Enter {label} name manually (blank to skip): ").strip()
            return manual or current
        opts = []
        for name, desc in entries:
            item = f"{desc} [{name}]"
            if current == name:
                item += " (current)"
            opts.append(item)
        idx = self._select_menu(f"Select {label}", opts + ["Manual input", "Keep current"])
        if idx == len(opts):
            manual = input(f"Enter {label} name: ").strip()
            return manual or current
        if idx == len(opts) + 1:
            return current
        return entries[idx][0]

    def _set_default_pulse_device(self, kind: str, name: str) -> None:
        cmd = ["pactl", f"set-default-{kind}", name]
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _print_pulse_devices(self, kind: str) -> None:
        entries = self._pulse_device_entries(kind)
        if not entries:
            print(f"No PulseAudio {kind} found.")
            return
        print(f"PulseAudio {kind}:")
        for name, desc in entries:
            print(f"  - {desc} [{name}]")

    async def _wait_for_dave_status(self, voice: discord.VoiceClient, *, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            conn = getattr(voice, "_connection", None)
            can_encrypt = bool(getattr(conn, "can_encrypt", False)) if conn else False
            now = asyncio.get_running_loop().time()
            if can_encrypt or now >= deadline:
                break
            await asyncio.sleep(0.5)

        conn = getattr(voice, "_connection", None)
        if conn is None:
            self._last_dave_status = "DAVE: no voice connection internals available"
            print(self._last_dave_status)
            self._dbg(self._last_dave_status)
            return
        max_proto = getattr(conn, "max_dave_protocol_version", None)
        active_proto = getattr(conn, "dave_protocol_version", None)
        can_encrypt = getattr(conn, "can_encrypt", None)
        session = getattr(conn, "dave_session", None)
        privacy_code = getattr(voice, "voice_privacy_code", None)
        self._last_dave_status = (
            "DAVE status: "
            f"max_protocol={max_proto} "
            f"active_protocol={active_proto} "
            f"can_encrypt={can_encrypt} "
            f"session={'yes' if session else 'no'} "
            f"privacy_code={privacy_code}"
        )
        print(self._last_dave_status)
        self._dbg(self._last_dave_status)

    def _enforce_dave_or_raise(self, voice: discord.VoiceClient) -> None:
        conn = getattr(voice, "_connection", None)
        if conn is None:
            raise RuntimeError("DAVE required but voice internals are unavailable")
        active_proto = getattr(conn, "dave_protocol_version", 0) or 0
        can_encrypt = bool(getattr(conn, "can_encrypt", False))
        if active_proto <= 0 or not can_encrypt:
            raise RuntimeError(
                f"DAVE required but not active (active_protocol={active_proto}, can_encrypt={can_encrypt})"
            )

    def _ffmpeg_demuxers(self, ffmpeg: str) -> set[str]:
        cached = self._ffmpeg_demuxers_cache.get(ffmpeg)
        if cached is not None:
            return cached
        demuxers: set[str] = set()
        try:
            proc = subprocess.run([ffmpeg, "-hide_banner", "-demuxers"], capture_output=True, text=True, check=False)
            out = f"{proc.stdout}\n{proc.stderr}"
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0].startswith("D"):
                    demuxers.add(parts[1])
        except Exception:
            self._ffmpeg_demuxers_cache[ffmpeg] = demuxers
            return demuxers
        self._ffmpeg_demuxers_cache[ffmpeg] = demuxers
        return demuxers

    def _resolve_mic_input_format(self, ffmpeg: str) -> str:
        req = (getattr(self.args, "mic_input_format", "auto") or "auto").strip().lower()
        allowed = {"auto", "pulse", "pipewire", "alsa"}
        if req not in allowed:
            raise RuntimeError(f"Invalid --mic-input-format '{req}'. Choose one of: auto,pulse,pipewire,alsa")
        demuxers = self._ffmpeg_demuxers(ffmpeg)
        if req != "auto":
            if req not in demuxers:
                raise RuntimeError(
                    f"ffmpeg does not support '{req}' input format on this system. "
                    f"Available demuxers include: {', '.join(sorted(demuxers)) or '(unknown)'}"
                )
            return req
        for candidate in ("pulse", "pipewire", "alsa"):
            if candidate in demuxers:
                return candidate
        raise RuntimeError(
            "ffmpeg has no supported mic input demuxer (pulse/pipewire/alsa). "
            "Install an ffmpeg build with PulseAudio or PipeWire support."
        )

    def _make_audio_source(self, ffmpeg: str) -> discord.AudioSource:
        if self.args.mode == "file":
            if not os.path.exists(self.args.file):
                raise RuntimeError(f"Audio file not found: {self.args.file}")
            return discord.FFmpegPCMAudio(
                source=self.args.file,
                executable=ffmpeg,
                before_options="-stream_loop -1 -re" if self.args.loop else None,
                options="-vn",
            )
        if self.args.mode == "mic":
            source_name = self.args.pulse_source or "default"
            fmt = self._resolve_mic_input_format(ffmpeg)
            if fmt == "alsa" and source_name.startswith("alsa_input."):
                raise RuntimeError(
                    "Selected source looks like a PulseAudio/PipeWire source name, but ffmpeg is using ALSA input. "
                    "Set --mic-input-format pulse (with ffmpeg pulse support) or pass an ALSA device (e.g. hw:1,0)."
                )
            return discord.FFmpegPCMAudio(
                source=source_name,
                executable=ffmpeg,
                before_options=f"-f {fmt} -thread_queue_size 1024",
                options="-vn",
            )

        return discord.FFmpegPCMAudio(
            source=f"anoisesrc=color=white:amplitude={self.args.noise_amp}:sample_rate=48000",
            executable=ffmpeg,
            before_options="-f lavfi -re",
            options="-vn",
        )

    async def _play_to_voice_client(self, voice: discord.VoiceClient, label: str) -> None:
        ffmpeg = self.args.ffmpeg_path or shutil.which("ffmpeg")
        if ffmpeg is None:
            await voice.disconnect(force=True)
            raise RuntimeError("ffmpeg not found. Install ffmpeg or pass --ffmpeg-path")

        source = self._make_audio_source(ffmpeg)
        if self.args.mode == "file":
            print(f"Playing file to {label} (loop={self.args.loop}): {self.args.file}")
        elif self.args.mode == "mic":
            print(f"Streaming microphone to {label} (pulse_source={self.args.pulse_source or 'default'})")
        else:
            print(f"Playing generated noise to {label} (amp={self.args.noise_amp})")

        finished = asyncio.Event()

        def after_play(err: Optional[Exception]):
            if err:
                print(f"Playback error: {err}")
            finished.set()

        voice.play(source, after=after_play)

        try:
            if self.args.loop and self.args.mode == "file":
                while True:
                    await finished.wait()
                    finished.clear()
                    source = self._make_audio_source(ffmpeg)
                    voice.play(source, after=after_play)
            else:
                await finished.wait()
        except KeyboardInterrupt:
            print("Interrupted")
        finally:
            if voice.is_playing():
                voice.stop()
            await voice.disconnect(force=True)


async def _run(args: argparse.Namespace) -> None:
    client = VoiceSelfClient(args)
    token = args.token or args.config_token or os.getenv("DISCORD_USER_TOKEN")
    if not token:
        raise RuntimeError("Token is required (--token, config file, or DISCORD_USER_TOKEN)")

    runner = asyncio.create_task(client.start(token))
    await client._done.wait()
    await client.close()
    await asyncio.sleep(0.2)
    if not runner.done():
        runner.cancel()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="shitcord-voice+video")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help=f"Path to local config json (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--token", help="Discord user token (or set DISCORD_USER_TOKEN)")
    parser.add_argument("--log-file", default=None, help="Append debug/errors to this file")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List guilds and voice channels")

    play = sub.add_parser("play", help="Join voice channel and play audio")
    play.add_argument("--guild-id", type=int, required=False)
    play.add_argument("--channel-id", type=int, required=False)
    play.add_argument("--mode", choices=["file", "noise"], default="file")
    play.add_argument("--file", default="rickroll.ogg", help="Audio file path when --mode file")
    play.add_argument("--loop", action="store_true", help="Loop file playback")
    play.add_argument("--noise-amp", type=float, default=0.08, help="Noise amplitude (0.0-1.0)")
    play.add_argument("--ffmpeg-path", default=None, help="Path to ffmpeg binary")
    play.add_argument("--pulse-source", default=None, help="PulseAudio source name (used with --mode mic)")
    play.add_argument("--pulse-sink", default=None, help="PulseAudio sink name to set as default")
    play.add_argument("--dave-debug", action="store_true", help="Print DAVE negotiation status after connect")
    play.add_argument("--require-dave", action="store_true", help="Abort if DAVE is not active/encrypting")
    play.add_argument("--dave-wait-timeout", type=float, default=10.0, help="Seconds to wait for DAVE encryption readiness")
    play.add_argument("--exp-self-video", action="store_true", help="Experimental: request self camera flag after connect")
    play.add_argument("--exp-self-stream", action="store_true", help="Experimental placeholder for self stream flag")
    play.add_argument("--exp-video-opcode", action="store_true", help="Experimental: send VOICE VIDEO opcode after connect")
    play.add_argument("--exp-video-loop-interval", type=float, default=2.0, help="Seconds between VIDEO opcode loop sends")
    play.add_argument("--exp-fake-video-interval", type=float, default=0.2, help="Seconds between fake video RTP sends in loop")
    play.add_argument("--exp-fake-video-pt", type=int, default=96, help="RTP payload type for fake video packets")
    play.add_argument("--exp-fake-video-payload", type=int, default=900, help="Fake video RTP payload size in bytes")

    dm_play = sub.add_parser("dm-play", help="Start/join DM call and play audio")
    dm_play.add_argument("--user-id", type=int, required=False, help="Target user id for DM call")
    dm_play.add_argument("--ring", action="store_true", help="Ring user when starting DM call")
    dm_play.add_argument("--mode", choices=["file", "noise", "mic"], default="file")
    dm_play.add_argument("--file", default="rickroll.ogg", help="Audio file path when --mode file")
    dm_play.add_argument("--loop", action="store_true", help="Loop file playback")
    dm_play.add_argument("--noise-amp", type=float, default=0.08, help="Noise amplitude (0.0-1.0)")
    dm_play.add_argument("--ffmpeg-path", default=None, help="Path to ffmpeg binary")
    dm_play.add_argument("--pulse-source", default=None, help="PulseAudio source name (used with --mode mic)")
    dm_play.add_argument("--mic-input-format", choices=["auto", "pulse", "pipewire", "alsa"], default="auto", help="ffmpeg input format for microphone mode")
    dm_play.add_argument("--pulse-sink", default=None, help="PulseAudio sink name to set as default")
    dm_play.add_argument("--dave-debug", action="store_true", help="Print DAVE negotiation status after connect")
    dm_play.add_argument("--require-dave", action="store_true", help="Abort if DAVE is not active/encrypting")
    dm_play.add_argument("--dave-wait-timeout", type=float, default=10.0, help="Seconds to wait for DAVE encryption readiness")
    dm_play.add_argument("--exp-self-video", action="store_true", help="Experimental: request self camera flag after connect")
    dm_play.add_argument("--exp-self-stream", action="store_true", help="Experimental placeholder for self stream flag")
    dm_play.add_argument("--exp-video-opcode", action="store_true", help="Experimental: send VOICE VIDEO opcode after connect")
    dm_play.add_argument("--exp-video-loop-interval", type=float, default=2.0, help="Seconds between VIDEO opcode loop sends")
    dm_play.add_argument("--exp-fake-video-interval", type=float, default=0.2, help="Seconds between fake video RTP sends in loop")
    dm_play.add_argument("--exp-fake-video-pt", type=int, default=96, help="RTP payload type for fake video packets")
    dm_play.add_argument("--exp-fake-video-payload", type=int, default=900, help="Fake video RTP payload size in bytes")

    tui = sub.add_parser("tui", help="Interactive terminal UI for DM/Guild voice connect")
    tui.add_argument("--ring", action="store_true", help="Ring user when starting DM call")
    tui.add_argument("--file", default="rickroll.ogg", help="Default file path in TUI file mode")
    tui.add_argument("--noise-amp", type=float, default=0.08, help="Default noise amplitude in TUI noise mode")
    tui.add_argument("--pulse-source", default=None, help="Default PulseAudio source for TUI microphone mode")
    tui.add_argument("--mic-input-format", choices=["auto", "pulse", "pipewire", "alsa"], default="auto", help="ffmpeg input format for microphone mode")
    tui.add_argument("--pulse-sink", default=None, help="Default PulseAudio sink")
    tui.add_argument("--ffmpeg-path", default=None, help="Path to ffmpeg binary")
    tui.add_argument("--dave-debug", action="store_true", help="Print DAVE negotiation status after connect")
    tui.add_argument("--require-dave", action="store_true", help="Abort if DAVE is not active/encrypting")
    tui.add_argument("--dave-wait-timeout", type=float, default=10.0, help="Seconds to wait for DAVE encryption readiness")
    tui.add_argument("--exp-self-video", action="store_true", help="Experimental: request self camera flag after connect")
    tui.add_argument("--exp-self-stream", action="store_true", help="Experimental placeholder for self stream flag")
    tui.add_argument("--exp-video-opcode", action="store_true", help="Experimental: send VOICE VIDEO opcode after connect")
    tui.add_argument("--exp-video-loop-interval", type=float, default=2.0, help="Seconds between VIDEO opcode loop sends")
    tui.add_argument("--exp-fake-video-interval", type=float, default=0.2, help="Seconds between fake video RTP sends in loop")
    tui.add_argument("--exp-fake-video-pt", type=int, default=96, help="RTP payload type for fake video packets")
    tui.add_argument("--exp-fake-video-payload", type=int, default=900, help="Fake video RTP payload size in bytes")

    ctui = sub.add_parser("ctui", help="Curses full-screen TUI for DM/Guild connect and live control")
    ctui.add_argument("--ring", action="store_true", help="Ring user when starting DM call")
    ctui.add_argument("--file", default="rickroll.ogg", help="Default file path in Curses TUI")
    ctui.add_argument("--noise-amp", type=float, default=0.08, help="Default noise amplitude")
    ctui.add_argument("--pulse-source", default=None, help="Default PulseAudio source")
    ctui.add_argument("--mic-input-format", choices=["auto", "pulse", "pipewire", "alsa"], default="auto", help="ffmpeg input format for microphone mode")
    ctui.add_argument("--pulse-sink", default=None, help="Default PulseAudio sink")
    ctui.add_argument("--ffmpeg-path", default=None, help="Path to ffmpeg binary")
    ctui.add_argument("--sixel", action="store_true", help="Enable SIXEL avatar/icon previews with chafa")
    ctui.add_argument("--dave-debug", action="store_true", help="Print DAVE negotiation status after connect")
    ctui.add_argument("--require-dave", action="store_true", help="Abort if DAVE is not active/encrypting")
    ctui.add_argument("--dave-wait-timeout", type=float, default=10.0, help="Seconds to wait for DAVE encryption readiness")
    ctui.add_argument("--exp-self-video", action="store_true", help="Experimental: request self camera flag after connect")
    ctui.add_argument("--exp-self-stream", action="store_true", help="Experimental placeholder for self stream flag")
    ctui.add_argument("--exp-video-opcode", action="store_true", help="Experimental: send VOICE VIDEO opcode after connect")
    ctui.add_argument("--exp-video-loop-interval", type=float, default=2.0, help="Seconds between VIDEO opcode loop sends")
    ctui.add_argument("--exp-fake-video-interval", type=float, default=0.2, help="Seconds between fake video RTP sends in loop")
    ctui.add_argument("--exp-fake-video-pt", type=int, default=96, help="RTP payload type for fake video packets")
    ctui.add_argument("--exp-fake-video-payload", type=int, default=900, help="Fake video RTP payload size in bytes")
    ctui.add_argument("--call-notify-seconds", type=float, default=15.0, help="Incoming-call notice duration in seconds")
    ctui.add_argument("--call-notify-persistent", action="store_true", help="Keep incoming-call notice visible until replaced")
    ctui.add_argument("--call-notify-sound", action="store_true", help="Play terminal bell on incoming call")
    ctui.add_argument("--call-notify-cmd", default=None, help="Shell command to run on incoming call")

    args = parser.parse_args()
    cfg = load_local_config(args.config)
    args.config_token = cfg.get("token")

    # Ensure interactive commands always have a full audio state.
    # ctui/tui menus mutate these, but status rendering may read them first.
    if args.command in ("tui", "ctui"):
        if not hasattr(args, "mode"):
            args.mode = "connect"
        if not hasattr(args, "loop"):
            args.loop = False
        if not hasattr(args, "noise_amp"):
            args.noise_amp = 0.08
        if not hasattr(args, "file"):
            args.file = "rickroll.ogg"
        if not hasattr(args, "pulse_source"):
            args.pulse_source = None
        if not hasattr(args, "pulse_sink"):
            args.pulse_sink = None

    if args.command == "play":
        if args.guild_id is None:
            args.guild_id = cfg.get("guild_id")
        if args.channel_id is None:
            args.channel_id = cfg.get("channel_id")
        if args.guild_id is None or args.channel_id is None:
            raise SystemExit("play requires --guild-id and --channel-id (or set them in config)")
    elif args.command == "dm-play":
        if args.user_id is None:
            args.user_id = cfg.get("dm_user_id")
        if args.user_id is None:
            raise SystemExit("dm-play requires --user-id (or set dm_user_id in config)")

    return args


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(_run(args))
        return 0
    except Exception as exc:  # pylint: disable=broad-except
        if args.log_file:
            try:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with open(args.log_file, "a", encoding="utf-8") as f:
                    f.write(f"[{ts}] fatal: {exc!r}\n")
            except Exception:
                pass
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
