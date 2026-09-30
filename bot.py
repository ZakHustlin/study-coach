"""Telegram bot: the phone interface to the study coach.

Run it with:  python bot.py      (keeps running; Ctrl+C to stop)

What it does
- 22:00 every day: the check-in. One message per session, answered with buttons
  (see checkin.py). When the last one is logged it re-plans and syncs the calendar.
- 23:00 fallback: if you never finished the check-in, it re-plans anyway.
- Commands: /checkin /today /plan /proposals /rule /rules /unrule /homework
  /covered /help

Key decisions
- Long polling: the bot keeps asking Telegram "anything new?" instead of Telegram
  calling a public URL (a webhook). So it runs anywhere, Codespaces included, with no
  web server. The cost is that it only works while `python bot.py` is running,
  which is why step 6 moves it to an always-on VPS.
- Private: the first chat to send /start becomes the owner (saved in
  bot_state.json, gitignored). Every other chat is ignored, so a stranger who
  finds the bot can't read or change your plan.
- The slow bits (the LLM re-plan, ~1 min, and the calendar sync) run in a worker
  thread via asyncio.to_thread, so the bot stays responsive meanwhile. Each thread
  opens its own SQLite connection: a connection shouldn't be shared across threads.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import time as dtime, timedelta
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

import availability
import checkin
import db
import planner

ROOT = Path(__file__).parent
STATE = ROOT / "bot_state.json"
TZ = ZoneInfo("Europe/London")
CHECKIN_AT = dtime(22, 0, tzinfo=TZ)
FALLBACK_REPLAN_AT = dtime(23, 0, tzinfo=TZ)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)   # don't log every poll (or the token in URLs)
log = logging.getLogger("bot")

_replan_lock = asyncio.Lock()


# ---------------------------------------------------------------- state + helpers

def load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(**changes) -> None:
    STATE.write_text(json.dumps({**load_state(), **changes}))


def owner_id() -> int | None:
    return load_state().get("chat_id")


def owner_only(handler):
    """Ignore every chat except the owner's."""
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat.id if update.effective_chat else None
        if owner_id() is None and update.message:
            # Not linked yet: say so, instead of silently ignoring
            log.info("Command from chat %s before /start", chat)
            await update.message.reply_text("I'm not linked to anyone yet. Send /start first.")
            return
        if chat != owner_id():
            log.info("Ignored chat %s (owner is %s)", chat, owner_id())
            return
        log.info("Handling %s", handler.__name__)
        return await handler(update, context)
    return wrapper


def markup(screen: checkin.Screen) -> InlineKeyboardMarkup | None:
    if not screen.buttons:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data)
                                  for label, data in row] for row in screen.buttons])


async def send(context, screen: checkin.Screen) -> None:
    await context.bot.send_message(owner_id(), screen.text, reply_markup=markup(screen))


def today() -> str:
    return db.local_now().date().isoformat()


# ---------------------------------------------------------------- re-plan + sync (worker thread)

def sync_calendar(conn) -> str:
    """Push planned/cancelled sessions to Google Calendar. Returns a one-line status."""
    import gcal
    try:
        tz = availability.load_config()["timezone"]
        result = gcal.sync(conn, gcal.service(), gcal.calendar_id(), tz)
        return f"Calendar updated: {result['created']} added, {result['removed']} removed."
    except SystemExit as e:                  # gcal's "run gcal.py auth/setup" messages
        return f"Calendar not updated: {e}"
    except Exception as e:                   # usually RefreshError: the 7-day sign-in expired
        log.warning("calendar sync failed: %r", e)
        return ("Calendar not updated: Google sign-in has probably expired. "
                "Run `python gcal.py auth` then `python gcal.py sync`.")


def replan_and_sync() -> str:
    conn = db.connect()
    try:
        now = db.local_now().strftime("%Y-%m-%d %H:%M")
        result = planner.replan(conn, planner.setup_llm(), now)
        status = "" if result.finished else "\n(The planner stopped early; check logs/.)"
        return (f"{result.summary}{status}\n\n{sync_calendar(conn)}\n"
                f"({len(result.changes)} changes, ~${result.cost_usd:.3f})")
    except planner.PlannerError as e:
        return f"Re-plan failed: {e}"
    finally:
        conn.close()


async def run_replan(context) -> None:
    if _replan_lock.locked():
        await context.bot.send_message(owner_id(), "A re-plan is already running.")
        return
    async with _replan_lock:
        await context.bot.send_message(owner_id(), "Re-planning the rest of the week (about a minute)...")
        text = await asyncio.to_thread(replan_and_sync)
        save_state(last_replan=today())
        await context.bot.send_message(owner_id(), text)
        conn = db.connect()
        for screen in checkin.proposal_screens(conn):
            await send(context, screen)
        conn.close()


# ---------------------------------------------------------------- scheduled jobs

async def checkin_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if owner_id() is None:
        return
    conn = db.connect()
    screen = checkin.next_screen(conn, today())
    conn.close()
    if screen is None:
        await context.bot.send_message(owner_id(), "No sessions to log today.")
        await run_replan(context)
    else:
        await context.bot.send_message(owner_id(), "Evening check-in 🕙")
        await send(context, screen)


async def fallback_replan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if owner_id() is not None and load_state().get("last_replan") != today():
        await context.bot.send_message(owner_id(), "Check-in wasn't finished; re-planning with what's logged.")
        await run_replan(context)


# ---------------------------------------------------------------- commands

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat.id
    if owner_id() is None:
        save_state(chat_id=chat)
        await update.message.reply_text(
            "Hi Zak. This chat is now linked to your study coach; I'll ignore everyone else.\n"
            f"Check-in every day at {CHECKIN_AT:%H:%M}. /help for commands.")
    elif chat == owner_id():
        await update.message.reply_text("Already linked. /help for commands.")
    # anyone else: silence


@owner_only
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/checkin: log today's sessions now\n"
        "/today: today's sessions and briefs\n"
        "/plan: re-plan the rest of the week now\n"
        "/proposals: things waiting for your yes/no\n"
        "/homework TITLE MINUTES YYYY-MM-DD: add homework\n"
        "/covered: mark set-text passages done in class\n"
        "/rule TEXT: standing instruction to the planner\n"
        "/rules: list rules   /unrule N: remove one")


@owner_only
async def checkin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db.connect()
    screen = checkin.next_screen(conn, today())
    conn.close()
    if screen is None:
        await update.message.reply_text("Nothing left to log today. /plan to re-plan anyway.")
    else:
        await send(context, screen)


@owner_only
async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db.connect()
    d = db.local_now().date()
    rows = db.sessions_between(conn, d.isoformat(), (d + timedelta(days=1)).isoformat())
    conn.close()
    if not rows:
        await update.message.reply_text("Nothing planned today.")
        return
    for r in rows:        # one message per session so each brief is easy to copy
        await update.message.reply_text(
            f"{r['start_at'][11:]}–{r['end_at'][11:]} · {r['track']}\n{r['title']}"
            + (f"\n\n{r['brief']}" if r["brief"] else ""))


@owner_only
async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_replan(context)


@owner_only
async def proposals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db.connect()
    screens = checkin.proposal_screens(conn)
    conn.close()
    if not screens:
        await update.message.reply_text("No proposals waiting.")
    for screen in screens:
        await send(context, screen)


@owner_only
async def homework_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    usage = "Usage: /homework TITLE MINUTES YYYY-MM-DD\ne.g. /homework CS essay 60 2026-10-08"
    if len(args) < 3:
        await update.message.reply_text(usage)
        return
    title, minutes, due = " ".join(args[:-2]), args[-2], args[-1]
    try:
        from datetime import date
        date.fromisoformat(due)
        minutes = int(minutes)
    except ValueError:
        await update.message.reply_text(usage)
        return
    conn = db.connect()
    track = next((t for t in db.list_tracks(conn) if t["name"] == "Schoolwork"), None)
    if track is None:
        await update.message.reply_text("No Schoolwork track.")
    else:
        item_id = db.add_item(conn, track["track_id"], title, minutes, due_date=due)
        await update.message.reply_text(f"Added homework {item_id}: {title}, {minutes} min, "
                                        f"due {due}. It'll be planned at the next re-plan (/plan to do it now).")
    conn.close()


@owner_only
async def rule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /rule No maths on Friday evenings")
        return
    conn = db.connect()
    rid = db.add_rule(conn, text)
    conn.close()
    await update.message.reply_text(f"Rule {rid} added: {text}\nApplies from the next re-plan.")


@owner_only
async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db.connect()
    rules = db.list_rules(conn)
    conn.close()
    await update.message.reply_text(
        "\n".join(f"{r['rule_id']}. {r['text']}" for r in rules) or "No rules yet. /rule TEXT")


@owner_only
async def unrule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db.connect()
    try:
        db.remove_rule(conn, int(context.args[0]))
        await update.message.reply_text(f"Rule {context.args[0]} removed.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /unrule N  (see /rules)")
    finally:
        conn.close()


@owner_only
async def covered_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Buttons for the next few uncovered passages of each text."""
    conn = db.connect()
    rows = []
    for strand in {p["strand"] for p in db.list_passages(conn)}:
        upcoming = [p for p in db.list_passages(conn, strand) if not p["covered_on"]][:3]
        rows += [[InlineKeyboardButton(f"up to {p['ref']}", callback_data=f"v:{p['passage_id']}")]
                 for p in upcoming]
    conn.close()
    if not rows:
        await update.message.reply_text("No passages left to mark.")
        return
    await update.message.reply_text("How far did you get in class?",
                                    reply_markup=InlineKeyboardMarkup(sorted(rows, key=lambda r: r[0].text)))


# ---------------------------------------------------------------- button presses

@owner_only
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()                       # stops the button's loading spinner
    data = query.data
    conn = db.connect()
    try:
        if data.startswith("v:"):              # passage covered
            n = db.mark_covered(conn, int(data[2:]))
            summary = "\n".join(f"{c['strand']}: {c['covered_count']}/{c['total']} covered"
                                for c in db.coverage(conn))
            await query.edit_message_text(f"Marked {n} passage(s) covered.\n{summary}")
            return
        result = checkin.handle(conn, data, today())
        await query.edit_message_text(result.screen.text, reply_markup=markup(result.screen))
        if result.changed_plan:
            await context.bot.send_message(owner_id(), await asyncio.to_thread(_sync_only))
    finally:
        conn.close()
    if result.finished:
        await run_replan(context)


def _sync_only() -> str:
    conn = db.connect()
    try:
        return sync_calendar(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------- main

def main() -> None:
    planner.load_env()
    import os
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("No TELEGRAM_BOT_TOKEN in .env")

    app = Application.builder().token(token).build()
    for name, fn in [("start", start), ("help", help_cmd), ("checkin", checkin_cmd),
                     ("today", today_cmd), ("plan", plan_cmd), ("proposals", proposals_cmd),
                     ("homework", homework_cmd), ("rule", rule_cmd), ("rules", rules_cmd),
                     ("unrule", unrule_cmd), ("covered", covered_cmd)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))

    app.job_queue.run_daily(checkin_job, CHECKIN_AT, name="checkin")
    app.job_queue.run_daily(fallback_replan_job, FALLBACK_REPLAN_AT, name="fallback")

    log.info("Bot running. Owner chat: %s. Check-in at %s.", owner_id() or "(send /start)", CHECKIN_AT)
    app.run_polling()


if __name__ == "__main__":
    main()
