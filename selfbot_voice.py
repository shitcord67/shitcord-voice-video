#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import shutil
import sys
from typing import Optional

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
        while True:
            root = self._select_menu("Choose target type", ["DM", "Guild", "Quit"])
            if root == 2:
                return

            if root == 0:
                mode = self._select_menu("DM mode", ["Input ID", "List", "Back"])
                if mode == 2:
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

    async def _connect_dm_by_user_id(self, user_id: int) -> None:
        user = self.get_user(user_id) or await self.fetch_user(user_id)
        if user is None:
            raise RuntimeError(f"User not found: {user_id}")
        dm = user.dm_channel or await user.create_dm()
        if dm is None:
            raise RuntimeError(f"Could not open DM channel with user {user_id}")
        print(f"Connecting to DM call with {user} (ring={self.args.ring})...")
        voice = await dm.connect(reconnect=True, ring=self.args.ring)
        await self._after_connect_dave_checks(voice)
        await self._hold_connection(voice, f"DM:{user}")

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
        await self._hold_connection(voice, f"DM:{recipient_label}")

    async def _connect_guild_voice(self, guild_id: int, channel_id: int) -> None:
        guild = self.get_guild(guild_id)
        if guild is None:
            raise RuntimeError(f"Guild not found: {guild_id}")
        channel = guild.get_channel(channel_id)
        if channel is None or not isinstance(channel, discord.VoiceChannel):
            raise RuntimeError(f"Voice channel not found: {channel_id}")
        print(f"Connecting to {guild.name}/{channel.name}...")
        voice = await channel.connect(reconnect=True, self_deaf=False, self_mute=False)
        await self._after_connect_dave_checks(voice)
        await self._hold_connection(voice, f"{guild.name}/{channel.name}")

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
        await self._hold_connection(voice, f"{guild.name}/{channel.name}")

    async def _after_connect_dave_checks(self, voice: discord.VoiceClient) -> None:
        if self.args.dave_debug or self.args.require_dave:
            await self._wait_for_dave_status(voice, timeout=self.args.dave_wait_timeout)
        if self.args.require_dave:
            self._enforce_dave_or_raise(voice)

    async def _hold_connection(self, voice: discord.VoiceClient, label: str) -> None:
        print(f"Connected to {label}. Press Ctrl+C to disconnect.")
        try:
            while True:
                await asyncio.sleep(1.0)
        except KeyboardInterrupt:
            print("Disconnecting...")
        finally:
            await voice.disconnect(force=True)

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
            print("DAVE: no voice connection internals available")
            return
        max_proto = getattr(conn, "max_dave_protocol_version", None)
        active_proto = getattr(conn, "dave_protocol_version", None)
        can_encrypt = getattr(conn, "can_encrypt", None)
        session = getattr(conn, "dave_session", None)
        privacy_code = getattr(voice, "voice_privacy_code", None)
        print(
            "DAVE status:",
            f"max_protocol={max_proto}",
            f"active_protocol={active_proto}",
            f"can_encrypt={can_encrypt}",
            f"session={'yes' if session else 'no'}",
            f"privacy_code={privacy_code}",
        )

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
                options="-vn -ac 2 -ar 48000",
            )

        return discord.FFmpegPCMAudio(
            source=f"anoisesrc=color=white:amplitude={self.args.noise_amp}:sample_rate=48000",
            executable=ffmpeg,
            before_options="-f lavfi -re",
            options="-vn -ac 2 -ar 48000",
        )

    async def _play_to_voice_client(self, voice: discord.VoiceClient, label: str) -> None:
        ffmpeg = self.args.ffmpeg_path or shutil.which("ffmpeg")
        if ffmpeg is None:
            await voice.disconnect(force=True)
            raise RuntimeError("ffmpeg not found. Install ffmpeg or pass --ffmpeg-path")

        source = self._make_audio_source(ffmpeg)
        if self.args.mode == "file":
            print(f"Playing file to {label} (loop={self.args.loop}): {self.args.file}")
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
    play.add_argument("--dave-debug", action="store_true", help="Print DAVE negotiation status after connect")
    play.add_argument("--require-dave", action="store_true", help="Abort if DAVE is not active/encrypting")
    play.add_argument("--dave-wait-timeout", type=float, default=10.0, help="Seconds to wait for DAVE encryption readiness")

    dm_play = sub.add_parser("dm-play", help="Start/join DM call and play audio")
    dm_play.add_argument("--user-id", type=int, required=False, help="Target user id for DM call")
    dm_play.add_argument("--ring", action="store_true", help="Ring user when starting DM call")
    dm_play.add_argument("--mode", choices=["file", "noise"], default="file")
    dm_play.add_argument("--file", default="rickroll.ogg", help="Audio file path when --mode file")
    dm_play.add_argument("--loop", action="store_true", help="Loop file playback")
    dm_play.add_argument("--noise-amp", type=float, default=0.08, help="Noise amplitude (0.0-1.0)")
    dm_play.add_argument("--ffmpeg-path", default=None, help="Path to ffmpeg binary")
    dm_play.add_argument("--dave-debug", action="store_true", help="Print DAVE negotiation status after connect")
    dm_play.add_argument("--require-dave", action="store_true", help="Abort if DAVE is not active/encrypting")
    dm_play.add_argument("--dave-wait-timeout", type=float, default=10.0, help="Seconds to wait for DAVE encryption readiness")

    tui = sub.add_parser("tui", help="Interactive terminal UI for DM/Guild voice connect")
    tui.add_argument("--ring", action="store_true", help="Ring user when starting DM call")
    tui.add_argument("--dave-debug", action="store_true", help="Print DAVE negotiation status after connect")
    tui.add_argument("--require-dave", action="store_true", help="Abort if DAVE is not active/encrypting")
    tui.add_argument("--dave-wait-timeout", type=float, default=10.0, help="Seconds to wait for DAVE encryption readiness")

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
