"""The evening re-plan: an LLM agent that plans the rest of the week using tools.py.

How the tool loop works (this is the core of any "agent"):

  1. Send the model: a system prompt (its job + rules), the tool schemas, and a
     first message holding the current state.
  2. The model replies. If the reply contains tool calls, run each one with
     tools.run_tool() and send the results back as the next message.
  3. Repeat until the model calls finish(), or a safety limit is hit.

The model never runs code or touches the database. It only *asks* for tool calls;
this file decides whether to run them, and tools.py enforces the rules.

Usage:
  python planner.py --dry-run      # plan against a COPY of coach.db, change nothing
  (needs DEEPSEEK_API_KEY=... in .env, or PLANNER_PROVIDER=anthropic + ANTHROPIC_API_KEY)
  python planner.py                # plan for real, then run `python gcal.py sync`
  python planner.py --dry-run --now "2026-10-01 21:45"   # pretend it's another time
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import availability
import db
import tools

ROOT = Path(__file__).parent
MAX_STEPS = 25            # model replies per run; stops a confused model looping forever
MAX_TOKENS = 16000        # per reply. Reasoning models "think" before answering and
                          # that counts as output, so this needs headroom. You only pay
                          # for tokens actually used, not for the limit.

# Which LLM to use. DeepSeek offers an Anthropic-compatible endpoint, so the same
# `anthropic` library and the same loop work for both: only the address, key and
# model name change. Choose with PLANNER_PROVIDER in .env (default: deepseek).
# prices: US$ per million tokens, for the cost estimate only. Update if they change.
PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/anthropic",
        "key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-flash",
        # off-peak rates; 21:45 UK time is off-peak
        "prices": {"input": 0.15, "output": 0.60, "cache_write": 0.15, "cache_read": 0.003},
    },
    "anthropic": {
        "base_url": None,
        "key_env": "ANTHROPIC_API_KEY",
        "model": "claude-haiku-4-5-20251001",
        "prices": {"input": 1.00, "output": 5.00, "cache_write": 1.25, "cache_read": 0.10},
    },
}
DEFAULT_PROVIDER = "deepseek"

SYSTEM_PROMPT = """\
You are Zak's study planner. Zak is a UK sixth-form student. Each evening you re-plan \
the rest of his week (up to the date in `plan_until`) across his study tracks.

How to plan
- Start from the state in the first message: tracks, weekly targets and progress, \
this week's priorities (if Zak set any), sessions already planned, free time.
- `standing_rules` are Zak's instructions for every plan. Follow them always; they \
override everything below except the hard rules the tools enforce. If a rule makes \
something impossible (e.g. a homework can't fit), plan the best you can and say so \
in the summary.
- Each track's `guidance` is Zak's instructions for that track. Follow it: it \
overrides the general rules below (e.g. "every day, 30 min" means spread, not batch).
- Weekly priorities from Zak come first. Then deadlines. Then tracks furthest behind \
their weekly target, weighted by track priority (5 = most important).
- Tracks with strands: rotate between them using `strand_balance_14d`, favouring the \
strand with the fewest recent minutes or the oldest last_touched.
- `set_text_coverage` lists which set-text passages Zak has covered in class. Tests \
and revision on a set text use ONLY covered passages: name the covered refs in the \
brief so the tutor knows what's fair game. If a text has nothing covered yet, give \
that slot to another strand. Never book first-time translation of an uncovered passage.
- Some items are open-ended (a very large estimate, e.g. a whole book or ongoing \
vocab work). Keep scheduling chunks of them; never propose marking them done.
- Keep what's already planned unless there's a reason to change it. Don't churn the \
calendar: move a session only if it clearly improves the week.
- Prefer 45-90 minute sessions. Split items bigger than ~90 minutes across days. \
Vary tracks within an evening rather than stacking one subject.
- Low confidence (1-2) on a finished topic, or a note like "didn't get it", is a \
reason to add a short revision item.
- Leave slack. Don't fill every free minute; Zak is at school all day.
- Plan ONE DAY AT A TIME, in date order: decide that day's sessions, book them with \
tool calls, then move on to the next day. Don't work out the whole week in your \
head first. Keep your reasoning short; the tools check the rules for you.
- Anything that follows on from other work (marking a paper, reviewing a topic) goes \
after that work, never before. Don't book something and then move it in the same run.
- Every session needs a brief. Zak pastes it into an AI tutor to run the session, \
so write it as that request: what to open (text, pages, lines, spec point), the \
task, and what "done" looks like, in 1-3 sentences. E.g. "Quiz me on the fetch-\
decode-execute cycle and the role of each register (OCR 1.1.1), then set 5 exam-\
style questions and mark my answers." For open-ended items, pick the next \
concrete chunk (e.g. "Read pages 60-90 of the Odyssey") based on the item history. \
Only the NEXT session of an open-ended item gets exact pages or lines; later ones say \
"continue from where you stopped", because a skipped session would make them wrong.
- Book every homework item that fits in this window, even if it's due next week: \
homework left for later competes with next week's homework.

Rules
- Tools enforce the hard rules (free time, daily cap, gaps, due dates, locked \
sessions). If a call returns an error, read it and try something else. Don't repeat \
the same failing call.
- Never touch locked sessions. To drop work without replacing it, pause a track, or \
mark an item done, use propose: Zak decides.
- Finish by calling finish exactly once, with a 2-5 line summary for Zak: what you \
changed and why. Plain language, no preamble.
"""


# ---------------------------------------------------------------- the first message

def initial_state(ctx: tools.Context) -> dict:
    """What the model sees before its first tool call. Front-loading this costs a
    few thousand tokens once, instead of the model spending tool calls to find it."""
    today = ctx.today.isoformat()
    until = ctx.horizon_end.isoformat()
    snap = db.planner_snapshot(ctx.conn, today)
    return {
        "now": ctx.now,
        "weekday": ctx.today.strftime("%A"),
        "plan_until": until,
        "standing_rules": [r["text"] for r in db.list_rules(ctx.conn)],
        "weekly_priorities": snap["weekly_priorities"] or "(Zak hasn't set any this week)",
        "tracks_this_week": db.week_progress(ctx.conn, today, ctx.now),
        "strand_balance_14d": db.strand_balance(ctx.conn, today),
        "set_text_coverage": db.coverage(ctx.conn),
        "planned_sessions": tools.get_sessions(ctx, today, until)["sessions"],
        "free_time": tools.get_free_slots(ctx, today, until)["days"],
        "available_items": db.available_items(ctx.conn, ctx.now),
        "completion_rates_14d": snap["completion_rates_14d"],
        "estimate_accuracy": snap["estimate_accuracy"],
        "needs_review": snap["needs_review"],
        "today_log": snap["today_summary"],
        "pending_proposals": snap["pending_proposals"],
    }


# ---------------------------------------------------------------- the loop

@dataclass
class RunResult:
    summary: str
    changes: list[dict]
    steps: int
    usage: dict = field(default_factory=dict)
    finished: bool = False
    trace: list[dict] = field(default_factory=list)   # per step: why it stopped, what it sent
    prices: dict = field(default_factory=lambda: PROVIDERS[DEFAULT_PROVIDER]["prices"])

    @property
    def cost_usd(self) -> float:
        u, p = self.usage, self.prices
        return (u.get("input", 0) * p["input"] + u.get("output", 0) * p["output"]
                + u.get("cache_write", 0) * p["cache_write"]
                + u.get("cache_read", 0) * p["cache_read"]) / 1_000_000


def _add_usage(total: dict, usage) -> None:
    total["input"] = total.get("input", 0) + (usage.input_tokens or 0)
    total["output"] = total.get("output", 0) + (usage.output_tokens or 0)
    total["cache_write"] = total.get("cache_write", 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    total["cache_read"] = total.get("cache_read", 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)


def _append_user_text(messages: list, text: str) -> None:
    """Add text to the last (user) message. Messages must alternate user/assistant,
    so after dropping an assistant reply we extend the user turn instead."""
    last = messages[-1]
    if isinstance(last["content"], str):
        last["content"] += "\n\n" + text
    else:
        last["content"] = list(last["content"]) + [{"type": "text", "text": text}]


def run(ctx: tools.Context, client, model: str = PROVIDERS[DEFAULT_PROVIDER]["model"],
        verbose: bool = False, prices: dict | None = None,
        extra: dict | None = None) -> RunResult:
    """extra: additional request fields, e.g. {"thinking": {"type": "disabled"}}."""
    prices = prices or PROVIDERS[DEFAULT_PROVIDER]["prices"]
    extra = extra or {}
    # Prompt caching: the system prompt and tool list are identical on every step,
    # so we mark them cacheable. From step 2 onwards they're read from cache at a
    # tenth of the normal input price instead of being paid for in full again.
    system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    tool_defs = [dict(s) for s in tools.SCHEMAS]
    tool_defs[-1]["cache_control"] = {"type": "ephemeral"}

    messages = [{"role": "user", "content":
                 "Current state:\n" + json.dumps(initial_state(ctx), indent=1)}]
    usage: dict = {}
    trace: list[dict] = []
    nudged = False
    overflows = 0

    for step in range(1, MAX_STEPS + 1):
        response = client.messages.create(model=model, max_tokens=MAX_TOKENS, system=system,
                                          tools=tool_defs, messages=messages, **extra)
        _add_usage(usage, response.usage)
        calls = [b for b in response.content if b.type == "tool_use"]
        stop = getattr(response, "stop_reason", None)
        trace.append({"step": step, "stop_reason": stop,
                      "blocks": [b.type for b in response.content],
                      "output_tokens": response.usage.output_tokens,
                      "text": " ".join(getattr(b, "text", "") or getattr(b, "thinking", "") or ""
                                       for b in response.content)[:500]})
        if verbose:
            print(f"  [{step}] stop={stop} blocks={[b.type for b in response.content]} "
                  f"out={response.usage.output_tokens}")
            for b in response.content:
                words = getattr(b, "text", None) or getattr(b, "thinking", None)
                if words and words.strip():
                    print(f"  [{step}] {b.type}: {words.strip()[:200]}")

        if not calls and stop == "max_tokens":
            # Ran out of room before calling any tool (usually over-long reasoning).
            # Drop the cut-off reply and ask again, more narrowly, up to twice.
            overflows += 1
            if overflows > 2:
                break
            _append_user_text(messages, "Your last reply ran out of space before any tool "
                              "call. Don't plan the whole week at once: book the next "
                              "day's sessions now with tool calls, then continue.")
            continue

        messages.append({"role": "assistant", "content": response.content})
        if not calls:
            # The model stopped without calling finish. Remind it once, then give up.
            if nudged:
                break
            nudged = True
            messages.append({"role": "user", "content":
                             "Use the tools to make the plan, then call finish with your summary."})
            continue

        results = []
        finish = next((c for c in calls if c.name == "finish"), None)
        for call in calls:
            if call.name == "finish":
                continue              # run the real work first, even if finish came first
            result = tools.run_tool(ctx, call.name, call.input)
            if verbose:
                print(f"  [{step}] {call.name}({json.dumps(call.input)[:150]}) -> "
                      f"{json.dumps(result)[:150]}")
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": json.dumps(result), "is_error": "error" in result})
        if finish is not None:
            summary = str(finish.input.get("summary", "")).strip()
            return RunResult(summary, ctx.log, step, usage, finished=True, prices=prices, trace=trace)
        # Every tool_use must be answered by a tool_result in the very next message.
        messages.append({"role": "user", "content": results})

    reason = ("the reply hit the max_tokens limit" if trace and trace[-1]["stop_reason"] == "max_tokens"
              else "step limit reached" if step == MAX_STEPS
              else "the model replied without calling any tool")
    return RunResult(f"Stopped before finishing: {reason}. See the trace in the log.",
                     ctx.log, step, usage, finished=False, prices=prices, trace=trace)


# ---------------------------------------------------------------- setup + CLI

def load_env(path: Path = ROOT / ".env") -> None:
    """Read KEY=value lines from .env into the environment (doesn't overwrite)."""
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def copy_db(src: Path) -> sqlite3.Connection:
    """In-memory copy of the database, for dry runs. Uses SQLite's backup API,
    which copies safely even if another program has the file open."""
    source = db.connect(src)
    copy = sqlite3.connect(":memory:")
    source.backup(copy)
    source.close()
    copy.row_factory = sqlite3.Row
    copy.execute("PRAGMA foreign_keys = ON")
    return copy


def save_log(result: RunResult, now: str, dry_run: bool) -> Path:
    folder = ROOT / "logs"
    folder.mkdir(exist_ok=True)
    path = folder / f"plan-{now.replace(' ', '_').replace(':', '')}{'-dry' if dry_run else ''}.json"
    path.write_text(json.dumps({
        "now": now, "dry_run": dry_run, "finished": result.finished, "steps": result.steps,
        "summary": result.summary, "changes": result.changes, "usage": result.usage,
        "cost_usd": round(result.cost_usd, 4), "trace": result.trace}, indent=2))
    return path


class PlannerError(Exception):
    """A problem worth showing the user as one line (bad key, no connection...)."""


@dataclass
class LLM:
    name: str
    client: object
    model: str
    prices: dict
    extra: dict
    thinking: str


def setup_llm(model: str | None = None, thinking: str | None = None) -> LLM:
    """Build the API client from .env. Shared by the CLI and the Telegram bot."""
    load_env()
    name = os.environ.get("PLANNER_PROVIDER", DEFAULT_PROVIDER)
    if name not in PROVIDERS:
        raise PlannerError(f"PLANNER_PROVIDER must be one of {', '.join(PROVIDERS)}")
    provider = PROVIDERS[name]
    key = os.environ.get(provider["key_env"])
    if not key:
        raise PlannerError(f"No {provider['key_env']}. Add it to .env (see README).")
    import anthropic                       # imported here so tests don't need it
    # PLANNER_THINKING=off asks the model to answer without a hidden reasoning phase.
    # Cheaper and can't overflow, but may plan less carefully: compare with --dry-run.
    thinking = (thinking or os.environ.get("PLANNER_THINKING", "on")).lower()
    return LLM(name=name,
               client=anthropic.Anthropic(api_key=key, base_url=provider["base_url"]),
               model=model or os.environ.get("PLANNER_MODEL", provider["model"]),
               prices=provider["prices"],
               extra={"thinking": {"type": "disabled"}} if thinking == "off" else {},
               thinking=thinking)


def replan(conn, llm: LLM, now: str, dry_run: bool = False, verbose: bool = False) -> RunResult:
    """One evening re-plan against conn. Raises PlannerError on API problems."""
    import anthropic
    if not dry_run:
        # Unanswered proposals from earlier days were about a plan that has moved on.
        db.expire_proposals(conn, created_before=now[:10])
    ctx = tools.Context(conn, availability.load_config(), now)
    if verbose:
        print(f"Planning {ctx.today} -> {ctx.horizon_end} with {llm.model}, "
              f"thinking {llm.thinking}{' (DRY RUN)' if dry_run else ''}")
    try:
        result = run(ctx, llm.client, llm.model, verbose=verbose, prices=llm.prices,
                     extra=llm.extra)
    except anthropic.AuthenticationError:
        raise PlannerError(f"{llm.name} rejected the API key. Check {PROVIDERS[llm.name]['key_env']} in .env.")
    except anthropic.APIConnectionError:
        raise PlannerError(f"Couldn't reach {llm.name}. Check the internet connection.")
    except anthropic.APIStatusError as e:
        raise PlannerError(f"{llm.name} returned an error ({e.status_code}): {e.message}")
    save_log(result, now, dry_run)
    return result


def main() -> None:
    p = argparse.ArgumentParser(description="Evening re-plan")
    p.add_argument("--dry-run", action="store_true", help="plan on a copy; change nothing")
    p.add_argument("--now", help="'YYYY-MM-DD HH:MM' (default: real now)")
    p.add_argument("--db", default=str(db.DEFAULT_DB))
    p.add_argument("--model", default=None, help="override the provider's default model")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--thinking", choices=["on", "off"], help="default: PLANNER_THINKING or on")
    args = p.parse_args()

    now = args.now or db.local_now().strftime(tools.FMT)
    conn = copy_db(Path(args.db)) if args.dry_run else db.connect(args.db)
    try:
        llm = setup_llm(args.model, args.thinking)
        result = replan(conn, llm, now, args.dry_run, verbose=not args.quiet)
    except PlannerError as e:
        raise SystemExit(str(e))

    print("\n" + result.summary)
    print(f"\n{len(result.changes)} change(s), {result.steps} step(s), "
          f"{sum(result.usage.values()):,} tokens, ~${result.cost_usd:.3f}")
    if not args.dry_run and result.changes:
        print("Run `python gcal.py sync` to update your calendar.")


if __name__ == "__main__":
    main()
