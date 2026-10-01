"""The weekly review: Claude reads the knowledge base and suggests instruction changes.

The planner (planner.py) runs every night and follows instructions: standing rules,
per-track guidance, track blocks, weekly targets. This file is the slower loop that
keeps those instructions right. Once a week it reads the evidence:

  - my notes            ("/note too tired for Greek in the mornings")
  - session_patterns    how planned sessions actually went, by track and time of day,
                        computed from completions (the auto-logged skips/low confidence)
  - weekly minutes, completion rates, estimate accuracy, low-confidence items
  - its own last summary and which of its past suggestions I approved or rejected

...and calls propose_change for each instruction it thinks should change. Nothing
changes until I tap Approve in Telegram. Two loops at two speeds is a common agent
design: a fast "act" loop and a slow "reflect" loop that edits the fast one's prompt.

Why approval instead of auto-apply: a wrong inference ("Zak skipped Greek twice on
Saturday, so never plan Greek on Saturdays") would quietly shape weeks of plans.
One tap is cheap insurance, and rejected suggestions are fed back as evidence.

Usage:
  python reviewer.py --dry-run     # review a COPY of coach.db, change nothing
  python reviewer.py               # real run; suggestions then wait in /changes
Needs ANTHROPIC_API_KEY in .env (REVIEWER_PROVIDER=deepseek to use DeepSeek instead).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import db
import planner

ROOT = Path(__file__).parent
MAX_STEPS = 10
MAX_TOKENS = 8000
MAX_CHANGES_PER_REVIEW = 5

# The review runs once a week, so a stronger model costs pennies. Prices (US$ per
# million tokens) are for the cost estimate only: check the provider's pricing page
# and update them if they've changed.
REVIEWERS = {
    "anthropic": {"model": "claude-sonnet-5-5",
                  "prices": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30}},
    "deepseek": {"model": planner.PROVIDERS["deepseek"]["model"],
                 "prices": planner.PROVIDERS["deepseek"]["prices"]},
}
DEFAULT_REVIEWER = "anthropic"

SYSTEM_PROMPT = """\
You are the weekly reviewer for Zak's study planner. Zak is a UK sixth-form student. \
A separate planner model books his study sessions every night, following the \
instructions in `current_instructions`. Your job is to keep those instructions right, \
using the evidence in the knowledge base. You don't plan sessions.

What you can change (each with propose_change; Zak approves or rejects every one):
- add_rule {text} / remove_rule {rule_id}: standing soft instructions the planner \
follows (e.g. "Put Greek before Computer Science on the same evening").
- set_guidance {track_id, guidance}: replaces a track's whole guidance text. Keep \
everything in the old text that's still right; change only what the evidence supports.
- add_block {track_id, days, start, end} / remove_block {block_id}: hard rule, the \
planner can never put that track in that window. days: list from mon..sun, or \
"weekdays", "weekend", "all". Times HH:MM. Weekday study is 17:30-22:00, weekend 09:00-21:30.
- set_weekly_target {track_id, minutes}.

How to judge the evidence
- Zak's own words (new_notes, completion notes) are the strongest evidence. A clear \
statement like "too tired for Greek in the mornings" is enough on its own for one \
change; map it to the narrowest change that fixes it (a weekend-morning block for \
that track, not a block on all mornings for everything).
- Patterns need numbers: suggest a change from session_patterns_28d only when it \
shows at least 3 planned sessions in that slot and most were skipped, partial or \
low-confidence. Say the numbers in the reason ("Greek, weekend mornings: 3 of 4 skipped").
- Hard block vs soft rule: a block when Zak says he can't or won't do it then, or the \
skips are consistent; a rule for preferences and ordering.
- Respect past_decisions: don't re-suggest something Zak rejected unless there's new \
evidence, and then say what's new.
- Don't change things for the sake of it. A week with no clear signal gets no changes. \
At most {max_changes} suggestions.
- Never suggest anything that conflicts with deadlines or with an explicit instruction \
in a track's guidance unless Zak's own notes contradict it.

Finish by calling finish exactly once with a summary for Zak (3-6 short lines): what \
you noticed, what you suggested and why, and anything worth him knowing that isn't a \
change (e.g. "Maths learning is 40% under target three weeks running"). Plain \
language, second person, no preamble.
""".replace("{max_changes}", str(MAX_CHANGES_PER_REVIEW))

SCHEMAS = [
    {
        "name": "propose_change",
        "description": "Suggest one change to the planner's instructions. Zak approves "
                       "or rejects it. payload fields depend on action: add_rule {text}; "
                       "remove_rule {rule_id}; set_guidance {track_id, guidance}; add_block "
                       "{track_id, days, start, end}; remove_block {block_id}; "
                       "set_weekly_target {track_id, minutes}.",
        "input_schema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": list(db.CHANGE_ACTIONS)},
            "payload": {"type": "object", "properties": {
                "text": {"type": "string"}, "rule_id": {"type": "integer"},
                "track_id": {"type": "integer"}, "guidance": {"type": "string"},
                "days": {"type": "array", "items": {"type": "string"}},
                "start": {"type": "string", "description": "HH:MM"},
                "end": {"type": "string", "description": "HH:MM"},
                "block_id": {"type": "integer"}, "minutes": {"type": "integer"}}},
            "reason": {"type": "string",
                       "description": "One or two sentences Zak will read: the evidence, with numbers"}},
            "required": ["action", "payload", "reason"]},
    },
    {
        "name": "finish",
        "description": "End the review. summary: 3-6 short lines for Zak. Call exactly once, last.",
        "input_schema": {"type": "object", "properties": {
            "summary": {"type": "string"}}, "required": ["summary"]},
    },
]


@dataclass
class ReviewResult:
    summary: str
    change_ids: list[int]
    steps: int
    usage: dict = field(default_factory=dict)
    finished: bool = False
    prices: dict = field(default_factory=lambda: REVIEWERS[DEFAULT_REVIEWER]["prices"])

    @property
    def cost_usd(self) -> float:
        return planner.RunResult("", [], 0, self.usage, prices=self.prices).cost_usd


def _propose(conn, args: dict, review_id: int, made: list[int]) -> dict:
    if len(made) >= MAX_CHANGES_PER_REVIEW:
        return {"error": f"that's {MAX_CHANGES_PER_REVIEW} suggestions, the limit. Call finish."}
    try:
        cid = db.create_change(conn, args.get("action"), args.get("payload") or {},
                               args.get("reason", ""), review_id)
    except ValueError as e:
        return {"error": str(e)}
    except sqlite3.IntegrityError:
        return {"error": "that exact change is already suggested"}
    made.append(cid)
    return {"ok": True, "change_id": cid}


def run(conn, client, model: str, now: str, prices: dict | None = None,
        extra: dict | None = None, verbose: bool = False) -> ReviewResult:
    """The tool loop. Same shape as planner.run, smaller: two tools, few steps."""
    prices = prices or REVIEWERS[DEFAULT_REVIEWER]["prices"]
    db.expire_changes(conn)                       # last week's unanswered ones are stale
    snapshot = db.kb_snapshot(conn, now[:10])
    seen_note = max((n["note_id"] for n in snapshot["new_notes"]), default=0)
    # The review row exists from the start so its suggestions can point at it;
    # the summary is filled in at the end.
    review_id = db.save_review(conn, "(running)", False, now=now)

    messages = [{"role": "user", "content":
                 "Knowledge base:\n" + json.dumps(snapshot, indent=1)}]
    usage: dict = {}
    made: list[int] = []
    summary, finished, step, nudged = "", False, 0, False
    for step in range(1, MAX_STEPS + 1):
        response = client.messages.create(model=model, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT,
                                          tools=SCHEMAS, messages=messages, **(extra or {}))
        planner._add_usage(usage, response.usage)
        calls = [b for b in response.content if b.type == "tool_use"]
        messages.append({"role": "assistant", "content": response.content})
        if not calls:
            if nudged:                            # reminded once already: give up
                break
            nudged = True
            messages.append({"role": "user", "content": "Use propose_change for any "
                             "changes, then call finish with your summary."})
            continue
        results = []
        for call in calls:
            if call.name == "finish":
                summary, finished = str(call.input.get("summary", "")).strip(), True
                continue
            result = (_propose(conn, call.input, review_id, made) if call.name == "propose_change"
                      else {"error": f"unknown tool {call.name!r}"})
            if verbose:
                print(f"  [{step}] {call.name}({json.dumps(call.input)[:160]}) -> {result}")
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": json.dumps(result), "is_error": "error" in result})
        if finished:
            break
        messages.append({"role": "user", "content": results})

    if not finished:
        summary = f"Review stopped after {step} steps without finishing."
    result = ReviewResult(summary, made, step, usage, finished, prices)
    with db.transaction(conn):
        conn.execute("UPDATE reviews SET summary = ?, finished = ?, cost_usd = ? WHERE review_id = ?",
                     (summary, int(finished), round(result.cost_usd, 4), review_id))
    if finished and seen_note:
        db.mark_notes_reviewed(conn, seen_note, now)
    return result


# ---------------------------------------------------------------- setup + CLI

def setup_llm() -> planner.LLM:
    """Like planner.setup_llm, but for the reviewer's provider and model."""
    planner.load_env()
    name = os.environ.get("REVIEWER_PROVIDER", DEFAULT_REVIEWER)
    if name not in REVIEWERS:
        raise planner.PlannerError(f"REVIEWER_PROVIDER must be one of {', '.join(REVIEWERS)}")
    provider = planner.PROVIDERS[name]
    key = os.environ.get(provider["key_env"])
    if not key:
        raise planner.PlannerError(
            f"No {provider['key_env']} for the weekly review. Add it to .env, or set "
            f"REVIEWER_PROVIDER=deepseek to use the planner's key.")
    import anthropic
    return planner.LLM(name=name,
                       client=anthropic.Anthropic(api_key=key, base_url=provider["base_url"]),
                       model=os.environ.get("REVIEWER_MODEL", REVIEWERS[name]["model"]),
                       prices=REVIEWERS[name]["prices"], extra={}, thinking="default")


def review(conn, llm: planner.LLM, now: str, dry_run: bool = False,
           verbose: bool = False) -> ReviewResult:
    """One review against conn. Raises planner.PlannerError on API problems."""
    import anthropic
    try:
        result = run(conn, llm.client, llm.model, now, llm.prices, llm.extra, verbose)
    except anthropic.AuthenticationError:
        raise planner.PlannerError(f"{llm.name} rejected the API key.")
    except anthropic.APIConnectionError:
        raise planner.PlannerError(f"Couldn't reach {llm.name}.")
    except anthropic.APIStatusError as e:
        raise planner.PlannerError(f"{llm.name} returned an error ({e.status_code}): {e.message}")
    folder = ROOT / "logs"
    folder.mkdir(exist_ok=True)
    (folder / f"review-{now.replace(' ', '_').replace(':', '')}{'-dry' if dry_run else ''}.json"
     ).write_text(json.dumps({"now": now, "dry_run": dry_run, "finished": result.finished,
                              "summary": result.summary,
                              "changes": [db.get_change(conn, c) for c in result.change_ids],
                              "usage": result.usage, "cost_usd": round(result.cost_usd, 4)},
                             indent=2))
    return result


def main() -> None:
    p = argparse.ArgumentParser(description="Weekly knowledge-base review")
    p.add_argument("--dry-run", action="store_true", help="review a copy; change nothing")
    p.add_argument("--now", help="'YYYY-MM-DD HH:MM' (default: real now)")
    p.add_argument("--db", default=str(db.DEFAULT_DB))
    args = p.parse_args()

    now = args.now or db.local_now().strftime("%Y-%m-%d %H:%M")
    conn = planner.copy_db(Path(args.db)) if args.dry_run else db.connect(args.db)
    try:
        result = review(conn, setup_llm(), now, args.dry_run, verbose=True)
    except planner.PlannerError as e:
        raise SystemExit(str(e))
    print("\n" + result.summary + "\n")
    for cid in result.change_ids:
        ch = db.get_change(conn, cid)
        print(f"#{cid}  {db.describe_change(conn, ch)}\n     why: {ch['reason']}")
    print(f"\n~${result.cost_usd:.3f}"
          + ("  (dry run: nothing saved)" if args.dry_run
             else "  - approve with /changes in Telegram or `python db.py approve-change N`"))


if __name__ == "__main__":
    main()
