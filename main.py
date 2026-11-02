"""
PRIVATE DISCORD MANAGEMENT BOT
Python 3.12 | discord.py 2.x | Prefix + Slash (hybrid) | SQLite
Single-file with owner controls, moderation, logging, AutoMod,
anti-nuke safeguards, server snapshots, personal tools, automation + MUSIC
+ 24/7 VC stay + Voice Recording system.

IMPORTANT:
- Set DISCORD_TOKEN and OWNER_ID in Railway Variables.
- Enable Message Content Intent and Server Members Intent in the Developer Portal.
- Mount a Railway Volume at /data. The database and backups are stored there.
- Give the bot only the permissions it needs. Do not casually grant Administrator.
- Anti-nuke is best-effort; Discord audit-log/API delays mean no bot can guarantee
  prevention of every destructive action.
- MUSIC: Requires FFmpeg + a JavaScript runtime (nodejs or deno) + yt-dlp + PyNaCl.
  Railway: set RAILPACK_DEPLOY_APT_PACKAGES=ffmpeg   and add nodejs (Railpack: RAILPACK_PACKAGES=node)
  requirements: discord.py[voice], yt-dlp[default], PyNaCl, numpy
  If YouTube says "Sign in to confirm you're not a bot": export cookies.txt from a
  logged-in browser and paste its content into the YTDLP_COOKIES variable.
- VOICE RECORDING (optional but recommended):
  pip install discord-ext-voice-recv
  Full per-user recording needs this package. Without it, recording commands
  will show a helpful message instead of crashing.
- Prefix system: Current prefix + previous prefix + DEFAULT_PREFIX + @mention all work.
- Slash commands: Most important commands are hybrid (work with both prefix and /).
"""

from __future__ import annotations

import os
import re
import json
import time
import math
import asyncio
import secrets
import string
import logging
import platform
import shutil
import shlex
import threading
import datetime as dt
from pathlib import Path
from typing import Optional, Dict, List, Any
from collections import deque

import discord
from discord.ext import commands, tasks
from discord import ui, ButtonStyle, Interaction, SelectOption, app_commands

try:
    import psutil
except ImportError:
    psutil = None

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

# Optional voice recording support
try:
    from discord.ext import voice_recv
    from discord.ext.voice_recv import VoiceRecvClient, AudioSink
    HAS_VOICE_RECV = True
except ImportError:
    voice_recv = None
    VoiceRecvClient = None
    AudioSink = object  # placeholder so the class below still defines
    HAS_VOICE_RECV = False

# ----------------------------- CONFIGURATION -----------------------------

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DEFAULT_PREFIX = os.getenv("DEFAULT_PREFIX", ".")
DB_PATH = os.getenv("DATABASE_PATH", "/data/bot.db")
DATA_DIR = Path(DB_PATH).parent
BACKUP_DIR = DATA_DIR / "backups"
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")

RECORDINGS_DIR = DATA_DIR / "recordings"
DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

# Optional YouTube cookies (fixes "Sign in to confirm you're not a bot" on Railway).
# Put the full contents of a Netscape cookies.txt into the YTDLP_COOKIES variable,
# or upload cookies.txt to /data/cookies.txt yourself.
COOKIES_FILE = DATA_DIR / "cookies.txt"
_ck = os.getenv("YTDLP_COOKIES", "")
if _ck.strip():
    try:
        COOKIES_FILE.write_text(_ck.replace("\\n", "\n"), encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("private-management-bot")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.moderation = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix=DEFAULT_PREFIX,  # will be replaced by dynamic_prefix
    intents=intents,
    help_command=None,
    case_insensitive=True,
    strip_after_prefix=True
)
bot.start_time = time.monotonic()
db: Optional[object] = None
anti_nuke_events: dict[tuple[int, str], list[float]] = {}
reminder_task_started = False
stay_vc_task_started = False

# Voice recording state
active_recordings: Dict[int, dict] = {}  # guild_id -> recording info

# ----------------------------- MUSIC STATE --------------------------------

# Per-guild music state
music_queues: Dict[int, deque] = {}          # guild_id -> deque of track dicts
now_playing: Dict[int, dict] = {}            # guild_id -> current track info
np_messages: Dict[int, discord.Message] = {} # guild_id -> now playing message
progress_tasks: Dict[int, asyncio.Task] = {} # guild_id -> progress updater task
volume_levels: Dict[int, float] = {}         # guild_id -> volume 0.0-2.0

YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extract_flat": False,
    "noplaylist": True,
    "geo_bypass": True,
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin",
    "options": "-vn",
}

# ----------------------------- DATABASE ----------------------------------

def connect_db():
    import sqlite3
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    global db
    db = connect_db()
    schema = """
    CREATE TABLE IF NOT EXISTS guild_config(
      guild_id INTEGER PRIMARY KEY, prefix TEXT, previous_prefix TEXT,
      log_channel INTEGER,
      welcome_channel INTEGER, goodbye_channel INTEGER, autorole INTEGER,
      antinuke INTEGER DEFAULT 0, antinuke_limit INTEGER DEFAULT 3,
      automod INTEGER DEFAULT 0, bad_words TEXT DEFAULT '[]',
      blocked_links INTEGER DEFAULT 0, invite_filter INTEGER DEFAULT 0,
      spam_limit INTEGER DEFAULT 6, lockdown INTEGER DEFAULT 0,
      stay_vc INTEGER DEFAULT 0, stay_channel INTEGER
    );
    CREATE TABLE IF NOT EXISTS owners(user_id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS whitelist(guild_id INTEGER, user_id INTEGER,
      PRIMARY KEY(guild_id,user_id));
    CREATE TABLE IF NOT EXISTS blacklist(user_id INTEGER PRIMARY KEY, reason TEXT);
    CREATE TABLE IF NOT EXISTS warnings(id INTEGER PRIMARY KEY AUTOINCREMENT,
      guild_id INTEGER, user_id INTEGER, moderator_id INTEGER, reason TEXT,
      created_at TEXT);
    CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER, note TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS todos(id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER, task TEXT, done INTEGER DEFAULT 0, created_at TEXT);
    CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER, channel_id INTEGER, message TEXT, due_at REAL, sent INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS mod_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,
      guild_id INTEGER, actor_id INTEGER, action TEXT, target_id INTEGER,
      reason TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS autoresponses(guild_id INTEGER, trigger TEXT,
      response TEXT, PRIMARY KEY(guild_id,trigger));
    CREATE TABLE IF NOT EXISTS sticky_messages(channel_id INTEGER PRIMARY KEY,
      content TEXT, message_id INTEGER);
    CREATE TABLE IF NOT EXISTS backups(id INTEGER PRIMARY KEY AUTOINCREMENT,
      guild_id INTEGER, filename TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS recordings(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      guild_id INTEGER, channel_id INTEGER, started_by INTEGER,
      filename TEXT, title TEXT, duration REAL DEFAULT 0,
      created_at TEXT, file_size INTEGER DEFAULT 0
    );
    """
    db.executescript(schema)
    # Migration for older DBs that may lack new columns
    try:
        db.execute("ALTER TABLE guild_config ADD COLUMN previous_prefix TEXT")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE guild_config ADD COLUMN stay_vc INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE guild_config ADD COLUMN stay_channel INTEGER")
    except Exception:
        pass
    if OWNER_ID:
        db.execute("INSERT OR IGNORE INTO owners(user_id) VALUES(?)", (OWNER_ID,))
    db.commit()

def q(sql, params=(), fetch="none"):
    cur = db.execute(sql, params)
    db.commit()
    if fetch == "one":
        return cur.fetchone()
    if fetch == "all":
        return cur.fetchall()
    return cur.lastrowid

def get_cfg(guild_id: int):
    row = q("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,), "one")
    if row:
        return dict(row)
    q("INSERT OR IGNORE INTO guild_config(guild_id,prefix) VALUES(?,?)",
      (guild_id, DEFAULT_PREFIX))
    return dict(q("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,), "one"))

def prefix_for(guild_id: Optional[int]):
    if guild_id is None:
        return DEFAULT_PREFIX
    return get_cfg(guild_id).get("prefix") or DEFAULT_PREFIX

def get_all_prefixes(guild_id: Optional[int]) -> List[str]:
    """Return all valid prefixes for a guild: current + previous + default."""
    prefixes = [DEFAULT_PREFIX]
    if guild_id is None:
        return prefixes
    cfg = get_cfg(guild_id)
    current = cfg.get("prefix")
    previous = cfg.get("previous_prefix")
    if current and current not in prefixes:
        prefixes.append(current)
    if previous and previous not in prefixes:
        prefixes.append(previous)
    return prefixes

async def dynamic_prefix(bot_obj, message):
    """Support: current prefix, previous prefix, DEFAULT_PREFIX, and @mention."""
    guild_id = message.guild.id if message.guild else None
    prefixes = get_all_prefixes(guild_id)
    return commands.when_mentioned_or(*prefixes)(bot_obj, message)

bot.command_prefix = dynamic_prefix

def is_owner_id(user_id: int) -> bool:
    if OWNER_ID and user_id == OWNER_ID:
        return True
    return q("SELECT 1 FROM owners WHERE user_id=?", (user_id,), "one") is not None

def owner_only():
    async def predicate(ctx):
        if not is_owner_id(ctx.author.id):
            raise commands.CheckFailure("This command is restricted to the bot owner.")
        return True
    return commands.check(predicate)

def guild_only():
    return commands.guild_only()

def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

def safe_reason(reason: Optional[str]) -> str:
    return (reason or "No reason provided")[:450]

async def send_embed(ctx, title, description=None, color=discord.Color.blurple()):
    em = discord.Embed(title=title, description=description, color=color,
                       timestamp=dt.datetime.now(dt.timezone.utc))
    await ctx.send(embed=em)

async def send_log(guild: discord.Guild, title: str, description: str):
    cfg = get_cfg(guild.id)
    channel_id = cfg.get("log_channel")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if channel:
        try:
            await channel.send(embed=discord.Embed(
                title=title, description=description[:4000],
                color=discord.Color.orange(),
                timestamp=dt.datetime.now(dt.timezone.utc)))
        except discord.HTTPException:
            pass

def log_action(guild_id, actor_id, action, target_id=None, reason=None):
    q("""INSERT INTO mod_logs(guild_id,actor_id,action,target_id,reason,created_at)
         VALUES(?,?,?,?,?,?)""",
      (guild_id, actor_id, action, target_id, safe_reason(reason), utcnow()))

async def ensure_manageable(ctx, member: discord.Member):
    if member.id == ctx.guild.owner_id:
        await ctx.send("I cannot moderate the server owner.")
        return False
    if member.id == ctx.author.id:
        await ctx.send("You cannot target yourself with this command.")
        return False
    if member.top_role >= ctx.guild.me.top_role:
        await ctx.send("My highest role must be above the target's highest role.")
        return False
    if ctx.author.id != ctx.guild.owner_id and member.top_role >= ctx.author.top_role:
        await ctx.send("You cannot target a member with an equal or higher role.")
        return False
    return True

# ----------------------------- MUSIC HELPERS ------------------------------

def format_duration(seconds: float | int | None) -> str:
    if seconds is None or seconds <= 0:
        return "Live / Unknown"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def make_progress_bar(elapsed: float, total: float, length: int = 18) -> str:
    if total <= 0:
        return "░" * length + "  LIVE"
    ratio = min(max(elapsed / total, 0.0), 1.0)
    filled = int(ratio * length)
    bar = "━" * filled + "●" + "─" * (length - filled - 1)
    if filled >= length:
        bar = "━" * (length - 1) + "●"
    return f"`{bar}`"

def create_np_embed(track: dict, elapsed: float = 0.0, paused: bool = False) -> discord.Embed:
    title = track.get("title", "Unknown Title")
    url = track.get("webpage_url") or track.get("url") or ""
    duration = track.get("duration") or 0
    requester = track.get("requester_name", "Unknown")
    thumbnail = track.get("thumbnail")
    uploader = track.get("uploader") or track.get("channel") or "Unknown"

    status = "⏸️ Paused" if paused else "▶️ Now Playing"
    color = discord.Color.orange() if paused else discord.Color.green()

    em = discord.Embed(
        title=f"{status}",
        description=f"**[{title}]({url})**" if url else f"**{title}**",
        color=color,
        timestamp=dt.datetime.now(dt.timezone.utc)
    )
    if thumbnail:
        em.set_thumbnail(url=thumbnail)
        # banner-style image if available
        if track.get("thumbnail"):
            em.set_image(url=thumbnail)

    em.add_field(name="👤 Requested by", value=requester, inline=True)
    em.add_field(name="🎤 Channel / Artist", value=uploader[:60], inline=True)
    em.add_field(name="⏱️ Duration", value=format_duration(duration), inline=True)

    progress = make_progress_bar(elapsed, duration)
    time_str = f"{format_duration(elapsed)} / {format_duration(duration)}"
    em.add_field(name="Progress", value=f"{progress}\n`{time_str}`", inline=False)

    em.set_footer(text="Use the buttons below to control playback")
    return em

LAST_EXTRACT_ERROR = ""  # last failure reason, shown to the user by .play

# Different YouTube player clients to try one after another.
_YT_ATTEMPTS = [
    ("default", {}),
    ("android_vr+web_safari", {"extractor_args": {"youtube": {"player_client": ["android_vr", "web_safari"]}}}),
    ("tv+ios", {"extractor_args": {"youtube": {"player_client": ["tv", "ios"]}}}),
]

def _clean_err(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", str(text)).strip()

def _base_ydl_opts() -> dict:
    opts = dict(YDL_OPTS)
    # Modern YouTube needs a JavaScript runtime (deno / node / bun) for yt-dlp.
    runtimes = {}
    for name in ("deno", "node", "bun"):
        path = shutil.which(name)
        if path:
            runtimes[name] = {"path": path}
    if runtimes:
        opts["js_runtimes"] = runtimes
    opts["remote_components"] = ["ejs:github"]
    try:
        if COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0:
            opts["cookiefile"] = str(COOKIES_FILE)
    except Exception:
        pass
    return opts

def _run_extract(query_final: str, extra: dict) -> Optional[dict]:
    opts = _base_ydl_opts()
    opts.update(extra)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query_final, download=False)
    if info and "entries" in info:
        entries = [e for e in (info["entries"] or []) if e]
        info = entries[0] if entries else None
    return info

async def extract_track_info(query: str, requester: discord.Member) -> Optional[dict]:
    """Extract info from URL or search query using yt-dlp (with fallbacks)."""
    global LAST_EXTRACT_ERROR
    LAST_EXTRACT_ERROR = ""
    if yt_dlp is None:
        LAST_EXTRACT_ERROR = "yt-dlp installed nahi hai"
        return None

    is_url = re.match(r"https?://", query) is not None
    primary = query if is_url else f"ytsearch1:{query}"
    plans = [(label, primary, extra) for label, extra in _YT_ATTEMPTS]
    if not is_url:
        plans.append(("soundcloud", f"scsearch1:{query}", {}))

    loop = asyncio.get_running_loop()
    info = None
    errors: List[str] = []
    for label, query_final, extra in plans:
        try:
            info = await loop.run_in_executor(None, _run_extract, query_final, extra)
            if info and (info.get("url") or info.get("formats")):
                break
            info = None
            errors.append(f"{label}: no result")
        except Exception as e:
            info = None
            errors.append(f"{label}: {_clean_err(e)[:220]}")
            log.warning("yt-dlp attempt '%s' failed: %s", label, _clean_err(e))

    if not info:
        LAST_EXTRACT_ERROR = " | ".join(errors[-2:]) or "No results"
        return None

    # Prefer streamable URL (audio-only formats first)
    stream_url = info.get("url")
    if not stream_url and info.get("formats"):
        fmts = [f for f in info["formats"] if f.get("url") and f.get("acodec") not in (None, "none")]
        audio_only = [f for f in fmts if f.get("vcodec") in (None, "none")]
        pick = (audio_only or fmts or [None])[-1]
        if pick:
            stream_url = pick["url"]
    if not stream_url:
        LAST_EXTRACT_ERROR = "Stream URL nahi mila"
        return None

    thumb = info.get("thumbnail")
    if not thumb and info.get("thumbnails"):
        thumb = info["thumbnails"][-1].get("url")

    headers = info.get("http_headers") or {}
    return {
        "title": info.get("title") or "Unknown",
        "url": stream_url,
        "webpage_url": info.get("webpage_url") or info.get("original_url") or query,
        "duration": info.get("duration") or 0,
        "thumbnail": thumb,
        "uploader": info.get("uploader") or info.get("channel") or info.get("artist"),
        "user_agent": headers.get("User-Agent"),
        "requester_id": requester.id,
        "requester_name": str(requester),
        "start_time": None,  # set when playback actually starts
    }

def get_queue(guild_id: int) -> deque:
    if guild_id not in music_queues:
        music_queues[guild_id] = deque()
    return music_queues[guild_id]

def is_stay_vc_enabled(guild_id: int) -> bool:
    cfg = get_cfg(guild_id)
    return bool(cfg.get("stay_vc"))

def get_stay_channel_id(guild_id: int) -> Optional[int]:
    cfg = get_cfg(guild_id)
    return cfg.get("stay_channel")

async def ensure_voice(ctx) -> Optional[discord.VoiceClient]:
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ Pehle kisi voice channel me join karo.")
        return None
    channel = ctx.author.voice.channel
    if ctx.voice_client is None:
        try:
            # Prefer VoiceRecvClient when recording package is available
            cls = VoiceRecvClient if HAS_VOICE_RECV else discord.VoiceClient
            vc = await channel.connect(timeout=15.0, reconnect=True, cls=cls)
            return vc
        except Exception as e:
            await ctx.send(f"❌ Voice channel join nahi ho paya: `{e}`")
            return None
    if ctx.voice_client.channel != channel:
        await ctx.voice_client.move_to(channel)
    return ctx.voice_client

async def play_next(guild: discord.Guild, text_channel: discord.TextChannel):
    """Play next song in queue or clean up."""
    gid = guild.id
    queue = get_queue(gid)
    vc = guild.voice_client

    # Cancel old progress task
    if gid in progress_tasks:
        progress_tasks[gid].cancel()
        progress_tasks.pop(gid, None)

    if not queue:
        now_playing.pop(gid, None)
        if gid in np_messages:
            try:
                stay = is_stay_vc_enabled(gid)
                msg = "🎵 Queue empty. Playback finished." + (" (24/7 mode — still in VC)" if stay else "")
                await np_messages[gid].edit(content=msg, embed=None, view=None)
            except Exception:
                pass
            np_messages.pop(gid, None)
        # Do NOT disconnect when 24/7 stay is enabled
        return

    track = queue.popleft()
    now_playing[gid] = track
    track["start_time"] = time.time()

    if not vc or not vc.is_connected():
        return

    try:
        before = FFMPEG_OPTIONS["before_options"]
        if track.get("user_agent"):
            before += f" -user_agent {shlex.quote(track['user_agent'])}"
        source = discord.FFmpegPCMAudio(track["url"], before_options=before, options=FFMPEG_OPTIONS["options"])
        # Apply volume
        vol = volume_levels.get(gid, 1.0)
        source = discord.PCMVolumeTransformer(source, volume=vol)

        def after_play(error):
            if error:
                log.error("Player error in guild %s: %s", gid, error)
            fut = asyncio.run_coroutine_threadsafe(
                play_next(guild, text_channel), bot.loop
            )
            try:
                fut.result(timeout=10)
            except Exception:
                pass

        vc.play(source, after=after_play)

        # Send / update Now Playing with buttons
        embed = create_np_embed(track, elapsed=0.0)
        view = MusicControlView(guild.id)
        try:
            if gid in np_messages:
                msg = await np_messages[gid].edit(content=None, embed=embed, view=view)
            else:
                msg = await text_channel.send(embed=embed, view=view)
            np_messages[gid] = msg
        except Exception:
            msg = await text_channel.send(embed=embed, view=view)
            np_messages[gid] = msg

        # Start progress updater
        progress_tasks[gid] = bot.loop.create_task(update_progress(guild.id))

    except Exception as e:
        log.exception("Failed to play track: %s", e)
        await text_channel.send(f"❌ Playback error: `{e}`")
        await play_next(guild, text_channel)

async def update_progress(guild_id: int):
    """Periodically edit the Now Playing message with moving progress bar."""
    try:
        while True:
            await asyncio.sleep(4)  # update every 4 seconds
            track = now_playing.get(guild_id)
            msg = np_messages.get(guild_id)
            if not track or not msg:
                break
            guild = bot.get_guild(guild_id)
            if not guild or not guild.voice_client:
                break
            vc = guild.voice_client
            if not vc.is_playing() and not vc.is_paused():
                break

            start = track.get("start_time") or time.time()
            # Adjust for pause time roughly (simple version)
            elapsed = time.time() - start
            if vc.is_paused():
                # freeze visual progress while paused
                pass
            duration = track.get("duration") or 0
            if duration and elapsed > duration + 2:
                break

            embed = create_np_embed(track, elapsed=elapsed, paused=vc.is_paused())
            view = MusicControlView(guild_id)
            try:
                await msg.edit(embed=embed, view=view)
            except discord.HTTPException:
                break
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.debug("Progress updater stopped: %s", e)

# ----------------------------- MUSIC UI VIEWS -----------------------------

class MusicControlView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)  # persistent while song plays
        self.guild_id = guild_id

    async def interaction_check(self, interaction: Interaction) -> bool:
        # Anyone in the same voice channel can control
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await interaction.response.send_message("Wrong server.", ephemeral=True)
            return False
        return True

    @ui.button(label="Pause", style=ButtonStyle.secondary, emoji="⏸️", custom_id="music_pause")
    async def pause_btn(self, interaction: Interaction, button: ui.Button):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            return await interaction.response.send_message("Kuch play nahi ho raha.", ephemeral=True)
        vc.pause()
        track = now_playing.get(self.guild_id)
        if track and self.guild_id in np_messages:
            elapsed = time.time() - (track.get("start_time") or time.time())
            embed = create_np_embed(track, elapsed=elapsed, paused=True)
            await np_messages[self.guild_id].edit(embed=embed, view=self)
        await interaction.response.defer()

    @ui.button(label="Resume", style=ButtonStyle.success, emoji="▶️", custom_id="music_resume")
    async def resume_btn(self, interaction: Interaction, button: ui.Button):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_paused():
            return await interaction.response.send_message("Paused nahi hai.", ephemeral=True)
        vc.resume()
        track = now_playing.get(self.guild_id)
        if track and self.guild_id in np_messages:
            # rough resume — progress continues from wall time (simple)
            elapsed = time.time() - (track.get("start_time") or time.time())
            embed = create_np_embed(track, elapsed=elapsed, paused=False)
            await np_messages[self.guild_id].edit(embed=embed, view=self)
        await interaction.response.defer()

    @ui.button(label="Skip", style=ButtonStyle.primary, emoji="⏭️", custom_id="music_skip")
    async def skip_btn(self, interaction: Interaction, button: ui.Button):
        vc = interaction.guild.voice_client
        if not vc or (not vc.is_playing() and not vc.is_paused()):
            return await interaction.response.send_message("Kuch play nahi ho raha.", ephemeral=True)
        vc.stop()  # triggers after_play -> play_next
        await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)

    @ui.button(label="Stop", style=ButtonStyle.danger, emoji="⏹️", custom_id="music_stop")
    async def stop_btn(self, interaction: Interaction, button: ui.Button):
        vc = interaction.guild.voice_client
        gid = self.guild_id
        get_queue(gid).clear()
        now_playing.pop(gid, None)
        if gid in progress_tasks:
            progress_tasks[gid].cancel()
            progress_tasks.pop(gid, None)
        if vc:
            vc.stop()
            if not is_stay_vc_enabled(gid):
                await vc.disconnect()
        if gid in np_messages:
            try:
                msg = "⏹️ Playback stopped & queue cleared."
                if is_stay_vc_enabled(gid):
                    msg += " (24/7 — still in VC)"
                await np_messages[gid].edit(content=msg, embed=None, view=None)
            except Exception:
                pass
            np_messages.pop(gid, None)
        text = "⏹️ Stopped" + (" (staying in VC because of 24/7)" if is_stay_vc_enabled(gid) else " and left voice channel.")
        await interaction.response.send_message(text, ephemeral=True)

    @ui.button(label="Queue", style=ButtonStyle.secondary, emoji="📜", custom_id="music_queue")
    async def queue_btn(self, interaction: Interaction, button: ui.Button):
        qlist = list(get_queue(self.guild_id))
        track = now_playing.get(self.guild_id)
        lines = []
        if track:
            lines.append(f"**Now:** {track.get('title', '?')[:60]}")
        if not qlist:
            lines.append("Queue empty.")
        else:
            for i, t in enumerate(qlist[:12], 1):
                lines.append(f"`{i}.` {t.get('title', '?')[:55]}")
            if len(qlist) > 12:
                lines.append(f"... +{len(qlist)-12} more")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

# ----------------------------- HELP UI ------------------------------------

HELP_PAGES = {
    "owner": ("👑 Owner", "`{p}ping` `{p}stats` `{p}uptime` `{p}prefix` `{p}maintenance` `{p}shutdown` `{p}guilds` `{p}system`"),
    "moderation": ("🛡️ Moderation", "`{p}ban` `{p}unban` `{p}kick` `{p}timeout` `{p}untimeout` `{p}warn` `{p}warnings` `{p}purge` `{p}slowmode` `{p}lock` `{p}unlock`"),
    "security": ("🔒 Security", "`{p}antinuke` `{p}whitelist` `{p}unwhitelist` `{p}automod` `{p}badword` `{p}linkfilter` `{p}invitefilter`"),
    "server": ("🏠 Server", "`{p}serverinfo` `{p}userinfo` `{p}avatar` `{p}roleinfo` `{p}channelinfo` `{p}membercount` `{p}createchannel` `{p}createrole`"),
    "logs": ("📋 Logs", "`{p}setlogs` `{p}modlogs`"),
    "backup": ("💾 Backup", "`{p}backup create` `{p}backup list` `{p}backup restore`"),
    "personal": ("📝 Personal", "`{p}note add/list/delete` `{p}todo add/list/done` `{p}remind` `{p}password` `{p}calculate` `{p}timestamp`"),
    "automation": ("⚙️ Automation", "`{p}setwelcome` `{p}setgoodbye` `{p}setautorole` `{p}autoresponse add/remove/list` `{p}sticky`"),
    "music": ("🎵 Music", "`{p}play <url/name>` `{p}pause` `{p}resume` `{p}skip` `{p}stop` `{p}queue` `{p}np` `{p}volume` `{p}leave` `{p}join`"),
    "voice": ("🎙️ Voice & 24/7", "`{p}247 <channel>` `{p}record start/stop` `{p}recordings` `{p}playrec <id>` `{p}delrec <id>`"),
}

class HelpSelect(ui.Select):
    def __init__(self, prefix: str):
        options = [
            SelectOption(label=v[0], value=k, description=f"Show {v[0]} commands")
            for k, v in HELP_PAGES.items()
        ]
        super().__init__(placeholder="📂 Category choose karo...", min_values=1, max_values=1, options=options)
        self.prefix = prefix

    async def callback(self, interaction: Interaction):
        key = self.values[0]
        title, cmds = HELP_PAGES[key]
        text = cmds.format(p=self.prefix)
        em = discord.Embed(
            title=f"{title} Commands",
            description=text,
            color=discord.Color.dark_blue()
        )
        em.set_footer(text=f"Prefix: {self.prefix}  •  Select another category below")
        await interaction.response.edit_message(embed=em, view=self.view)

class HelpView(ui.View):
    def __init__(self, prefix: str):
        super().__init__(timeout=120)
        self.add_item(HelpSelect(prefix))

# ----------------------------- READY / TASKS ------------------------------

@bot.event
async def on_ready():
    global reminder_task_started, stay_vc_task_started
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    if not reminder_task_started:
        reminder_loop.start()
        reminder_task_started = True
    if not stay_vc_task_started:
        stay_vc_loop.start()
        stay_vc_task_started = True
    # Sync hybrid / slash commands
    try:
        synced = await bot.tree.sync()
        log.info("Synced %s application commands", len(synced))
    except Exception as e:
        log.warning("Slash command sync failed: %s", e)
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening, name=f"{DEFAULT_PREFIX}help | /help | Music + 24/7"))

@tasks.loop(seconds=20)
async def reminder_loop():
    now = time.time()
    rows = q("SELECT * FROM reminders WHERE sent=0 AND due_at<=?", (now,), "all")
    for row in rows:
        user = bot.get_user(row["user_id"])
        channel = bot.get_channel(row["channel_id"])
        try:
            target = channel or user
            if target:
                await target.send(f"⏰ Reminder: {row['message']}")
        except discord.HTTPException:
            pass
        q("UPDATE reminders SET sent=1 WHERE id=?", (row["id"],))

@reminder_loop.before_loop
async def before_reminders():
    await bot.wait_until_ready()

# ----------------------------- HELP / OWNER --------------------------------

@bot.hybrid_command(name="help", description="Show bot help and command categories")
async def help_command(ctx, category: Optional[str] = None):
    p = prefix_for(ctx.guild.id if ctx.guild else None)
    if category:
        key = category.lower()
        if key not in HELP_PAGES:
            return await ctx.send("Categories: " + ", ".join(HELP_PAGES.keys()))
        title, cmds = HELP_PAGES[key]
        em = discord.Embed(title=f"{title} Commands", description=cmds.format(p=p), color=discord.Color.dark_blue())
        return await ctx.send(embed=em)

    em = discord.Embed(
        title="🎵 Private Management Bot • Help",
        description=f"**Prefix:** `{p}` (purana + naya dono + @mention bhi chalega)\n"
                    f"**Slash:** `/help` bhi use kar sakte ho\n\n"
                    f"Neeche se category select karo — ya seedha type: `{p}help music` / `{p}help voice`",
        color=discord.Color.dark_blue()
    )
    em.set_footer(text="Owner-authorized • Music • 24/7 VC • Recording")
    view = HelpView(p)
    await ctx.send(embed=em, view=view)

@bot.hybrid_command(name="ping", description="Check bot latency")
@owner_only()
async def ping(ctx):
    await ctx.send(f"🏓 Pong! `{round(bot.latency * 1000)}ms`")

@bot.hybrid_command(name="uptime", description="Show bot uptime")
@owner_only()
async def uptime(ctx):
    seconds = int(time.monotonic() - bot.start_time)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    await ctx.send(f"⏱️ Uptime: **{days}d {hours}h {minutes}m {secs}s**")

@bot.command()
@owner_only()
async def stats(ctx):
    await send_embed(ctx, "Bot statistics",
      f"Servers: **{len(bot.guilds)}**\nCached users: **{len(bot.users)}**\n"
      f"Latency: **{bot.latency*1000:.0f} ms**\nPython: **{platform.python_version()}**\n"
      f"discord.py: **{discord.__version__}**")

@bot.command()
@owner_only()
async def system(ctx):
    mem = f"{psutil.virtual_memory().percent}%" if psutil else "psutil unavailable"
    cpu = f"{psutil.cpu_percent(interval=0.1)}%" if psutil else "unavailable"
    await send_embed(ctx, "System health",
      f"Platform: `{platform.platform()}`\nCPU: `{cpu}`\nMemory: `{mem}`\n"
      f"Database: `{DB_PATH}`\nGuilds: `{len(bot.guilds)}`")

@bot.command()
@owner_only()
async def guilds(ctx):
    lines = [f"`{g.id}` • {discord.utils.escape_markdown(g.name)} • {g.member_count} members"
             for g in bot.guilds]
    await ctx.send("\n".join(lines[:40]) or "No servers connected.")

@bot.hybrid_command(name="prefix", description="View or change the bot prefix (old prefix still works)")
@owner_only()
async def prefix(ctx, new_prefix: Optional[str] = None):
    if not ctx.guild:
        return await ctx.send(f"Current prefix: `{DEFAULT_PREFIX}`")
    cfg = get_cfg(ctx.guild.id)
    if not new_prefix:
        all_p = get_all_prefixes(ctx.guild.id)
        return await ctx.send(
            f"**Current prefix:** `{cfg.get('prefix') or DEFAULT_PREFIX}`\n"
            f"**Also works:** {', '.join(f'`{p}`' for p in all_p)} + @mention"
        )
    if len(new_prefix) > 5 or " " in new_prefix:
        return await ctx.send("Prefix must be 1–5 characters with no spaces.")
    old = cfg.get("prefix") or DEFAULT_PREFIX
    if old == new_prefix:
        return await ctx.send(f"Prefix already is `{new_prefix}`.")
    q("UPDATE guild_config SET previous_prefix=?, prefix=? WHERE guild_id=?",
      (old, new_prefix, ctx.guild.id))
    await ctx.send(
        f"✅ Prefix changed to `{new_prefix}`.\n"
        f"Purana prefix `{old}` ab bhi kaam karega + @mention bhi."
    )

@bot.command()
@owner_only()
async def maintenance(ctx, state: Optional[str] = None):
    if state not in ("on", "off"):
        return await ctx.send("Usage: `.maintenance on/off`")
    await ctx.send(f"Maintenance mode: **{state.upper()}**. This is an announcement flag; it does not disable Discord itself.")

@bot.command()
@owner_only()
async def shutdown(ctx):
    await ctx.send("Shutting down. Railway restart policy may start me again.")
    await bot.close()

@bot.command()
@guild_only()
@owner_only()
async def whitelist(ctx, member: discord.Member):
    q("INSERT OR IGNORE INTO whitelist(guild_id,user_id) VALUES(?,?)",
      (ctx.guild.id, member.id))
    await ctx.send(f"Added {member.mention} to the trusted list.")

@bot.command()
@guild_only()
@owner_only()
async def unwhitelist(ctx, member: discord.Member):
    q("DELETE FROM whitelist WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id))
    await ctx.send(f"Removed {member.mention} from the trusted list.")

# ----------------------------- MUSIC COMMANDS -----------------------------

@bot.hybrid_command(name="play", aliases=["p"], description="Play a song from YouTube URL or search name")
@guild_only()
async def play(ctx, *, query: str):
    """Play song from YouTube URL or search by name."""
    if yt_dlp is None:
        return await ctx.send("❌ `yt-dlp` install nahi hai. `pip install yt-dlp` karo.")

    vc = await ensure_voice(ctx)
    if not vc:
        return

    status = await ctx.send(f"🔍 Searching / loading: `{query[:80]}` ...")
    track = await extract_track_info(query, ctx.author)
    if not track or not track.get("url"):
        reason = LAST_EXTRACT_ERROR[:600] if LAST_EXTRACT_ERROR else "unknown"
        return await status.edit(content=f"❌ Song nahi mila ya stream extract fail ho gaya.\n```{reason}```")

    queue = get_queue(ctx.guild.id)
    if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused() or now_playing.get(ctx.guild.id)):
        queue.append(track)
        em = discord.Embed(
            title="➕ Added to Queue",
            description=f"**[{track['title']}]({track.get('webpage_url', '')})**",
            color=discord.Color.blue()
        )
        if track.get("thumbnail"):
            em.set_thumbnail(url=track["thumbnail"])
        em.add_field(name="Position", value=str(len(queue)), inline=True)
        em.add_field(name="Duration", value=format_duration(track.get("duration")), inline=True)
        em.set_footer(text=f"Requested by {ctx.author}")
        await status.edit(content=None, embed=em)
    else:
        queue.append(track)
        await status.edit(content=f"✅ Loading **{track['title'][:60]}** ...")
        await play_next(ctx.guild, ctx.channel)

@bot.hybrid_command(name="pause", description="Pause current track")
@guild_only()
async def pause(ctx):
    vc = ctx.voice_client
    if not vc or not vc.is_playing():
        return await ctx.send("Kuch play nahi ho raha.")
    vc.pause()
    await ctx.send("⏸️ Paused.")

@bot.hybrid_command(name="resume", description="Resume paused track")
@guild_only()
async def resume(ctx):
    vc = ctx.voice_client
    if not vc or not vc.is_paused():
        return await ctx.send("Paused nahi hai.")
    vc.resume()
    await ctx.send("▶️ Resumed.")

@bot.hybrid_command(name="skip", aliases=["s", "next"], description="Skip current track")
@guild_only()
async def skip(ctx):
    vc = ctx.voice_client
    if not vc or (not vc.is_playing() and not vc.is_paused()):
        return await ctx.send("Kuch play nahi ho raha.")
    vc.stop()
    await ctx.send("⏭️ Skipped.")

@bot.hybrid_command(name="stop", description="Stop music and clear queue (stays in VC if 24/7 enabled)")
@guild_only()
async def stop(ctx):
    gid = ctx.guild.id
    get_queue(gid).clear()
    now_playing.pop(gid, None)
    if gid in progress_tasks:
        progress_tasks[gid].cancel()
        progress_tasks.pop(gid, None)
    vc = ctx.voice_client
    stay = is_stay_vc_enabled(gid)
    if vc:
        vc.stop()
        if not stay:
            await vc.disconnect()
    if gid in np_messages:
        try:
            msg = "⏹️ Stopped." + (" (24/7 — still in VC)" if stay else "")
            await np_messages[gid].edit(content=msg, embed=None, view=None)
        except Exception:
            pass
        np_messages.pop(gid, None)
    await ctx.send("⏹️ Stopped & queue cleared." + (" Still in VC (24/7)." if stay else " Left voice."))

@bot.hybrid_command(name="queue", aliases=["q"], description="Show music queue")
@guild_only()
async def queue_cmd(ctx):
    qlist = list(get_queue(ctx.guild.id))
    track = now_playing.get(ctx.guild.id)
    em = discord.Embed(title="📜 Music Queue", color=discord.Color.blurple())
    if track:
        em.add_field(name="Now Playing", value=track.get("title", "?")[:70], inline=False)
    if not qlist:
        em.description = "Queue is empty."
    else:
        lines = [f"`{i}.` {t.get('title', '?')[:55]}" for i, t in enumerate(qlist[:15], 1)]
        if len(qlist) > 15:
            lines.append(f"... and {len(qlist)-15} more")
        em.description = "\n".join(lines)
    await ctx.send(embed=em)

@bot.hybrid_command(name="np", aliases=["nowplaying", "current"], description="Show now playing track")
@guild_only()
async def nowplaying(ctx):
    track = now_playing.get(ctx.guild.id)
    if not track:
        return await ctx.send("Abhi kuch play nahi ho raha.")
    start = track.get("start_time") or time.time()
    elapsed = time.time() - start
    vc = ctx.voice_client
    paused = vc.is_paused() if vc else False
    embed = create_np_embed(track, elapsed=elapsed, paused=paused)
    view = MusicControlView(ctx.guild.id)
    msg = await ctx.send(embed=embed, view=view)
    np_messages[ctx.guild.id] = msg

@bot.hybrid_command(name="volume", aliases=["vol"], description="Set or check volume (0-200)")
@guild_only()
async def volume(ctx, level: Optional[int] = None):
    if level is None:
        cur = int(volume_levels.get(ctx.guild.id, 1.0) * 100)
        return await ctx.send(f"🔊 Current volume: **{cur}%**")
    if not 0 <= level <= 200:
        return await ctx.send("Volume 0–200 ke beech me do.")
    volume_levels[ctx.guild.id] = level / 100.0
    vc = ctx.voice_client
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = level / 100.0
    await ctx.send(f"🔊 Volume set to **{level}%**")

@bot.hybrid_command(name="join", description="Join your voice channel")
@guild_only()
async def join(ctx):
    vc = await ensure_voice(ctx)
    if vc:
        await ctx.send(f"✅ Joined **{vc.channel.name}**")

@bot.hybrid_command(name="leave", aliases=["dc", "disconnect"], description="Force leave voice channel (overrides 24/7)")
@guild_only()
async def leave(ctx):
    vc = ctx.voice_client
    if not vc:
        return await ctx.send("Main kisi voice channel me nahi hoon.")
    get_queue(ctx.guild.id).clear()
    now_playing.pop(ctx.guild.id, None)
    if ctx.guild.id in progress_tasks:
        progress_tasks[ctx.guild.id].cancel()
    # Explicit leave always disconnects, even in 24/7 mode
    await vc.disconnect()
    await ctx.send("👋 Left the voice channel." + (" (24/7 was on — use `.247` to re-enable)" if is_stay_vc_enabled(ctx.guild.id) else ""))

# ----------------------------- 24/7 VC + RECORDING ------------------------

@bot.hybrid_command(name="247", aliases=["stay", "stayvc"], description="Bot ko 24/7 ek voice channel me rakho")
@guild_only()
@owner_only()
async def stay_vc_cmd(ctx, channel: Optional[discord.VoiceChannel] = None):
    """Enable/disable 24/7 stay in a voice channel.
    Usage: .247 #voice-channel   or   .247 off
    """
    cfg = get_cfg(ctx.guild.id)
    if channel is None:
        # Toggle off if currently on, otherwise ask for channel
        if cfg.get("stay_vc"):
            q("UPDATE guild_config SET stay_vc=0, stay_channel=NULL WHERE guild_id=?", (ctx.guild.id,))
            return await ctx.send("🔴 24/7 VC **OFF**. Bot ab normal leave karega.")
        return await ctx.send("Usage: `.247 <voice-channel>` to enable, or `.247` again to disable.")

    q("UPDATE guild_config SET stay_vc=1, stay_channel=? WHERE guild_id=?", (channel.id, ctx.guild.id))
    # Join immediately if possible
    try:
        cls = VoiceRecvClient if HAS_VOICE_RECV else discord.VoiceClient
        if ctx.voice_client:
            if ctx.voice_client.channel.id != channel.id:
                await ctx.voice_client.move_to(channel)
        else:
            await channel.connect(timeout=15.0, reconnect=True, cls=cls)
        await ctx.send(f"🟢 24/7 **ON** — Bot ab **{channel.name}** me continuously rahega.\n"
                       f"Music stop hone ke baad bhi nahi niklega. `.leave` se force nikal sakte ho.")
    except Exception as e:
        await ctx.send(f"Config saved but join failed: `{e}`. Bot task se auto-join karega.")

@tasks.loop(seconds=45)
async def stay_vc_loop():
    """Keep the bot in the configured 24/7 channel."""
    for guild in list(bot.guilds):
        try:
            if not is_stay_vc_enabled(guild.id):
                continue
            ch_id = get_stay_channel_id(guild.id)
            if not ch_id:
                continue
            channel = guild.get_channel(ch_id)
            if not channel or not isinstance(channel, discord.VoiceChannel):
                continue
            vc = guild.voice_client
            if vc and vc.is_connected() and vc.channel and vc.channel.id == ch_id:
                continue
            # Need to join / rejoin
            try:
                cls = VoiceRecvClient if HAS_VOICE_RECV else discord.VoiceClient
                if vc and vc.is_connected():
                    await vc.move_to(channel)
                else:
                    await channel.connect(timeout=20.0, reconnect=True, cls=cls)
                log.info("24/7 re-joined %s in guild %s", channel.name, guild.id)
            except Exception as e:
                log.warning("24/7 join failed for guild %s: %s", guild.id, e)
        except Exception:
            continue

@stay_vc_loop.before_loop
async def before_stay_vc():
    await bot.wait_until_ready()

# ----- Voice Recording helpers & commands -----

PCM_BYTES_PER_SEC = 48000 * 2 * 2  # 48kHz, 16-bit, stereo

def mix_pcm(buffers: List[bytes]) -> bytes:
    """Mix several equal-format PCM tracks into one (sum + clip)."""
    bufs = [bytes(b) for b in buffers if b]
    if not bufs:
        return b""
    n = max(len(b) for b in bufs)
    n -= n % 4
    bufs = [b[:n].ljust(n, b"\x00") for b in bufs]
    if len(bufs) == 1:
        return bufs[0]
    try:
        import numpy as np
        acc = np.zeros(n // 2, dtype=np.int32)
        for b in bufs:
            acc += np.frombuffer(b, dtype="<i2")
        return np.clip(acc, -32768, 32767).astype("<i2").tobytes()
    except ImportError:
        pass
    try:
        import audioop  # removed in Python 3.13
        out = bufs[0]
        for b in bufs[1:]:
            out = audioop.add(out, b, 2)
        return out
    except ImportError:
        # No mixer available: keep the longest single speaker rather than garbage.
        return max(bufs, key=len)

class WaveRecordingSink(AudioSink):
    """Per-user PCM recorder (time-aligned) for discord-ext-voice-recv."""
    def __init__(self):
        if HAS_VOICE_RECV:
            super().__init__()
        self.user_data: Dict[int, bytearray] = {}
        self.start_time = time.time()
        self._lock = threading.Lock()

    def wants_opus(self) -> bool:
        return False  # we want decoded PCM

    def write(self, user, data):
        pcm = getattr(data, "pcm", None)
        if not pcm:
            return
        uid = getattr(user, "id", 0) or 0
        expected = int((time.time() - self.start_time) * PCM_BYTES_PER_SEC)
        expected -= expected % 4
        with self._lock:
            buf = self.user_data.setdefault(uid, bytearray())
            # If this user was silent for >100ms, pad silence so tracks stay in sync.
            if expected - len(buf) > PCM_BYTES_PER_SEC // 10:
                buf.extend(b"\x00" * (expected - len(buf)))
            buf.extend(pcm)

    @property
    def has_audio(self) -> bool:
        with self._lock:
            return any(len(b) for b in self.user_data.values())

    def mixed_pcm(self) -> bytes:
        with self._lock:
            tracks = [bytes(b) for b in self.user_data.values()]
        return mix_pcm(tracks)

    def cleanup(self):
        pass

def _write_wav(path: Path, pcm: bytes, sample_rate: int = 48000, channels: int = 2):
    import wave
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)

@bot.hybrid_command(name="record", description="Start / stop voice channel recording")
@guild_only()
@owner_only()
async def record_cmd(ctx, action: str = "start", *, title: Optional[str] = None):
    """Start or stop recording the current voice channel.
    Usage: .record start [title]   |   .record stop
    """
    action = action.lower().strip()
    gid = ctx.guild.id

    if action in ("start", "on", "begin"):
        if not HAS_VOICE_RECV:
            return await ctx.send(
                "❌ Voice recording ke liye `discord-ext-voice-recv` package chahiye.\n"
                "Install: `pip install discord-ext-voice-recv`\n"
                "Uske baad bot restart karo."
            )
        if gid in active_recordings:
            return await ctx.send("Is server pe pehle se recording chal rahi hai. Pehle `.record stop` karo.")
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send("Pehle voice channel me join karo.")

        channel = ctx.author.voice.channel
        vc = ctx.voice_client
        try:
            if vc is None or not isinstance(vc, VoiceRecvClient):
                if vc:
                    await vc.disconnect()
                vc = await channel.connect(timeout=15.0, reconnect=True, cls=VoiceRecvClient)
            sink = WaveRecordingSink()
            vc.listen(sink)
            active_recordings[gid] = {
                "sink": sink,
                "channel_id": channel.id,
                "started_by": ctx.author.id,
                "start_time": time.time(),
                "title": (title or f"Recording {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")[:100],
                "vc": vc,
            }
            await ctx.send(f"🔴 **Recording started** in **{channel.name}**\nTitle: `{active_recordings[gid]['title']}`\nStop: `.record stop`")
        except Exception as e:
            log.exception("Record start failed")
            await ctx.send(f"❌ Recording start nahi ho payi: `{e}`")

    elif action in ("stop", "off", "end"):
        info = active_recordings.pop(gid, None)
        if not info:
            return await ctx.send("Koi active recording nahi hai.")
        sink: WaveRecordingSink = info["sink"]
        duration = time.time() - info["start_time"]
        try:
            # Stop listening
            vc = info.get("vc") or ctx.voice_client
            if vc and hasattr(vc, "stop_listening"):
                vc.stop_listening()
        except Exception:
            pass

        if not sink.has_audio:
            return await ctx.send(
                "Recording empty thi (kisi ne speak nahi kiya?).\n"
                "Agar log bol rahe the phir bhi empty hai to `pip install -U discord-ext-voice-recv discord.py[voice]` "
                "karke redeploy karo — Discord voice encryption update ke baad purane versions audio receive nahi kar paate."
            )

        fname = f"{gid}_{int(time.time())}.wav"
        path = RECORDINGS_DIR / fname
        try:
            pcm = await asyncio.to_thread(sink.mixed_pcm)
            await asyncio.to_thread(_write_wav, path, pcm)
            size = path.stat().st_size
            rid = q(
                """INSERT INTO recordings(guild_id,channel_id,started_by,filename,title,duration,created_at,file_size)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (gid, info["channel_id"], info["started_by"], fname,
                 info["title"], duration, utcnow(), size)
            )
            await ctx.send(
                f"⏹️ **Recording saved**\n"
                f"ID: `{rid}` | Duration: `{format_duration(duration)}` | Size: `{size//1024} KB`\n"
                f"Sunne ke liye: `.recordings` ya `.playrec {rid}`"
            )
        except Exception as e:
            log.exception("Save recording failed")
            await ctx.send(f"❌ Save fail: `{e}`")
    else:
        await ctx.send("Usage: `.record start [title]`  ya  `.record stop`")

@bot.hybrid_command(name="recordings", aliases=["reclist", "rec"], description="List saved voice recordings")
@guild_only()
async def recordings_list(ctx, limit: int = 15):
    rows = q(
        "SELECT * FROM recordings WHERE guild_id=? ORDER BY id DESC LIMIT ?",
        (ctx.guild.id, min(max(limit, 1), 25)), "all"
    )
    if not rows:
        return await ctx.send("Is server pe koi recording save nahi hai.")
    lines = []
    for r in rows:
        lines.append(
            f"`#{r['id']}` **{r['title'][:40]}** — {format_duration(r['duration'] or 0)} "
            f"• <t:{int(dt.datetime.fromisoformat(r['created_at']).timestamp())}:R>"
        )
    em = discord.Embed(title="🎙️ Saved Recordings", description="\n".join(lines), color=discord.Color.dark_red())
    em.set_footer(text="Play: .playrec <id>   |   Delete: .delrec <id>")
    await ctx.send(embed=em)

@bot.hybrid_command(name="playrec", aliases=["playrecording"], description="Play a saved recording in voice")
@guild_only()
async def play_recording(ctx, recording_id: int):
    row = q("SELECT * FROM recordings WHERE id=? AND guild_id=?", (recording_id, ctx.guild.id), "one")
    if not row:
        return await ctx.send("Recording nahi mili.")
    path = RECORDINGS_DIR / row["filename"]
    if not path.exists():
        return await ctx.send("File missing hai disk se.")
    vc = await ensure_voice(ctx)
    if not vc:
        return
    # Stop current music if any
    if vc.is_playing() or vc.is_paused():
        vc.stop()
    try:
        source = discord.FFmpegPCMAudio(str(path), options="-vn")
        vc.play(source)
        await ctx.send(f"▶️ Playing recording **#{row['id']}** — {row['title'][:50]}")
    except Exception as e:
        await ctx.send(f"❌ Play fail: `{e}`")

@bot.hybrid_command(name="delrec", description="Delete a saved recording")
@guild_only()
@owner_only()
async def delete_recording(ctx, recording_id: int):
    row = q("SELECT * FROM recordings WHERE id=? AND guild_id=?", (recording_id, ctx.guild.id), "one")
    if not row:
        return await ctx.send("Recording nahi mili.")
    path = RECORDINGS_DIR / row["filename"]
    q("DELETE FROM recordings WHERE id=?", (recording_id,))
    if path.exists():
        try:
            path.unlink()
        except Exception:
            pass
    await ctx.send(f"🗑️ Recording `#{recording_id}` delete ho gayi.")

# ----------------------------- MODERATION ----------------------------------

@bot.command()
@guild_only()
@owner_only()
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason=None):
    if not await ensure_manageable(ctx, member): return
    await member.ban(reason=safe_reason(reason), delete_message_seconds=0)
    log_action(ctx.guild.id, ctx.author.id, "ban", member.id, reason)
    await ctx.send(f"🔨 Banned **{member}**.")
    await send_log(ctx.guild, "Member banned", f"{member} (`{member.id}`)\nBy: {ctx.author}\nReason: {safe_reason(reason)}")

@bot.command()
@guild_only()
@owner_only()
@commands.has_permissions(ban_members=True)
async def unban(ctx, user_id: int, *, reason=None):
    user = await bot.fetch_user(user_id)
    await ctx.guild.unban(user, reason=safe_reason(reason))
    log_action(ctx.guild.id, ctx.author.id, "unban", user.id, reason)
    await ctx.send(f"Unbanned **{user}**.")

@bot.command()
@guild_only()
@owner_only()
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason=None):
    if not await ensure_manageable(ctx, member): return
    await member.kick(reason=safe_reason(reason))
    log_action(ctx.guild.id, ctx.author.id, "kick", member.id, reason)
    await ctx.send(f"👢 Kicked **{member}**.")
    await send_log(ctx.guild, "Member kicked", f"{member} (`{member.id}`)\nBy: {ctx.author}\nReason: {safe_reason(reason)}")

@bot.command()
@guild_only()
@owner_only()
@commands.has_permissions(moderate_members=True)
async def timeout(ctx, member: discord.Member, duration: str, *, reason=None):
    if not await ensure_manageable(ctx, member): return
    match = re.fullmatch(r"(\d+)(s|m|h|d)", duration.lower())
    if not match: return await ctx.send("Duration example: `10m`, `2h`, `1d`.")
    amount, unit = int(match.group(1)), match.group(2)
    seconds = amount * {"s":1, "m":60, "h":3600, "d":86400}[unit]
    if seconds > 28 * 86400: return await ctx.send("Maximum timeout is 28 days.")
    await member.timeout(dt.timedelta(seconds=seconds), reason=safe_reason(reason))
    await ctx.send(f"⏳ Timed out {member} for {duration}.")

@bot.command()
@guild_only()
@owner_only()
@commands.has_permissions(moderate_members=True)
async def untimeout(ctx, member: discord.Member, *, reason=None):
    await member.timeout(None, reason=safe_reason(reason))
    await ctx.send(f"Timeout removed for {member}.")

@bot.command()
@guild_only()
@owner_only()
@commands.has_permissions(moderate_members=True)
async def warn(ctx, member: discord.Member, *, reason="No reason provided"):
    rowid = q("""INSERT INTO warnings(guild_id,user_id,moderator_id,reason,created_at)
      VALUES(?,?,?,?,?)""", (ctx.guild.id, member.id, ctx.author.id, safe_reason(reason), utcnow()))
    log_action(ctx.guild.id, ctx.author.id, "warn", member.id, reason)
    await ctx.send(f"⚠️ Warned {member.mention}. Warning ID: `{rowid}`")
    await send_log(ctx.guild, "Member warned", f"{member} • {safe_reason(reason)}")

@bot.command()
@guild_only()
async def warnings(ctx, member: discord.Member):
    rows = q("SELECT * FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
             (ctx.guild.id, member.id), "all")
    if not rows: return await ctx.send("No warnings found.")
    await ctx.send("\n".join(f"`#{r['id']}` {r['reason']} • <t:{int(dt.datetime.fromisoformat(r['created_at']).timestamp())}:R>" for r in rows))

@bot.command()
@guild_only()
@owner_only()
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int):
    if not 1 <= amount <= 100: return await ctx.send("Choose 1–100 messages.")
    deleted = await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(f"Deleted {max(0, len(deleted)-1)} messages.")
    await asyncio.sleep(4)
    try: await msg.delete()
    except discord.HTTPException: pass

@bot.command()
@guild_only()
@owner_only()
@commands.has_permissions(manage_channels=True)
async def slowmode(ctx, seconds: int):
    if not 0 <= seconds <= 21600: return await ctx.send("Slowmode must be 0–21600 seconds.")
    await ctx.channel.edit(slowmode_delay=seconds)
    await ctx.send(f"Slowmode set to {seconds}s.")

@bot.command()
@guild_only()
@owner_only()
@commands.has_permissions(manage_channels=True)
async def lock(ctx):
    ow = ctx.channel.overwrites_for(ctx.guild.default_role)
    ow.send_messages = False
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=ow)
    await ctx.send("🔒 Channel locked.")

@bot.command()
@guild_only()
@owner_only()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx):
    ow = ctx.channel.overwrites_for(ctx.guild.default_role)
    ow.send_messages = None
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=ow)
    await ctx.send("🔓 Channel unlocked.")

# ----------------------------- LOGGING ------------------------------------

@bot.command()
@guild_only()
@owner_only()
async def setlogs(ctx, channel: discord.TextChannel):
    q("UPDATE guild_config SET log_channel=? WHERE guild_id=?", (channel.id, ctx.guild.id))
    await ctx.send(f"Logs will be sent to {channel.mention}.")

@bot.command()
@guild_only()
@owner_only()
async def modlogs(ctx, limit: int = 10):
    rows = q("SELECT * FROM mod_logs WHERE guild_id=? ORDER BY id DESC LIMIT ?",
             (ctx.guild.id, min(max(limit, 1), 20)), "all")
    if not rows: return await ctx.send("No moderation logs.")
    await ctx.send("\n".join(f"`{r['action']}` target `{r['target_id']}` by `{r['actor_id']}` — {r['reason']}" for r in rows))

@bot.event
async def on_message_delete(message):
    if message.guild and not message.author.bot:
        content = message.content[:900] or "[No cached text]"
        await send_log(message.guild, "Message deleted",
          f"Author: {message.author} (`{message.author.id}`)\nChannel: {message.channel.mention}\nContent: {discord.utils.escape_markdown(content)}")

@bot.event
async def on_message_edit(before, after):
    if before.guild and not before.author.bot and before.content != after.content:
        await send_log(before.guild, "Message edited",
          f"Author: {before.author}\nChannel: {before.channel.mention}\nBefore: {before.content[:700]}\nAfter: {after.content[:700]}")

@bot.event
async def on_member_join(member):
    cfg = get_cfg(member.guild.id)
    if cfg.get("autorole"):
        role = member.guild.get_role(cfg["autorole"])
        if role:
            try: await member.add_roles(role, reason="Configured autorole")
            except discord.HTTPException: pass
    if cfg.get("welcome_channel"):
        ch = member.guild.get_channel(cfg["welcome_channel"])
        if ch:
            await ch.send(f"Welcome {member.mention} to **{member.guild.name}**!")
    await send_log(member.guild, "Member joined", f"{member} (`{member.id}`)")

@bot.event
async def on_member_remove(member):
    cfg = get_cfg(member.guild.id)
    if cfg.get("goodbye_channel"):
        ch = member.guild.get_channel(cfg["goodbye_channel"])
        if ch:
            await ch.send(f"**{member}** left the server.")
    await send_log(member.guild, "Member left", f"{member} (`{member.id}`)")

@bot.event
async def on_member_update(before, after):
    if before.nick != after.nick:
        await send_log(after.guild, "Nickname updated", f"{before} → {after.nick or after.name}")

# ----------------------------- SERVER TOOLS -------------------------------

@bot.command()
@guild_only()
async def serverinfo(ctx):
    g = ctx.guild
    em = discord.Embed(title=g.name, color=discord.Color.blurple())
    em.add_field(name="ID", value=g.id)
    em.add_field(name="Members", value=g.member_count)
    em.add_field(name="Channels", value=len(g.channels))
    em.add_field(name="Roles", value=len(g.roles))
    em.add_field(name="Created", value=f"<t:{int(g.created_at.timestamp())}:D>")
    if g.icon: em.set_thumbnail(url=g.icon.url)
    await ctx.send(embed=em)

@bot.command()
@guild_only()
async def userinfo(ctx, member: Optional[discord.Member] = None):
    m = member or ctx.author
    em = discord.Embed(title=str(m), color=discord.Color.blurple())
    em.add_field(name="ID", value=m.id)
    em.add_field(name="Joined", value=f"<t:{int(m.joined_at.timestamp())}:R>" if m.joined_at else "Unknown")
    em.add_field(name="Created", value=f"<t:{int(m.created_at.timestamp())}:D>")
    em.add_field(name="Top role", value=m.top_role.mention)
    em.set_thumbnail(url=m.display_avatar.url)
    await ctx.send(embed=em)

@bot.command()
async def avatar(ctx, member: Optional[discord.Member] = None):
    m = member or ctx.author
    await ctx.send(m.display_avatar.url)

@bot.command()
@guild_only()
async def membercount(ctx):
    await ctx.send(f"Members: **{ctx.guild.member_count}**")

@bot.command()
@guild_only()
@owner_only()
@commands.has_permissions(manage_channels=True)
async def createchannel(ctx, *, name: str):
    ch = await ctx.guild.create_text_channel(name[:90], reason=f"Requested by {ctx.author}")
    await ctx.send(f"Created {ch.mention}.")

@bot.command()
@guild_only()
@commands.has_permissions(manage_roles=True)
async def createrole(ctx, *, name: str):
    role = await ctx.guild.create_role(name=name[:90], reason=f"Requested by {ctx.author}")
    await ctx.send(f"Created {role.mention}.")

@bot.command()
@guild_only()
async def roleinfo(ctx, role: discord.Role):
    await send_embed(ctx, role.name,
      f"ID: `{role.id}`\nMembers: `{len(role.members)}`\n"
      f"Position: `{role.position}`\nMentionable: `{role.mentionable}`")

@bot.command()
@guild_only()
async def channelinfo(ctx, channel: Optional[discord.TextChannel] = None):
    ch = channel or ctx.channel
    await send_embed(ctx, ch.name, f"ID: `{ch.id}`\nType: `{ch.type}`\nCategory: `{ch.category}`")

# ----------------------------- ANTINUKE -----------------------------------

async def trusted_actor(guild_id: int, user_id: int) -> bool:
    if is_owner_id(user_id):
        return True
    return q("SELECT 1 FROM whitelist WHERE guild_id=? AND user_id=?",
             (guild_id, user_id), "one") is not None

async def check_antinuke(guild: discord.Guild, action: str):
    cfg = get_cfg(guild.id)
    if not cfg.get("antinuke"):
        return
    try:
        async for entry in guild.audit_logs(limit=5):
            if entry.target is None or entry.user is None:
                continue
            age = (dt.datetime.now(dt.timezone.utc) - entry.created_at).total_seconds()
            if age > 12:
                continue
            actor = entry.user
            if await trusted_actor(guild.id, actor.id) or actor.id == bot.user.id:
                return
            key = (guild.id, str(actor.id))
            now = time.time()
            events = [x for x in anti_nuke_events.get(key, []) if now - x < 15]
            events.append(now)
            anti_nuke_events[key] = events
            limit = max(2, int(cfg.get("antinuke_limit") or 3))
            if len(events) >= limit:
                member = guild.get_member(actor.id)
                if member and member != guild.owner and guild.me.top_role > member.top_role:
                    try:
                        await member.timeout(dt.timedelta(hours=1), reason="Anti-nuke suspicious activity")
                    except discord.HTTPException:
                        pass
                await send_log(guild, "🚨 Anti-Nuke alert",
                    f"Actor: {actor} (`{actor.id}`)\nAction: `{action}`\n"
                    f"Recent suspicious actions: `{len(events)}`\nAutomatic timeout attempted where possible.")
                anti_nuke_events[key] = []
            return
    except (discord.Forbidden, discord.HTTPException):
        log.warning("Cannot read audit logs in %s", guild.id)

@bot.command()
@guild_only()
@owner_only()
async def antinuke(ctx, state: Optional[str] = None, limit: Optional[int] = None):
    cfg = get_cfg(ctx.guild.id)
    if state is None:
        return await ctx.send(f"Anti-nuke: **{'ON' if cfg['antinuke'] else 'OFF'}** | threshold: `{cfg['antinuke_limit']}`")
    if state.lower() in ("on", "enable"):
        q("UPDATE guild_config SET antinuke=1 WHERE guild_id=?", (ctx.guild.id,))
    elif state.lower() in ("off", "disable"):
        q("UPDATE guild_config SET antinuke=0 WHERE guild_id=?", (ctx.guild.id,))
    elif state.lower() == "limit":
        if not limit or not 2 <= limit <= 10:
            return await ctx.send("Usage: `.antinuke limit 2-10`")
        q("UPDATE guild_config SET antinuke_limit=? WHERE guild_id=?", (limit, ctx.guild.id))
    else:
        return await ctx.send("Usage: `.antinuke on/off` or `.antinuke limit 2-10`")
    await ctx.send("Anti-nuke configuration updated.")

@bot.event
async def on_guild_channel_delete(channel):
    await check_antinuke(channel.guild, "channel_delete")

@bot.event
async def on_guild_role_delete(role):
    await check_antinuke(role.guild, "role_delete")

@bot.event
async def on_member_ban(guild, user):
    await check_antinuke(guild, "member_ban")

@bot.event
async def on_guild_channel_create(channel):
    await check_antinuke(channel.guild, "channel_create")

@bot.event
async def on_guild_role_create(role):
    await check_antinuke(role.guild, "role_create")

# ----------------------------- AUTOMOD -------------------------------------

@bot.command()
@guild_only()
@owner_only()
async def automod(ctx, state: Optional[str] = None):
    cfg = get_cfg(ctx.guild.id)
    if state is None:
        return await ctx.send(f"AutoMod: **{'ON' if cfg['automod'] else 'OFF'}**")
    if state.lower() not in ("on", "off"):
        return await ctx.send("Usage: `.automod on/off`")
    q("UPDATE guild_config SET automod=? WHERE guild_id=?",
      (1 if state.lower() == "on" else 0, ctx.guild.id))
    await ctx.send(f"AutoMod {state.upper()}.")

@bot.command()
@guild_only()
@owner_only()
async def badword(ctx, action: str, *, word: str):
    cfg = get_cfg(ctx.guild.id)
    words = json.loads(cfg["bad_words"] or "[]")
    if action.lower() == "add":
        if word.lower() not in words: words.append(word.lower())
    elif action.lower() == "remove":
        words = [w for w in words if w != word.lower()]
    else:
        return await ctx.send("Usage: `.badword add/remove <word>`")
    q("UPDATE guild_config SET bad_words=? WHERE guild_id=?",
      (json.dumps(words), ctx.guild.id))
    await ctx.send("Bad-word filter updated.")

@bot.command()
@guild_only()
@owner_only()
async def linkfilter(ctx, state: str):
    if state.lower() not in ("on", "off"): return await ctx.send("Use on/off.")
    q("UPDATE guild_config SET blocked_links=? WHERE guild_id=?",
      (int(state.lower()=="on"), ctx.guild.id))
    await ctx.send(f"Link filter {state.upper()}.")

@bot.command()
@guild_only()
@owner_only()
async def invitefilter(ctx, state: str):
    if state.lower() not in ("on", "off"): return await ctx.send("Use on/off.")
    q("UPDATE guild_config SET invite_filter=? WHERE guild_id=?",
      (int(state.lower()=="on"), ctx.guild.id))
    await ctx.send(f"Invite filter {state.upper()}.")

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.guild:
        cfg = get_cfg(message.guild.id)
        if cfg["automod"] and not await trusted_actor(message.guild.id, message.author.id):
            content = message.content.lower()
            words = json.loads(cfg["bad_words"] or "[]")
            reason = None
            if any(w and w in content for w in words):
                reason = "Blocked word"
            elif cfg["invite_filter"] and re.search(r"(discord\.gg/|discord\.com/invite/)", content):
                reason = "Discord invite link"
            elif cfg["blocked_links"] and re.search(r"https?://", content):
                reason = "Link not allowed"
            if reason:
                try:
                    await message.delete()
                    await message.channel.send(f"{message.author.mention}, message removed: {reason}.", delete_after=5)
                    await send_log(message.guild, "AutoMod action", f"{message.author} • {reason}")
                except discord.HTTPException:
                    pass
                return
        # Basic exact-trigger auto responses.
        row = q("SELECT response FROM autoresponses WHERE guild_id=? AND trigger=?",
                (message.guild.id, message.content.lower()), "one")
        if row:
            await message.channel.send(row["response"][:1800])
    await bot.process_commands(message)

@bot.command()
@guild_only()
@owner_only()
async def autoresponse(ctx, action: str, trigger: Optional[str] = None, *, response: Optional[str] = None):
    if action == "add":
        if not trigger or not response: return await ctx.send("Usage: `.autoresponse add trigger | response` (use quoted arguments or command help).")
        q("INSERT OR REPLACE INTO autoresponses(guild_id,trigger,response) VALUES(?,?,?)",
          (ctx.guild.id, trigger.lower(), response))
        await ctx.send("Auto-response saved.")
    elif action == "remove":
        if not trigger: return await ctx.send("Provide a trigger.")
        q("DELETE FROM autoresponses WHERE guild_id=? AND trigger=?", (ctx.guild.id, trigger.lower()))
        await ctx.send("Auto-response removed.")
    elif action == "list":
        rows = q("SELECT trigger FROM autoresponses WHERE guild_id=?", (ctx.guild.id,), "all")
        await ctx.send(", ".join(r["trigger"] for r in rows) or "No auto-responses.")
    else:
        await ctx.send("Usage: `.autoresponse add/remove/list ...`")

# ----------------------------- WELCOME / STICKY ---------------------------

@bot.command()
@guild_only()
@owner_only()
async def setwelcome(ctx, channel: discord.TextChannel):
    q("UPDATE guild_config SET welcome_channel=? WHERE guild_id=?", (channel.id, ctx.guild.id))
    await ctx.send(f"Welcome channel set to {channel.mention}.")

@bot.command()
@guild_only()
@owner_only()
async def setgoodbye(ctx, channel: discord.TextChannel):
    q("UPDATE guild_config SET goodbye_channel=? WHERE guild_id=?", (channel.id, ctx.guild.id))
    await ctx.send(f"Goodbye channel set to {channel.mention}.")

@bot.command()
@guild_only()
@owner_only()
async def setautorole(ctx, role: discord.Role):
    if role >= ctx.guild.me.top_role:
        return await ctx.send("Move my bot role above the autorole.")
    q("UPDATE guild_config SET autorole=? WHERE guild_id=?", (role.id, ctx.guild.id))
    await ctx.send(f"Autorole set to {role.mention}.")

@bot.command()
@guild_only()
@owner_only()
async def sticky(ctx, *, content: str):
    sent = await ctx.send(content[:1800])
    q("INSERT OR REPLACE INTO sticky_messages(channel_id,content,message_id) VALUES(?,?,?)",
      (ctx.channel.id, content[:1800], sent.id))
    await ctx.send("Sticky message saved. Re-posting is best-effort in this starter build.")

# ----------------------------- BACKUP / RECOVERY --------------------------

def make_guild_snapshot(guild: discord.Guild):
    data = {
      "guild_id": guild.id, "guild_name": guild.name, "created_at": utcnow(),
      "roles": [], "categories": [], "channels": []
    }
    for role in guild.roles:
        if role.is_default() or role.managed:
            continue
        data["roles"].append({
          "name": role.name, "permissions": role.permissions.value,
          "color": role.color.value, "hoist": role.hoist,
          "mentionable": role.mentionable, "position": role.position
        })
    for cat in guild.categories:
        data["categories"].append({"name": cat.name, "position": cat.position})
    for ch in guild.channels:
        if isinstance(ch, discord.TextChannel):
            data["channels"].append({"type":"text","name":ch.name,
              "category":ch.category.name if ch.category else None,
              "topic":ch.topic,"slowmode":ch.slowmode_delay,"nsfw":ch.nsfw})
        elif isinstance(ch, discord.VoiceChannel):
            data["channels"].append({"type":"voice","name":ch.name,
              "category":ch.category.name if ch.category else None,
              "bitrate":ch.bitrate,"user_limit":ch.user_limit})
    return data

@bot.group(name="backup", invoke_without_command=True)
@guild_only()
@owner_only()
async def backup_group(ctx):
    await ctx.send("Usage: `.backup create`, `.backup list`, `.backup restore <id>`")

@backup_group.command(name="create")
async def backup_create(ctx):
    data = make_guild_snapshot(ctx.guild)
    filename = f"{ctx.guild.id}_{int(time.time())}.json"
    path = BACKUP_DIR / filename
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    q("INSERT INTO backups(guild_id,filename,created_at) VALUES(?,?,?)",
      (ctx.guild.id, filename, utcnow()))
    await ctx.send(f"Backup created: `{filename}`\nRoles: {len(data['roles'])} | Channels: {len(data['channels'])}")

@backup_group.command(name="list")
async def backup_list(ctx):
    rows = q("SELECT * FROM backups WHERE guild_id=? ORDER BY id DESC LIMIT 15",
             (ctx.guild.id,), "all")
    await ctx.send("\n".join(f"`{r['id']}` • `{r['filename']}` • {r['created_at']}" for r in rows)
                   or "No backups found.")

@backup_group.command(name="restore")
async def backup_restore(ctx, backup_id: int):
    row = q("SELECT * FROM backups WHERE id=? AND guild_id=?", (backup_id, ctx.guild.id), "one")
    if not row: return await ctx.send("Backup not found.")
    path = BACKUP_DIR / row["filename"]
    if not path.exists(): return await ctx.send("Backup file is missing.")
    data = json.loads(path.read_text(encoding="utf-8"))
    # Safe, non-destructive restore: only recreate missing roles/categories/channels.
    if not await ctx.send("Restore snapshot? This only recreates missing roles/categories/channels. Type `CONFIRM` within 20 seconds."):
        return
    def check(m):
        return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id and m.content == "CONFIRM"
    try:
        await bot.wait_for("message", timeout=20, check=check)
    except asyncio.TimeoutError:
        return await ctx.send("Restore cancelled.")
    existing_roles = {r.name for r in ctx.guild.roles}
    for r in data.get("roles", []):
        if r["name"] not in existing_roles:
            try:
                await ctx.guild.create_role(name=r["name"],
                    permissions=discord.Permissions(r["permissions"]),
                    colour=discord.Colour(r["color"]), hoist=r["hoist"],
                    mentionable=r["mentionable"], reason="Snapshot restore")
            except discord.HTTPException:
                pass
    existing_cats = {c.name for c in ctx.guild.categories}
    for c in data.get("categories", []):
        if c["name"] not in existing_cats:
            try: await ctx.guild.create_category(c["name"], reason="Snapshot restore")
            except discord.HTTPException: pass
    existing_channels = {c.name for c in ctx.guild.channels}
    for ch in data.get("channels", []):
        if ch["name"] in existing_channels: continue
        category = discord.utils.get(ctx.guild.categories, name=ch.get("category")) if ch.get("category") else None
        try:
            if ch["type"] == "text":
                await ctx.guild.create_text_channel(ch["name"], category=category,
                    topic=ch.get("topic"), slowmode_delay=ch.get("slowmode",0), nsfw=ch.get("nsfw",False))
            elif ch["type"] == "voice":
                await ctx.guild.create_voice_channel(ch["name"], category=category,
                    bitrate=ch.get("bitrate"), user_limit=ch.get("user_limit",0))
        except discord.HTTPException:
            pass
    await ctx.send("Restore attempt finished. Check server structure and permissions manually.")

# ----------------------------- PERSONAL TOOLS -----------------------------

@bot.group(name="note", invoke_without_command=True)
@owner_only()
async def note_group(ctx):
    await ctx.send("Usage: `.note add <text>`, `.note list`, `.note delete <id>`")

@note_group.command(name="add")
async def note_add(ctx, *, text_value: str):
    note_id = q("INSERT INTO notes(user_id,note,created_at) VALUES(?,?,?)",
                (ctx.author.id, text_value[:1500], utcnow()))
    await ctx.send(f"Note saved. ID: `{note_id}`")

@note_group.command(name="list")
async def note_list(ctx):
    rows = q("SELECT * FROM notes WHERE user_id=? ORDER BY id DESC LIMIT 15",
             (ctx.author.id,), "all")
    await ctx.send("\n".join(f"`{r['id']}` • {r['note']}" for r in rows) or "No notes.")

@note_group.command(name="delete")
async def note_delete(ctx, note_id: int):
    q("DELETE FROM notes WHERE id=? AND user_id=?", (note_id, ctx.author.id))
    await ctx.send("If that note existed, it has been deleted.")

@bot.group(name="todo", invoke_without_command=True)
@owner_only()
async def todo_group(ctx):
    await ctx.send("Usage: `.todo add <task>`, `.todo list`, `.todo done <id>`")

@todo_group.command(name="add")
async def todo_add(ctx, *, task: str):
    tid = q("INSERT INTO todos(user_id,task,created_at) VALUES(?,?,?)",
            (ctx.author.id, task[:500], utcnow()))
    await ctx.send(f"Task added. ID: `{tid}`")

@todo_group.command(name="list")
async def todo_list(ctx):
    rows = q("SELECT * FROM todos WHERE user_id=? ORDER BY done,id DESC LIMIT 20",
             (ctx.author.id,), "all")
    await ctx.send("\n".join(f"`{r['id']}` {'✅' if r['done'] else '▫️'} {r['task']}" for r in rows)
                   or "No tasks.")

@todo_group.command(name="done")
async def todo_done(ctx, task_id: int):
    q("UPDATE todos SET done=1 WHERE id=? AND user_id=?", (task_id, ctx.author.id))
    await ctx.send("Task marked complete if it existed.")

@bot.command()
@owner_only()
async def remind(ctx, duration: str, *, message: str):
    match = re.fullmatch(r"(\d+)(s|m|h|d)", duration.lower())
    if not match: return await ctx.send("Example: `.remind 30m check scrim result`")
    amount, unit = int(match.group(1)), match.group(2)
    seconds = amount * {"s":1, "m":60, "h":3600, "d":86400}[unit]
    if seconds > 30 * 86400: return await ctx.send("Reminder maximum is 30 days.")
    rid = q("INSERT INTO reminders(user_id,channel_id,message,due_at) VALUES(?,?,?,?)",
            (ctx.author.id, ctx.channel.id, message[:1000], time.time()+seconds))
    await ctx.send(f"Reminder saved. ID `{rid}` — in {duration}.")

@bot.command()
@owner_only()
async def password(ctx, length: int = 20):
    if not 8 <= length <= 64: return await ctx.send("Length must be 8–64.")
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    value = "".join(secrets.choice(alphabet) for _ in range(length))
    await ctx.author.send(f"Your generated password: `{value}`")
    await ctx.send("Password sent to your DMs. Delete the DM after saving it securely.")

@bot.command()
@owner_only()
async def calculate(ctx, *, expression: str):
    # Deliberately supports only basic arithmetic; never eval arbitrary Python.
    if not re.fullmatch(r"[0-9+\-*/(). %]+", expression):
        return await ctx.send("Only basic arithmetic characters are allowed.")
    try:
        import ast
        tree = ast.parse(expression, mode="eval")
        allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
                   ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
                   ast.Mod, ast.Pow, ast.USub, ast.UAdd)
        if any(not isinstance(n, allowed) for n in ast.walk(tree)):
            raise ValueError
        def calc(node):
            if isinstance(node, ast.Expression): return calc(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int,float)): return node.value
            if isinstance(node, ast.UnaryOp):
                x=calc(node.operand); return -x if isinstance(node.op,ast.USub) else +x
            a,b=calc(node.left),calc(node.right)
            if isinstance(node.op,ast.Add): return a+b
            if isinstance(node.op,ast.Sub): return a-b
            if isinstance(node.op,ast.Mult): return a*b
            if isinstance(node.op,ast.Div): return a/b
            if isinstance(node.op,ast.FloorDiv): return a//b
            if isinstance(node.op,ast.Mod): return a%b
            if isinstance(node.op,ast.Pow):
                if abs(b)>8: raise ValueError
                return a**b
            raise ValueError
        result=calc(tree)
        if not math.isfinite(float(result)) or abs(float(result)) > 1e100: raise ValueError
        await ctx.send(f"Result: `{result}`")
    except Exception:
        await ctx.send("Invalid or unsafe arithmetic expression.")

@bot.command()
@owner_only()
async def timestamp(ctx, *, date_text: str):
    try:
        parsed = dt.datetime.fromisoformat(date_text)
        if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=dt.timezone.utc)
        await ctx.send(f"`{int(parsed.timestamp())}` • <t:{int(parsed.timestamp())}:F>")
    except ValueError:
        await ctx.send("Use ISO format, e.g. `2026-12-25T18:30:00+05:30`.")

# ----------------------------- ERROR HANDLING -----------------------------

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        return await ctx.send("⛔ You are not authorized to use this command.")
    if isinstance(error, commands.MissingPermissions):
        return await ctx.send("⛔ You do not have the required Discord permissions.")
    if isinstance(error, commands.BotMissingPermissions):
        return await ctx.send("⛔ I am missing required Discord permissions.")
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(f"Missing argument: `{error.param.name}`. Use `{prefix_for(ctx.guild.id if ctx.guild else None)}help`.")
    if isinstance(error, commands.BadArgument):
        return await ctx.send("Invalid argument. Check the command format.")
    log.exception("Command error", exc_info=error)
    await ctx.send("An unexpected error occurred. Check Railway logs.")

# ----------------------------- STARTUP ------------------------------------

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN environment variable.")
    if not OWNER_ID:
        raise SystemExit("Missing OWNER_ID environment variable. Set your numeric Discord user ID.")
    if yt_dlp is None:
        log.warning("yt-dlp is not installed. Music commands will not work. pip install yt-dlp")
    init_db()
    bot.run(TOKEN, log_handler=None)
