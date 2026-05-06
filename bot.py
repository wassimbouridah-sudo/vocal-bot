import discord
from discord.ext import commands, tasks
from discord import app_commands
import sqlite3
import json
import os
import time
import asyncio
from datetime import datetime, timedelta
import logging

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
# Les variables d'environnement (Railway) ont la priorité sur config.json
with open("config.json", "r") as f:
    config = json.load(f)

def env(key: str, fallback):
    """Lit d'abord la variable d'environnement, sinon utilise config.json."""
    val = os.environ.get(key)
    if val is not None:
        return type(fallback)(val)
    return fallback

TRACKED_CHANNEL_ID  = env("TRACKED_CHANNEL_ID",  config["tracked_voice_channel_id"])
ANNOUNCE_CHANNEL_ID = env("ANNOUNCE_CHANNEL_ID",  config["announce_channel_id"])
TOP_ROLE_ID         = env("TOP_ROLE_ID",         config["top1_role_id"])
ANNOUNCE_DAY        = env("ANNOUNCE_DAY",        config["announce_day"])
ANNOUNCE_HOUR       = env("ANNOUNCE_HOUR",       config["announce_hour"])
ANNOUNCE_MINUTE     = env("ANNOUNCE_MINUTE",     config["announce_minute"])
TOKEN               = os.environ.get("DISCORD_TOKEN") or config["token"]

# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect("voice_time.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS voice_sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT    NOT NULL,
                join_time   REAL    NOT NULL,
                leave_time  REAL,
                week_start  TEXT    NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS weekly_top (
                week_start  TEXT PRIMARY KEY,
                top1_id     TEXT,
                top2_id     TEXT,
                top3_id     TEXT
            )
        """)
        db.commit()
    log.info("Base de données initialisée.")

def current_week_start() -> str:
    """Retourne le lundi de la semaine courante au format YYYY-MM-DD."""
    today = datetime.utcnow().date()
    monday = today - timedelta(days=today.weekday())
    return str(monday)

def open_session(user_id: int):
    week = current_week_start()
    with get_db() as db:
        db.execute(
            "INSERT INTO voice_sessions (user_id, join_time, week_start) VALUES (?, ?, ?)",
            (str(user_id), time.time(), week)
        )
        db.commit()

def close_session(user_id: int):
    with get_db() as db:
        db.execute("""
            UPDATE voice_sessions
            SET leave_time = ?
            WHERE user_id = ? AND leave_time IS NULL
        """, (time.time(), str(user_id)))
        db.commit()

def get_top3(week: str) -> list[dict]:
    """Retourne le top 3 des utilisateurs pour une semaine donnée."""
    with get_db() as db:
        rows = db.execute("""
            SELECT user_id,
                   SUM(COALESCE(leave_time, ?) - join_time) AS total_seconds
            FROM voice_sessions
            WHERE week_start = ?
            GROUP BY user_id
            ORDER BY total_seconds DESC
            LIMIT 3
        """, (time.time(), week)).fetchall()
    return [{"user_id": r["user_id"], "seconds": r["total_seconds"]} for r in rows]

def save_weekly_top(week: str, top: list[dict]):
    ids = [t["user_id"] for t in top]
    while len(ids) < 3:
        ids.append(None)
    with get_db() as db:
        db.execute("""
            INSERT OR REPLACE INTO weekly_top (week_start, top1_id, top2_id, top3_id)
            VALUES (?, ?, ?, ?)
        """, (week, ids[0], ids[1], ids[2]))
        db.commit()

def get_previous_top1(week: str) -> str | None:
    """Retourne l'ID du top 1 de la semaine précédente."""
    with get_db() as db:
        row = db.execute(
            "SELECT top1_id FROM weekly_top WHERE week_start < ? ORDER BY week_start DESC LIMIT 1",
            (week,)
        ).fetchone()
    return row["top1_id"] if row else None

# ── Bot ───────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Stocke les sessions actives en mémoire { user_id: join_timestamp }
active_sessions: dict[int, float] = {}

def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}min {s:02d}s"
    return f"{m}min {s:02d}s"

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    init_db()
    log.info(f"Connecté en tant que {bot.user} ({bot.user.id})")
    try:
        synced = await bot.tree.sync()
        log.info(f"{len(synced)} commande(s) slash synchronisée(s).")
    except Exception as e:
        log.error(f"Erreur sync slash: {e}")

    weekly_announcement.start()
    log.info("Tâche hebdomadaire démarrée.")

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    uid = member.id

    # L'utilisateur REJOINT le salon tracké
    if after.channel and after.channel.id == TRACKED_CHANNEL_ID:
        if uid not in active_sessions:
            active_sessions[uid] = time.time()
            open_session(uid)
            log.info(f"{member.display_name} a rejoint le salon tracké.")

    # L'utilisateur QUITTE le salon tracké
    if before.channel and before.channel.id == TRACKED_CHANNEL_ID:
        if after.channel is None or after.channel.id != TRACKED_CHANNEL_ID:
            if uid in active_sessions:
                del active_sessions[uid]
            close_session(uid)
            log.info(f"{member.display_name} a quitté le salon tracké.")

# ── Weekly task ───────────────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def weekly_announcement():
    now = datetime.utcnow()
    if now.weekday() != ANNOUNCE_DAY:
        return
    if now.hour != ANNOUNCE_HOUR or now.minute != ANNOUNCE_MINUTE:
        return

    await do_weekly_announcement()

@weekly_announcement.before_loop
async def before_weekly():
    await bot.wait_until_ready()

async def do_weekly_announcement(week: str | None = None):
    """Effectue l'annonce + attribution du rôle."""
    announce_channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)
    if not announce_channel:
        log.error("Salon d'annonce introuvable !")
        return

    if week is None:
        week = current_week_start()

    top = get_top3(week)
    if not top:
        await announce_channel.send("😴 Personne n'a été actif dans le vocal cette semaine !")
        return

    save_weekly_top(week, top)

    guild = announce_channel.guild
    top_role = guild.get_role(TOP_ROLE_ID)

    # ── Retirer le rôle à l'ancien top 1 ──────────────────────────────────
    prev_week_monday = str(
        (datetime.strptime(week, "%Y-%m-%d") - timedelta(weeks=1)).date()
    )
    prev_top1_id = get_previous_top1(week)

    if prev_top1_id and top_role:
        prev_member = guild.get_member(int(prev_top1_id))
        if prev_member and top_role in prev_member.roles:
            try:
                await prev_member.remove_roles(top_role, reason="Rotation top 1 vocal")
                log.info(f"Rôle retiré à {prev_member.display_name}")
            except discord.Forbidden:
                log.warning("Permission manquante pour retirer le rôle.")

    # ── Donner le rôle au nouveau top 1 ───────────────────────────────────
    new_top1_member = None
    if top_role and top:
        new_top1_member = guild.get_member(int(top[0]["user_id"]))
        if new_top1_member:
            try:
                await new_top1_member.add_roles(top_role, reason="Top 1 vocal de la semaine")
                log.info(f"Rôle attribué à {new_top1_member.display_name}")
            except discord.Forbidden:
                log.warning("Permission manquante pour ajouter le rôle.")

    # ── Construction de l'embed ────────────────────────────────────────────
    medals = ["🥇", "🥈", "🥉"]
    embed = discord.Embed(
        title="🎙️ Top vocal de la semaine !",
        description=f"Semaine du **{week}**",
        color=discord.Color.gold(),
        timestamp=datetime.utcnow()
    )

    for i, entry in enumerate(top):
        member = guild.get_member(int(entry["user_id"]))
        name   = member.display_name if member else f"Utilisateur #{entry['user_id']}"
        extra  = f"  ← {top_role.mention}" if i == 0 and top_role else ""
        embed.add_field(
            name=f"{medals[i]} {name}{extra}",
            value=f"⏱️ **{fmt_duration(entry['seconds'])}**",
            inline=False
        )

    embed.set_footer(text="Rendez-vous la semaine prochaine !")

    await announce_channel.send("@here", embed=embed)
    log.info("Annonce hebdomadaire envoyée.")

# ── Slash commands ────────────────────────────────────────────────────────────
@bot.tree.command(name="stats", description="Affiche ton temps dans le salon vocal cette semaine")
async def stats(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    week = current_week_start()

    extra = 0.0
    if interaction.user.id in active_sessions:
        extra = time.time() - active_sessions[interaction.user.id]

    with get_db() as db:
        row = db.execute("""
            SELECT SUM(COALESCE(leave_time, ?) - join_time) AS total
            FROM voice_sessions
            WHERE user_id = ? AND week_start = ?
        """, (time.time(), uid, week)).fetchone()

    total = (row["total"] or 0) + extra
    await interaction.response.send_message(
        f"⏱️ Tu as passé **{fmt_duration(total)}** dans le salon vocal cette semaine !",
        ephemeral=True
    )

@bot.tree.command(name="top", description="Affiche le classement vocal de la semaine en cours")
async def top_command(interaction: discord.Interaction):
    week = current_week_start()
    top  = get_top3(week)

    if not top:
        await interaction.response.send_message("Aucune donnée pour cette semaine.", ephemeral=True)
        return

    medals = ["🥇", "🥈", "🥉"]
    lines  = []
    for i, entry in enumerate(top):
        member = interaction.guild.get_member(int(entry["user_id"]))
        name   = member.display_name if member else f"#{entry['user_id']}"
        lines.append(f"{medals[i]} **{name}** — {fmt_duration(entry['seconds'])}")

    embed = discord.Embed(
        title="🎙️ Classement vocal — semaine en cours",
        description="\n".join(lines),
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="announce", description="[ADMIN] Force l'annonce hebdomadaire maintenant")
@app_commands.checks.has_permissions(administrator=True)
async def force_announce(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await do_weekly_announcement()
    await interaction.followup.send("✅ Annonce envoyée !", ephemeral=True)

@force_announce.error
async def force_announce_error(interaction: discord.Interaction, error):
    await interaction.response.send_message("❌ Réservé aux administrateurs.", ephemeral=True)

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(TOKEN)
