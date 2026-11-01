"""
PRIVATE DISCORD MANAGEMENT BOT
Python 3.12 | discord.py 2.x | Prefix commands only | SQLite
Single-file starter with owner controls, moderation, logging, AutoMod,
anti-nuke safeguards, server snapshots, personal tools and automation.

IMPORTANT:
- Set DISCORD_TOKEN and OWNER_ID in Railway Variables.
- Enable Message Content Intent and Server Members Intent in the Developer Portal.
- Mount a Railway Volume at /data. The database and backups are stored there.
- Give the bot only the permissions it needs. Do not casually grant Administrator.
- Anti-nuke is best-effort; Discord audit-log/API delays mean no bot can guarantee
  prevention of every destructive action.
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
import datetime as dt
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands, tasks

try:
    import psutil
except ImportError:
    psutil = None

# ----------------------------- CONFIGURATION -----------------------------

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DEFAULT_PREFIX = os.getenv("DEFAULT_PREFIX", ".")
DB_PATH = os.getenv("DATABASE_PATH", "/data/bot.db")
DATA_DIR = Path(DB_PATH).parent
BACKUP_DIR = DATA_DIR / "backups"
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")

DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

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
    command_prefix=DEFAULT_PREFIX,
    intents=intents,
    help_command=None,
    case_insensitive=True,
    strip_after_prefix=True
)
bot.start_time = time.monotonic()
db: Optional[object] = None
anti_nuke_events: dict[tuple[int, str], list[float]] = {}
reminder_task_started = False

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
      guild_id INTEGER PRIMARY KEY, prefix TEXT, log_channel INTEGER,
      welcome_channel INTEGER, goodbye_channel INTEGER, autorole INTEGER,
      antinuke INTEGER DEFAULT 0, antinuke_limit INTEGER DEFAULT 3,
      automod INTEGER DEFAULT 0, bad_words TEXT DEFAULT '[]',
      blocked_links INTEGER DEFAULT 0, invite_filter INTEGER DEFAULT 0,
      spam_limit INTEGER DEFAULT 6, lockdown INTEGER DEFAULT 0
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
    """
    db.executescript(schema)
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

async def dynamic_prefix(bot_obj, message):
    return prefix_for(message.guild.id if message.guild else None)

# Re-create bot with dynamic prefix while preserving intents/config.
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

# ----------------------------- READY / TASKS ------------------------------

@bot.event
async def on_ready():
    global reminder_task_started
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    if not reminder_task_started:
        reminder_loop.start()
        reminder_task_started = True
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name=f"{len(bot.guilds)} servers | {DEFAULT_PREFIX}help"))

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

@bot.command(name="help")
async def help_command(ctx, category: Optional[str] = None):
    p = prefix_for(ctx.guild.id if ctx.guild else None)
    pages = {
      "owner": f"`{p}ping` `{p}stats` `{p}uptime` `{p}prefix` `{p}maintenance` `{p}shutdown` `{p}guilds` `{p}system`",
      "moderation": f"`{p}ban` `{p}unban` `{p}kick` `{p}timeout` `{p}untimeout` `{p}warn` `{p}warnings` `{p}purge` `{p}slowmode` `{p}lock` `{p}unlock`",
      "security": f"`{p}antinuke` `{p}whitelist` `{p}unwhitelist` `{p}automod` `{p}badword` `{p}linkfilter` `{p}invitefilter` `{p}lockdown`",
      "server": f"`{p}serverinfo` `{p}userinfo` `{p}avatar` `{p}roleinfo` `{p}channelinfo` `{p}membercount` `{p}createchannel` `{p}createrole`",
      "logs": f"`{p}setlogs` `{p}modlogs`",
      "backup": f"`{p}backup create` `{p}backup list` `{p}backup restore`",
      "personal": f"`{p}note add/list/delete` `{p}todo add/list/done` `{p}remind` `{p}password` `{p}calculate` `{p}timestamp`",
      "automation": f"`{p}setwelcome` `{p}setgoodbye` `{p}setautorole` `{p}autoresponse add/remove/list` `{p}sticky`"
    }
    if category:
        text = pages.get(category.lower())
        if not text:
            return await ctx.send("Categories: " + ", ".join(pages))
        return await send_embed(ctx, f"{category.title()} commands", text)
    em = discord.Embed(title="Private Management Bot • Help",
      description=f"Prefix: `{p}`\nUse `{p}help <category>` for commands.",
      color=discord.Color.dark_blue())
    for name, cmds in pages.items():
        em.add_field(name=name.title(), value=cmds, inline=False)
    em.set_footer(text="Owner-authorized management system")
    await ctx.send(embed=em)

@bot.command()
@owner_only()
async def ping(ctx):
    await ctx.send(f"🏓 Pong! `{round(bot.latency * 1000)}ms`")

@bot.command()
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

@bot.command()
@owner_only()
async def prefix(ctx, new_prefix: Optional[str] = None):
    if not ctx.guild:
        return await ctx.send(f"Current prefix: `{DEFAULT_PREFIX}`")
    cfg = get_cfg(ctx.guild.id)
    if not new_prefix:
        return await ctx.send(f"Current prefix: `{cfg['prefix']}`")
    if len(new_prefix) > 5 or " " in new_prefix:
        return await ctx.send("Prefix must be 1–5 characters with no spaces.")
    q("UPDATE guild_config SET prefix=? WHERE guild_id=?", (new_prefix, ctx.guild.id))
    await ctx.send(f"Prefix changed to `{new_prefix}`.")

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
    init_db()
    bot.run(TOKEN, log_handler=None)
