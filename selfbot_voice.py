#!/usr/bin/env python3
import argparse
import asyncio
import curses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from typing import Callable, Optional

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
                    ["Connect DM", "Connect Guild", "Recent", "Find User in Voice", "Audio Settings", "Quit"],
                )
                if choice == 0:
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
                            (f"{ch.recipient} ({ch.recipient.id})" if ch.recipient else f"unknown ({ch.id})")
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
                elif choice == 1:
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
                elif choice == 2:
                    conn = self._ctui_connect_recent(stdscr, loop)
                    if conn is not None:
                        voice, label = conn
                elif choice == 3:
                    conn = self._ctui_find_user_in_voice(stdscr, loop)
                    if conn is not None:
                        voice, label = conn
                elif choice == 4:
                    self._ctui_audio_settings(stdscr)
                else:
                    return
            else:
                choice = self._curses_menu(
                    stdscr,
                    f"Connected: {label}",
                    [
                        "Restart / Apply audio mode",
                        "Audio settings",
                        "Show DAVE status",
                        "Show debug log",
                        "Switch target (disconnect)",
                        "Disconnect",
                        "Quit",
                    ],
                    voice=voice,
                )
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
                        run(self._wait_for_dave_status(voice, timeout=max(0.5, self.args.dave_wait_timeout)))
                        self._curses_message(stdscr, self._last_dave_status)
                    except Exception as e:
                        self._curses_message(stdscr, f"Error: {e}")
                elif choice == 3:
                    self._curses_show_debug_log(stdscr)
                elif choice in (4, 5):
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
            sources = self._pulse_devices("sources")
            if sources:
                idx = self._curses_menu(stdscr, "Select Pulse source", sources + ["Manual", "Back"])
                if idx < len(sources):
                    self.args.pulse_source = sources[idx]
                elif idx == len(sources):
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

        sinks = self._pulse_devices("sinks")
        if sinks:
            idx = self._curses_menu(stdscr, "Output sink", ["Keep current"] + sinks + ["Manual"])
            if idx >= 1 and idx <= len(sinks):
                self.args.pulse_sink = sinks[idx - 1]
                self._set_default_pulse_device("sink", self.args.pulse_sink)
            elif idx == len(sinks) + 1:
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
            if title.startswith("Connected:"):
                status = self._collect_voice_status(voice)
                self._curses_add_wrapped(stdscr, 1, 0, status)
                self._curses_add_wrapped(stdscr, 2, 0, self._last_dave_status)
                base = 5
            else:
                base = 3
            self._safe_addstr(stdscr, base - 1, 0, f"Search: {query}")
            render_items = [items[i] for i in filtered] if filtered else ["(no results)"]
            for i, item in enumerate(render_items):
                prefix = "> " if i == idx else "  "
                self._safe_addstr(stdscr, base + i, 0, f"{prefix}{item}")
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
            elif ch in (ord("p"), ord("P")):
                if preview_callback and filtered:
                    preview_callback(filtered[idx])
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
            with urllib.request.urlopen(url, timeout=10) as resp:
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

    def _collect_voice_status(self, voice: Optional[discord.VoiceClient]) -> str:
        if voice is None:
            return "Status: disconnected"
        conn = getattr(voice, "_connection", None)
        dave_proto = getattr(conn, "dave_protocol_version", None) if conn else None
        dave_encrypt = getattr(conn, "can_encrypt", None) if conn else None
        ws = getattr(conn, "ws", None) if conn else None
        voice_ver = getattr(ws, "voice_version", None) if ws else None
        rtc_ver = getattr(ws, "rtc_worker_version", None) if ws else None
        return (
            "Status: "
            f"connected={voice.is_connected()} "
            f"playing={voice.is_playing()} "
            f"mode={self.args.mode} "
            f"dave_protocol={dave_proto} "
            f"dave_encrypt={dave_encrypt} "
            f"voice_backend={voice_ver} "
            f"rtc_worker={rtc_ver}"
        )

    def _dbg(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._debug_lines.append(f"[{ts}] {msg}")
        if len(self._debug_lines) > 500:
            self._debug_lines = self._debug_lines[-500:]

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
        if voice.is_playing():
            voice.stop()
        await voice.disconnect(force=True)

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

    def _pulse_devices(self, kind: str) -> list[str]:
        try:
            out = subprocess.check_output(["pactl", "list", "short", kind], text=True, stderr=subprocess.DEVNULL)
        except Exception:
            return []
        devices = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                devices.append(parts[1].strip())
        return devices

    def _select_pulse_device(self, kind: str, label: str, current: Optional[str]) -> Optional[str]:
        devices = self._pulse_devices(kind)
        if not devices:
            manual = input(f"No PulseAudio {label}s found. Enter {label} name manually (blank to skip): ").strip()
            return manual or current
        opts = [f"{name}{' (current)' if current == name else ''}" for name in devices]
        idx = self._select_menu(f"Select {label}", opts + ["Manual input", "Keep current"])
        if idx == len(opts):
            manual = input(f"Enter {label} name: ").strip()
            return manual or current
        if idx == len(opts) + 1:
            return current
        return devices[idx]

    def _set_default_pulse_device(self, kind: str, name: str) -> None:
        cmd = ["pactl", f"set-default-{kind}", name]
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _print_pulse_devices(self, kind: str) -> None:
        devices = self._pulse_devices(kind)
        if not devices:
            print(f"No PulseAudio {kind} found.")
            return
        print(f"PulseAudio {kind}:")
        for dev in devices:
            print(f"  - {dev}")

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
            return discord.FFmpegPCMAudio(
                source=source_name,
                executable=ffmpeg,
                before_options="-f pulse -thread_queue_size 1024",
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
    parser = argparse.ArgumentParser(description="discord.py-self voice helper")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help=f"Path to local config json (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--token", help="Discord user token (or set DISCORD_USER_TOKEN)")

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

    dm_play = sub.add_parser("dm-play", help="Start/join DM call and play audio")
    dm_play.add_argument("--user-id", type=int, required=False, help="Target user id for DM call")
    dm_play.add_argument("--ring", action="store_true", help="Ring user when starting DM call")
    dm_play.add_argument("--mode", choices=["file", "noise", "mic"], default="file")
    dm_play.add_argument("--file", default="rickroll.ogg", help="Audio file path when --mode file")
    dm_play.add_argument("--loop", action="store_true", help="Loop file playback")
    dm_play.add_argument("--noise-amp", type=float, default=0.08, help="Noise amplitude (0.0-1.0)")
    dm_play.add_argument("--ffmpeg-path", default=None, help="Path to ffmpeg binary")
    dm_play.add_argument("--pulse-source", default=None, help="PulseAudio source name (used with --mode mic)")
    dm_play.add_argument("--pulse-sink", default=None, help="PulseAudio sink name to set as default")
    dm_play.add_argument("--dave-debug", action="store_true", help="Print DAVE negotiation status after connect")
    dm_play.add_argument("--require-dave", action="store_true", help="Abort if DAVE is not active/encrypting")
    dm_play.add_argument("--dave-wait-timeout", type=float, default=10.0, help="Seconds to wait for DAVE encryption readiness")

    tui = sub.add_parser("tui", help="Interactive terminal UI for DM/Guild voice connect")
    tui.add_argument("--ring", action="store_true", help="Ring user when starting DM call")
    tui.add_argument("--file", default="rickroll.ogg", help="Default file path in TUI file mode")
    tui.add_argument("--noise-amp", type=float, default=0.08, help="Default noise amplitude in TUI noise mode")
    tui.add_argument("--pulse-source", default=None, help="Default PulseAudio source for TUI microphone mode")
    tui.add_argument("--pulse-sink", default=None, help="Default PulseAudio sink")
    tui.add_argument("--ffmpeg-path", default=None, help="Path to ffmpeg binary")
    tui.add_argument("--dave-debug", action="store_true", help="Print DAVE negotiation status after connect")
    tui.add_argument("--require-dave", action="store_true", help="Abort if DAVE is not active/encrypting")
    tui.add_argument("--dave-wait-timeout", type=float, default=10.0, help="Seconds to wait for DAVE encryption readiness")

    ctui = sub.add_parser("ctui", help="Curses full-screen TUI for DM/Guild connect and live control")
    ctui.add_argument("--ring", action="store_true", help="Ring user when starting DM call")
    ctui.add_argument("--file", default="rickroll.ogg", help="Default file path in Curses TUI")
    ctui.add_argument("--noise-amp", type=float, default=0.08, help="Default noise amplitude")
    ctui.add_argument("--pulse-source", default=None, help="Default PulseAudio source")
    ctui.add_argument("--pulse-sink", default=None, help="Default PulseAudio sink")
    ctui.add_argument("--ffmpeg-path", default=None, help="Path to ffmpeg binary")
    ctui.add_argument("--sixel", action="store_true", help="Enable SIXEL avatar/icon previews with chafa")
    ctui.add_argument("--dave-debug", action="store_true", help="Print DAVE negotiation status after connect")
    ctui.add_argument("--require-dave", action="store_true", help="Abort if DAVE is not active/encrypting")
    ctui.add_argument("--dave-wait-timeout", type=float, default=10.0, help="Seconds to wait for DAVE encryption readiness")

    args = parser.parse_args()
    cfg = load_local_config(args.config)
    args.config_token = cfg.get("token")

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
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
