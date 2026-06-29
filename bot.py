import discord
from discord.ext import commands, tasks
from discord import app_commands
import sqlite3
import json
import os
import time
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
import logging

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Health check server (requis par Railway) ──────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info(f"Health check server démarré sur le port {port}")
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

# ── Config ────────────────────────────────────────────────────────────────────
with open("config.json", "r") as f:
    config = json.load(f)

def env(key: str, fallback):
    val = os.environ.get(key)
    if val is not None:
        return type(fallback)(val)
    return fallback

TRACKED_CHANNEL_ID  = env("TRACKED_CHANNEL_ID",  config["tracked_voice_channel_id"])
# CORRECTIF : salon ranked au lieu du salon annonce
RANKED_CHANNEL_ID   = int(os.environ.get("RANKED_CHANNEL_ID", "1480760348905832582"))
ANNOUNCE_CHANNEL_ID = env("ANNOUNCE_CHANNEL_ID",  config["announce_channel_id"])
TOP_ROLE_ID         = env("TOP_ROLE_ID",          config["top1_role_id"])
# CORRECTIF : dimanche = 6, heure 21h59 UTC = 23h59 heure France (UTC+2)
ANNOUNCE_DAY        = int(os.environ.get("ANNOUNCE_DAY",    "6"))
ANNOUNCE_HOUR       = int(os.environ.get("ANNOUNCE_HOUR",   "21"))
ANNOUNCE_MINUTE     = int(os.environ.get("ANNOUNCE_MINUTE", "59"))
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
    # CORRECTIF : basé sur l'heure France (UTC+2) pour que la semaine
    # corresponde à ce que voient les élèves
    now_france = datetime.utcnow() + timedelta(hours=2)
    today = now_france.date()
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

def get_all_ranked(week: str) -> list[dict]:
    """Récupère TOUS les élèves classés par temps cette semaine."""
    with get_db() as db:
        rows = db.execute("""
            SELECT user_id,
                   SUM(COALESCE(leave_time, ?) - join_time) AS total_seconds
            FROM voice_sessions
            WHERE week_start = ?
            GROUP BY user_id
            ORDER BY total_seconds DESC
        """, (time.time(), week)).fetchall()
    return [{"user_id": r["user_id"], "seconds": r["total_seconds"]} for r in rows]

def get_top3(week: str) -> list[dict]:
    return get_all_ranked(week)[:3]

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
    if after.channel and after.channel.id == TRACKED_CHANNEL_ID:
        if uid not in active_sessions:
            active_sessions[uid] = time.time()
            open_session(uid)
            log.info(f"{member.display_name} a rejoint le salon tracké.")
    if before.channel and before.channel.id == TRACKED_CHANNEL_ID:
        if after.channel is None or after.channel.id != TRACKED_CHANNEL_ID:
            if uid in active_sessions:
                del active_sessions[uid]
            close_session(uid)
            log.info(f"{member.display_name} a quitté le salon tracké.")

# ── Weekly task ───────────────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def weekly_announcement():
    # CORRECTIF : on compare en heure UTC
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
    # CORRECTIF : publier dans le salon ranked
    ranked_channel = bot.get_channel(RANKED_CHANNEL_ID)
    if not ranked_channel:
        log.error("Salon ranked introuvable !")
        return

    if week is None:
        week = current_week_start()

    # CORRECTIF : récupère TOUS les élèves, pas juste le top 3
    all_ranked = get_all_ranked(week)

    if not all_ranked:
        await ranked_channel.send("😴 Personne n'a été actif dans le vocal cette semaine !")
        return

    top = all_ranked[:3]
    save_weekly_top(week, top)

    guild = ranked_channel.guild
    top_role = guild.get_role(TOP_ROLE_ID)

    # Rotation du rôle top 1
    prev_top1_id = get_previous_top1(week)
    if prev_top1_id and top_role:
        prev_member = guild.get_member(int(prev_top1_id))
        if prev_member and top_role in prev_member.roles:
            try:
                await prev_member.remove_roles(top_role, reason="Rotation top 1 vocal")
            except discord.Forbidden:
                log.warning("Permission manquante pour retirer le rôle.")

    if top_role and top:
        new_top1_member = guild.get_member(int(top[0]["user_id"]))
        if new_top1_member:
            try:
                await new_top1_member.add_roles(top_role, reason="Top 1 vocal de la semaine")
            except discord.Forbidden:
                log.warning("Permission manquante pour ajouter le rôle.")

    # Construction de l'embed avec TOUS les élèves
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, entry in enumerate(all_ranked):
        member = guild.get_member(int(entry["user_id"]))
        name = member.display_name if member else f"#{entry['user_id']}"
        prefix = medals[i] if i < 3 else f"`#{i+1}`"
        extra = f"  ← {top_role.mention}" if i == 0 and top_role else ""
        lines.append(f"{prefix} **{name}**{extra} — ⏱️ {fmt_duration(entry['seconds'])}")

    # Dernier du classement — mention spéciale
    if len(all_ranked) > 1:
        lines[-1] += "  ← 💀 Dernier"

    embed = discord.Embed(
        title="⚔️ CLASSEMENT RANKED — Semaine du " + week,
        description="\n".join(lines),
        color=discord.Color.gold(),
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text="Les compteurs se reset maintenant. Bonne semaine à tous.")

    await ranked_channel.send("@here", embed=embed)
    log.info("Classement ranked envoyé.")

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

@bot.tree.command(name="top", description="Affiche le classement vocal complet de la semaine")
async def top_command(interaction: discord.Interaction):
    week = current_week_start()
    all_ranked = get_all_ranked(week)
    if not all_ranked:
        await interaction.response.send_message("Aucune donnée pour cette semaine.", ephemeral=True)
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, entry in enumerate(all_ranked):
        member = interaction.guild.get_member(int(entry["user_id"]))
        name = member.display_name if member else f"#{entry['user_id']}"
        prefix = medals[i] if i < 3 else f"`#{i+1}`"
        lines.append(f"{prefix} **{name}** — {fmt_duration(entry['seconds'])}")
    embed = discord.Embed(
        title="⚔️ Classement vocal — semaine en cours",
        description="\n".join(lines),
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="announce", description="[ADMIN] Force l'annonce ranked maintenant")
@app_commands.checks.has_permissions(administrator=True)
async def force_announce(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await do_weekly_announcement()
    await interaction.followup.send("✅ Classement ranked envoyé !", ephemeral=True)

@force_announce.error
async def force_announce_error(interaction: discord.Interaction, error):
    await interaction.response.send_message("❌ Réservé aux administrateurs.", ephemeral=True)

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(TOKEN)
