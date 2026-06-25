"""
bot.py — always-on Discord service for the Lanto tracker (Architecture A).

Replaces the Railway cron: it keeps a gateway connection for slash commands AND
runs the daily pipeline (run_pipeline.py) on an internal schedule, so everything
is one service. Command logic lives in commands.py; this file is the thin
Discord wrapper.

Env:
  DISCORD_TOKEN   bot token (same app that reads the calls channel)
  GUILD_ID        your server id -> slash commands appear instantly (recommended)
  OWNER_ID        your Discord user id -> restricts commands to you (recommended)
  SCHEDULE_HOUR   UTC hour to run the daily pipeline (default 12)
  DATA_DIR        volume path (default /data)
"""
import os, sys, asyncio, subprocess, datetime as dt
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

import commands as C

CENTRAL = ZoneInfo("America/Chicago")
GUILD_ID = int(os.environ.get("GUILD_ID", "0"))
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
SCHEDULE_HOUR = int(os.environ.get("SCHEDULE_HOUR", "16"))   # Central time
SCHEDULE_MIN = int(os.environ.get("SCHEDULE_MIN", "15"))
APP = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR", "/data")

intents = discord.Intents.default()           # no privileged intents needed
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

RANGE_HELP = "7d / 14d / 30d / 90d / ytd / mtd / all, or YYYY-MM-DD[:YYYY-MM-DD]"
INSTR = [app_commands.Choice(name=x, value=x) for x in ("both", "ES", "NQ")]
DIRN = [app_commands.Choice(name=x, value=x) for x in ("both", "long", "short")]


def _auth(interaction):
    return OWNER_ID == 0 or interaction.user.id == OWNER_ID


async def _go(interaction, fn, *args, ephemeral=True):
    if not _auth(interaction):
        return await interaction.response.send_message("Not authorized.", ephemeral=True)
    await interaction.response.defer(ephemeral=ephemeral)
    text, files = await asyncio.to_thread(fn, *args)
    dfiles = [discord.File(p) for p in files]
    await interaction.followup.send(text[:1900] or "(no output)", files=dfiles)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@tree.command(description="Bot + data health")
async def status(interaction):
    await _go(interaction, C.cmd_status)


@tree.command(description="Performance metrics for a date range / filter")
@app_commands.describe(range=RANGE_HELP, instrument="ES / NQ / both", direction="long / short / both")
@app_commands.choices(instrument=INSTR, direction=DIRN)
async def stats(interaction, range: str = "all", instrument: str = "both", direction: str = "both"):
    s, e = C.parse_range(range)
    await _go(interaction, C.cmd_stats, s, e, instrument, direction)


@tree.command(description="Equity curves (toggle series, filter, x-axis)")
@app_commands.describe(range=RANGE_HELP, series="comma list: his,tp1,tp2,tp1half,trail (or all)",
                       instrument="ES / NQ / both", direction="long / short / both",
                       x="date or trade")
@app_commands.choices(instrument=INSTR, direction=DIRN,
                      x=[app_commands.Choice(name="date", value="date"),
                         app_commands.Choice(name="trade", value="trade")])
async def equity(interaction, range: str = "all", series: str = "all",
                 instrument: str = "both", direction: str = "both", x: str = "date"):
    s, e = C.parse_range(range)
    await _go(interaction, C.cmd_equity, s, e, instrument, direction, series, x == "date")


@tree.command(description="List trades in a range (table, or CSV with csv:true)")
@app_commands.describe(range=RANGE_HELP, series="columns: his,tp1,tp2,tp1half,trail (or all)",
                       instrument="ES / NQ / both", direction="long / short / both",
                       limit="rows to show (table mode)", csv="attach full CSV instead")
@app_commands.choices(instrument=INSTR, direction=DIRN)
async def trades(interaction, range: str = "30d", series: str = "tp1,his",
                 instrument: str = "both", direction: str = "both",
                 limit: int = 15, csv: bool = False):
    s, e = C.parse_range(range)
    await _go(interaction, C.cmd_trades, s, e, instrument, direction, limit, csv, series)


@tree.command(description="Trades most likely mis-scored (low confidence / unclear)")
async def review(interaction, limit: int = 15):
    await _go(interaction, C.cmd_review, limit)


@tree.command(description="Detection funnel: entries posted vs scored vs missed")
@app_commands.describe(range=RANGE_HELP)
async def coverage(interaction, range: str = "all"):
    s, e = C.parse_range(range)
    await _go(interaction, C.cmd_coverage, s, e)


@tree.command(description="Manually correct a trade (becomes ground truth)")
@app_commands.describe(msg_id="trade msg_id (from /trades or /review)",
                       outcome_type="tp_hit/stopped/breakeven/explicit_r/unclear",
                       realized_r="his realized R", instrument="ES/NQ", direction="long/short",
                       entry="entry price", stop="stop price", tp1="tp1 price")
async def flag(interaction, msg_id: str, outcome_type: str = "", realized_r: str = "",
               instrument: str = "", direction: str = "", entry: str = "",
               stop: str = "", tp1: str = ""):
    kw = dict(outcome_type=outcome_type, realized_R=realized_r, instrument=instrument,
              direction=direction, entry=entry, stop=stop, tp1=tp1)
    await _go(interaction, lambda: C.cmd_flag(msg_id, **kw))


@tree.command(description="Run the pipeline now (fetch prices, score, post)")
async def run(interaction):
    if not _auth(interaction):
        return await interaction.response.send_message("Not authorized.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    rc = await asyncio.to_thread(_run_pipeline)
    await interaction.followup.send(f"Pipeline finished (exit {rc}). Check the report channel.")


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #
def _run_pipeline():
    return subprocess.run([sys.executable, os.path.join(APP, "run_pipeline.py")],
                          cwd=DATA).returncode


@tasks.loop(time=dt.time(hour=SCHEDULE_HOUR, minute=SCHEDULE_MIN, tzinfo=CENTRAL))
async def daily_job():
    print("daily_job: running pipeline", flush=True)
    await asyncio.to_thread(_run_pipeline)


@client.event
async def on_ready():
    if GUILD_ID:
        g = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=g)
        await tree.sync(guild=g)
    else:
        await tree.sync()
    if not daily_job.is_running():
        daily_job.start()
    print(f"logged in as {client.user}; commands synced; "
          f"daily job at {SCHEDULE_HOUR:02d}:{SCHEDULE_MIN:02d} Central", flush=True)


def main():
    token = os.environ["DISCORD_TOKEN"]
    client.run(token)


if __name__ == "__main__":
    main()
