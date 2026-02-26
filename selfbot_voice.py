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
        voice = await channel.connect(reconnect=False, self_deaf=False, self_mute=False)
        await self._play_to_voice_client(voice, f"{guild.name}/{channel.name}")

    async def _play_dm_audio(self) -> None:
        user = self.get_user(self.args.user_id) or await self.fetch_user(self.args.user_id)
        if user is None:
            raise RuntimeError(f"User not found: {self.args.user_id}")

        dm = user.dm_channel or await user.create_dm()
        if dm is None:
            raise RuntimeError(f"Could not open DM channel with user {self.args.user_id}")

        print(f"Connecting to DM call with {user} (ring={self.args.ring})...")
        voice = await dm.connect(reconnect=False, ring=self.args.ring)
        await self._play_to_voice_client(voice, f"DM:{user}")

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

    dm_play = sub.add_parser("dm-play", help="Start/join DM call and play audio")
    dm_play.add_argument("--user-id", type=int, required=False, help="Target user id for DM call")
    dm_play.add_argument("--ring", action="store_true", help="Ring user when starting DM call")
    dm_play.add_argument("--mode", choices=["file", "noise"], default="file")
    dm_play.add_argument("--file", default="rickroll.ogg", help="Audio file path when --mode file")
    dm_play.add_argument("--loop", action="store_true", help="Loop file playback")
    dm_play.add_argument("--noise-amp", type=float, default=0.08, help="Noise amplitude (0.0-1.0)")
    dm_play.add_argument("--ffmpeg-path", default=None, help="Path to ffmpeg binary")

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
