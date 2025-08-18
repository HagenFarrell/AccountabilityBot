import os
import re
import sqlite3
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# =============================
# Environment / Config
# =============================
TOKEN = os.getenv("DISCORD_TOKEN", "")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "America/Chicago")
GUILD_ID = os.getenv("GUILD_ID")  # optional, speeds up command sync
BULLETIN_CHANNEL_ID = os.getenv("BULLETIN_CHANNEL_ID")  # can be set via command later
ADMIN_ROLE_ID = os.getenv("ADMIN_ROLE_ID")  # optional: restrict admin commands to this role id
DB_PATH = os.getenv("DB_PATH", "accountability.db")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN env var is required.")

# =============================
# Database
# =============================
class DB:
    def __init__(self, path: str):
        self.path = path
        self._init()


    def _connect(self):
        return sqlite3.connect(self.path)

    def _init(self):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS members (
                    user_id INTEGER PRIMARY KEY,
                    tz TEXT NOT NULL,
                    hhmm TEXT NOT NULL,
                    approved INTEGER NOT NULL DEFAULT 1
                );
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS checkins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES members(user_id)
                );
                """
            )
            conn.commit()
        self._ensure_columns()

    def _ensure_columns(self):
        """Lightweight migrations: add columns if they don't exist yet."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(members)")
            cols = {row[1] for row in cur.fetchall()}
            if "cadence" not in cols:
                cur.execute("ALTER TABLE members ADD COLUMN cadence TEXT NOT NULL DEFAULT 'daily'")
            if "dow" not in cols:
                cur.execute("ALTER TABLE members ADD COLUMN dow TEXT")  # mon..sun or NULL
            conn.commit()
    
    def set_member_cadence(self, user_id: int, cadence: str, dow: str | None):
        with self._connect() as conn:
            conn.execute("UPDATE members SET cadence=?, dow=? WHERE user_id=?", (cadence, dow, user_id))
            conn.commit()

    def get_member_full(self, user_id: int):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, tz, hhmm, approved, cadence, dow FROM members WHERE user_id=?",
                (user_id,)
            ).fetchone()
            if not row:
                return None
            return {
                "user_id": row[0], "tz": row[1], "hhmm": row[2],
                "approved": row[3], "cadence": row[4], "dow": row[5]
            }

    def get_active_members_full(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT user_id, tz, hhmm, cadence, dow FROM members WHERE approved=1"
            ).fetchall()
            return [{"user_id": r[0], "tz": r[1], "hhmm": r[2], "cadence": r[3], "dow": r[4]} for r in rows]

    
    # Settings
    def get_setting(self, key: str):
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def set_setting(self, key: str, value: str):
        with self._connect() as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            conn.commit()

    # Members
    def upsert_member(self, user_id: int, tz: str, hhmm: str, approved: int = 1):
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO members(user_id, tz, hhmm, approved)
                VALUES(?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET tz=excluded.tz, hhmm=excluded.hhmm, approved=excluded.approved
                """,
                (user_id, tz, hhmm, approved),
            )
            conn.commit()

    def set_member_time(self, user_id: int, hhmm: str):
        with self._connect() as conn:
            conn.execute("UPDATE members SET hhmm=? WHERE user_id=?", (hhmm, user_id))
            conn.commit()

    def set_member_tz(self, user_id: int, tz: str):
        with self._connect() as conn:
            conn.execute("UPDATE members SET tz=? WHERE user_id=?", (tz, user_id))
            conn.commit()

    def approve_member(self, user_id: int, approved: int):
        with self._connect() as conn:
            conn.execute("INSERT INTO members(user_id, tz, hhmm, approved) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET approved=excluded.approved",
                         (user_id, DEFAULT_TZ, "08:00", approved))
            conn.commit()

    def get_member(self, user_id: int):
        with self._connect() as conn:
            row = conn.execute("SELECT user_id, tz, hhmm, approved FROM members WHERE user_id=?", (user_id,)).fetchone()
            if row:
                return {"user_id": row[0], "tz": row[1], "hhmm": row[2], "approved": row[3]}
            return None

    def get_approved_members(self):
        with self._connect() as conn:
            rows = conn.execute("SELECT user_id, tz, hhmm FROM members WHERE approved=1").fetchall()
            return [(r[0], r[1], r[2]) for r in rows]

    # Check-ins
    def add_checkin(self, user_id: int, content: str):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO checkins(user_id, content, created_at_utc) VALUES(?,?,?)",
                (user_id, content, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()


db = DB(DB_PATH)

# If BULLETIN_CHANNEL_ID not provided, try DB setting
if BULLETIN_CHANNEL_ID is None:
    BULLETIN_CHANNEL_ID = db.get_setting("bulletin_channel_id")

# =============================
# Discord Bot Setup
# =============================
intents = discord.Intents.default()
# Needed to read user DMs for relaying check-ins
intents.message_content = True
intents.members = True
intents.dm_messages = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=timezone.utc)
user_jobs: dict[int, str] = {}  # user_id -> job id

# =============================
# Utilities
# =============================
ADMIN_ROLE_NAME="Check-In Bot Admin"
def is_admin(interaction: discord.Interaction) -> bool:
    """Admin if user has Manage Guild/Admin perms, or matches ADMIN_ROLE_ID / ADMIN_ROLE_NAME."""
    user = interaction.user
    if isinstance(user, discord.Member):
        # Native perms first
        if user.guild_permissions.manage_guild or user.guild_permissions.administrator:
            return True
        # Role by ID
        if ADMIN_ROLE_ID:
            try:
                admin_role_id = int(ADMIN_ROLE_ID)
                if any(r.id == admin_role_id for r in user.roles):
                    return True
            except ValueError:
                pass
        # Role by name (case-insensitive)
        if ADMIN_ROLE_NAME:
            target = ADMIN_ROLE_NAME.strip().lower()
            if any(r.name.lower() == target for r in user.roles):
                return True
    return False



def valid_hhmm(value: str) -> bool:
    return re.fullmatch(r"^(?:[01]\d|2[0-3]):[0-5]\d$", value) is not None


def ensure_zoneinfo(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except Exception as e:
        raise ValueError(f"Invalid timezone: {tz}") from e


async def get_bulletin_channel() -> discord.TextChannel | None:
    global BULLETIN_CHANNEL_ID
    if not BULLETIN_CHANNEL_ID:
        # Try DB again in case it was set later
        val = db.get_setting("bulletin_channel_id")
        if val:
            BULLETIN_CHANNEL_ID = val
    if not BULLETIN_CHANNEL_ID:
        return None
    try:
        channel_id = int(BULLETIN_CHANNEL_ID)
    except ValueError:
        return None
    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel
    return None


def _norm_dow(d: str | None) -> str | None:
    if not d:
        return None
    d = d.strip().lower()
    alias = {
        "monday":"mon","tuesday":"tue","wednesday":"wed","thursday":"thu","friday":"fri","saturday":"sat","sunday":"sun",
        "mon":"mon","tue":"tue","wed":"wed","thu":"thu","fri":"fri","sat":"sat","sun":"sun"
    }
    return alias.get(d)

async def schedule_for_member(user_id: int, tz: str, hhmm: str, cadence: str = "daily", dow: str | None = None):
    # remove existing
    job_id = user_jobs.get(user_id)
    if job_id:
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass

    hour, minute = map(int, hhmm.split(":"))
    try:
        tzinfo = ensure_zoneinfo(tz)
    except ValueError:
        tzinfo = ensure_zoneinfo(DEFAULT_TZ)

    trig_kwargs = {"hour": hour, "minute": minute, "timezone": tzinfo}
    if (cadence or "daily").lower() == "weekly":
        nd = _norm_dow(dow or "")
        if not nd:
            # fallback: default weekly on user's tz Monday
            nd = "mon"
        trig_kwargs["day_of_week"] = nd

    trigger = CronTrigger(**trig_kwargs)
    job = scheduler.add_job(send_daily_prompt_to_user, trigger=trigger, args=[user_id], id=f"dm_{user_id}")
    user_jobs[user_id] = job.id


async def send_daily_prompt_to_user(user_id: int):
    user = bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except discord.HTTPException:
            return
    try:
        await user.send(
            "Good morning, brother. How are you today? Reply here and I will post it to the group bulletin.\n\n" \
            "If you want to change the time I message you, use /settime HH:MM. To set your timezone, use /settimezone America/Chicago"
        )
    except discord.Forbidden:
        # DMs closed
        pass


async def reschedule_all():
    for m in db.get_active_members_full():
        await schedule_for_member(m["user_id"], m["tz"], m["hhmm"], m["cadence"], m["dow"])


# =============================
# Event Handlers
# =============================
@bot.event
async def on_ready():
    # Global sync (harmless; helps if you later want global commands)
    try:
        await bot.tree.sync()
        print("Synced global commands.")
    except Exception as e:
        print("Global sync error:", e)

    # Force-sync to each guild we actually share (instant appearance)
    for g in bot.guilds:
        try:
            await bot.tree.sync(guild=g)
            print(f"Synced commands to guild {g.name} ({g.id})")
        except Exception as e:
            print(f"Guild sync error for {g.id}: {e}")

    if not scheduler.running:
        scheduler.start()
        await reschedule_all()

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    # Ignore bots
    if message.author.bot:
        return

    # --- DM handling first ---
    if isinstance(message.channel, discord.DMChannel):
        member = db.get_member(message.author.id)
        if not member or member.get("approved") != 1:
            try:
                await message.channel.send(
                    "You're not enrolled yet. Use **/join** in the server to opt into daily check-ins.\n"
                    "After joining, set your time with **/settime** and timezone with **/settimezone**."
                )
            except discord.Forbidden:
                pass
            return  # end DM path cleanly

        content = (message.content or "").strip()
        if not content:
            return

        # Store check-in
        db.add_checkin(message.author.id, content)

        # Relay to bulletin channel
        channel = await get_bulletin_channel()
        if channel is None:
            try:
                await message.channel.send(
                    "I recorded your check-in, but the bulletin channel isn't configured yet."
                )
            except discord.Forbidden:
                pass
            return

        author_name = message.author.global_name or message.author.display_name or message.author.name
        member = db.get_member(message.author.id)  # refresh in case tz changed
        now_local = datetime.now(tz=timezone.utc).astimezone(ZoneInfo(member["tz"]))
        embed = discord.Embed(
            title="Daily Check-in",
            description=content,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=author_name, icon_url=message.author.display_avatar.url)
        embed.set_footer(text=f"Local time: {now_local.strftime('%Y-%m-%d %H:%M %Z')}")

        try:
            await channel.send(embed=embed)
            await message.channel.send("✅ Posted to the group bulletin.")
        except discord.HTTPException:
            try:
                await message.channel.send("Sorry, I couldn't post to the bulletin channel.")
            except discord.Forbidden:
                pass
        return  # end DM path

    # --- Non-DM messages go to command processor ---
    await bot.process_commands(message)


# =============================
# Slash Commands
# =============================

def guild_scope():
    if GUILD_ID:
        return app_commands.guilds(discord.Object(id=int(GUILD_ID)))
    return (lambda f: f)  # no-op decorator


@guild_scope()
@bot.tree.command(name="ping", description="Bot responsiveness check")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong! ✅", ephemeral=True)


@guild_scope()
@bot.tree.command(name="mysettings", description="Show your current DM time and timezone")
async def mysettings(interaction: discord.Interaction):
    member = db.get_member(interaction.user.id)
    if not member or member.get("approved") != 1:
        await interaction.response.send_message(
            "You're not enrolled. Use **/join** to start, then **/settimezone** and **/settime**.",
            ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"Time: **{member['hhmm']}**\nTimezone: **{member['tz']}**",
        ephemeral=True,
    )


@guild_scope()
@bot.tree.command(name="setcadence", description="Choose daily or weekly check-ins")
@app_commands.describe(mode="daily or weekly")
async def setcadence(interaction: discord.Interaction, mode: str):
    mode_l = (mode or "").strip().lower()
    if mode_l not in {"daily","weekly"}:
        await interaction.response.send_message("Use `daily` or `weekly`.", ephemeral=True)
        return
    m = db.get_member_full(interaction.user.id)
    if not m or m["approved"] != 1:
        await interaction.response.send_message("You're not enrolled. Use **/join** first.", ephemeral=True)
        return
    db.set_member_cadence(interaction.user.id, mode_l, m["dow"] if mode_l=="weekly" else None)
    # reschedule
    m = db.get_member_full(interaction.user.id)
    await schedule_for_member(m["user_id"], m["tz"], m["hhmm"], m["cadence"], m["dow"])
    await interaction.response.send_message(f"✅ Cadence set to **{mode_l}**.", ephemeral=True)


@guild_scope()
@bot.tree.command(name="setweekly", description="Set your weekly day and time for check-ins")
@app_commands.describe(day="mon..sun or full name", hhmm="24h time like 07:30")
async def setweekly(interaction: discord.Interaction, day: str, hhmm: str):
    if not valid_hhmm(hhmm):
        await interaction.response.send_message("Time must be HH:MM (24-hour).", ephemeral=True)
        return
    day_norm = _norm_dow(day)
    if not day_norm:
        await interaction.response.send_message("Day must be one of mon..sun (or full name).", ephemeral=True)
        return
    m = db.get_member_full(interaction.user.id)
    if not m or m["approved"] != 1:
        await interaction.response.send_message("You're not enrolled. Use **/join** first.", ephemeral=True)
        return
    # update both hhmm and cadence/dow
    db.set_member_time(interaction.user.id, hhmm)
    db.set_member_cadence(interaction.user.id, "weekly", day_norm)
    m = db.get_member_full(interaction.user.id)
    await schedule_for_member(m["user_id"], m["tz"], m["hhmm"], m["cadence"], m["dow"])
    await interaction.response.send_message(f"✅ Weekly check-in set: **{day_norm} {hhmm}**.", ephemeral=True)


@guild_scope()
@bot.tree.command(name="settime", description="Set the daily DM time (HH:MM in 24h format)")
@app_commands.describe(hhmm="Time like 07:30 or 21:05")
async def settime(interaction: discord.Interaction, hhmm: str):
    if not valid_hhmm(hhmm):
        await interaction.response.send_message("Please provide time as HH:MM (24-hour).", ephemeral=True)
        return

    member = db.get_member(interaction.user.id)
    if not member:
        # Auto-approve on first use with defaults (optional)
        db.upsert_member(interaction.user.id, DEFAULT_TZ, hhmm, approved=1)
    else:
        db.set_member_time(interaction.user.id, hhmm)

    # Reschedule this user's job
    member = db.get_member(interaction.user.id)
    await schedule_for_member(member["user_id"], member["tz"], member["hhmm"])

    await interaction.response.send_message(
        f"✅ I'll DM you daily at **{hhmm}** your time.", ephemeral=True
    )


@guild_scope()
@bot.tree.command(name="settimezone", description="Set your timezone (e.g., America/Chicago)")
@app_commands.describe(tz="IANA timezone like America/Chicago, Europe/London, Asia/Kolkata")
async def settimezone(interaction: discord.Interaction, tz: str):
    try:
        ensure_zoneinfo(tz)
    except ValueError:
        await interaction.response.send_message(
            "That doesn't look like a valid timezone. Try something like `America/Chicago`.", ephemeral=True
        )
        return

    member = db.get_member(interaction.user.id)
    if not member:
        db.upsert_member(interaction.user.id, tz, "08:00", approved=1)
    else:
        db.set_member_tz(interaction.user.id, tz)

    member = db.get_member(interaction.user.id)
    await schedule_for_member(member["user_id"], member["tz"], member["hhmm"])

    await interaction.response.send_message(
        f"✅ Timezone set to **{tz}**. Your daily DM is at **{member['hhmm']}** local time.",
        ephemeral=True,
    )

@guild_scope()
@bot.tree.command(name="join", description="Opt-in to daily check-ins for this server")
async def join(interaction: discord.Interaction):
    existing = db.get_member(interaction.user.id)
    tz = existing["tz"] if existing else DEFAULT_TZ
    hhmm = existing["hhmm"] if existing else "08:00"
    db.upsert_member(interaction.user.id, tz, hhmm, approved=1)
    db.set_member_cadence(interaction.user.id, "daily", None)
    await schedule_for_member(interaction.user.id, tz, hhmm)
    await interaction.response.send_message(
        "✅ You're in! I'll DM you each day. Use **/settimezone** and **/settime** to customize.",
        ephemeral=True,
    )


@guild_scope()
@bot.tree.command(name="leave", description="Opt-out so your DMs won't be posted anymore")
async def leave(interaction: discord.Interaction):
    m = db.get_member(interaction.user.id)
    if not m or m.get("approved") != 1:
        await interaction.response.send_message("You're not enrolled.", ephemeral=True)
        return
    db.approve_member(interaction.user.id, 0)
    job_id = user_jobs.get(interaction.user.id)
    if job_id:
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
        user_jobs.pop(interaction.user.id, None)
    await interaction.response.send_message("✅ Unsubscribed. You can **/join** again anytime.", ephemeral=True)


# ---- Admin Commands ----

@guild_scope()
@bot.tree.command(name="setbulletin", description="Admin: set the bulletin channel (where check-ins are posted)")
@app_commands.describe(channel="The target channel")
async def setbulletin(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(interaction):
        await interaction.response.send_message("You don't have permission to do that.", ephemeral=True)
        return
    db.set_setting("bulletin_channel_id", str(channel.id))
    global BULLETIN_CHANNEL_ID
    BULLETIN_CHANNEL_ID = str(channel.id)
    await interaction.response.send_message(
        f"✅ Bulletin channel set to {channel.mention}.", ephemeral=True
    )


@guild_scope()
@bot.tree.command(name="approve", description="Admin: approve a member to use the bot")
@app_commands.describe(user="User to approve")
async def approve(interaction: discord.Interaction, user: discord.User):
    if not is_admin(interaction):
        await interaction.response.send_message("You don't have permission to do that.", ephemeral=True)
        return
    # Default: current DEFAULT_TZ, 08:00
    db.approve_member(user.id, 1)
    m = db.get_member(user.id)
    await schedule_for_member(m["user_id"], m["tz"], m["hhmm"])

    try:
        await user.send(
            "You've been added to the accountability check-in bot.\n"
            "Use /settimezone and /settime to configure when I DM you each day."
        )
    except discord.Forbidden:
        pass

    await interaction.response.send_message(f"✅ Approved {user.mention}.", ephemeral=True)


@guild_scope()
@bot.tree.command(name="revoke", description="Admin: revoke a member so their DMs won't post")
@app_commands.describe(user="User to revoke")
async def revoke(interaction: discord.Interaction, user: discord.User):
    if not is_admin(interaction):
        await interaction.response.send_message("You don't have permission to do that.", ephemeral=True)
        return
    db.approve_member(user.id, 0)
    # Remove scheduled job if any
    job_id = user_jobs.get(user.id)
    if job_id:
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
        user_jobs.pop(user.id, None)

    await interaction.response.send_message(f"✅ Revoked {user.mention}.", ephemeral=True)


# =============================
# Run bot
# =============================
# Tip: Discord Developer Portal → Bot → Privileged Gateway Intents → enable Message Content + Server Members

if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        pass
