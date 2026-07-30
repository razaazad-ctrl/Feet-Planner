"""
digest_generator.py

The decision_log table grows forever and is never sent to Claude directly
-- that would make every future call slower and more expensive as it
grows, exactly the problem the planner correctly flagged. Instead, this
module periodically (the planner triggers it manually, e.g. monthly)
reads whatever's been logged since the last refresh, asks Claude to fold
that into the EXISTING digest, and saves the result -- a short, roughly
fixed-size summary of the planner's demonstrated real-world preferences.

Daily AI Review calls only ever read this one small digest, never the
raw log. That's what keeps cost and latency flat whether it's month 1
or year 5, no matter how many decisions have accumulated.
"""
import json

from anthropic import Anthropic

MODEL = "claude-sonnet-5"
MAX_DIGEST_WORDS = 400

SYSTEM_PROMPT = f"""You maintain a compact "preferences digest" for a fleet planner -- a short \
summary of patterns in how they actually make judgment calls (e.g. which drivers they tend to \
keep on-site vs cycle back, which suppliers they favor or avoid for certain vehicle types, how \
quickly they tend to apply comp days, how they handle VIP events). This digest is read by an AI \
assistant before making suggestions each day, so it should be genuinely useful, specific \
guidance -- not vague generalities.

You will be given the CURRENT digest (may be empty, if this is the first refresh) and a batch of \
NEW logged decisions (each one: which suggestion was made, the reasoning behind it, and whether \
the planner accepted or rejected it). Merge the new decisions into the existing digest:
- Reinforce patterns that keep showing up
- Add new patterns that are clearly emerging
- Drop or soften anything that new decisions contradict
- Keep the WHOLE digest under {MAX_DIGEST_WORDS} words, no matter how much history feeds into it \
-- if it's getting long, keep only the clearest, most useful patterns and drop weaker/older ones
- Write it as plain prose a planner-reasoning AI can read as guidance, not as a list of raw events

Respond with ONLY the updated digest text. No preamble, no markdown headers, no JSON -- just the \
digest itself, ready to be stored and reused as-is.
"""


class DigestError(Exception):
    pass


def refresh_digest(api_key, conn, db):
    """
    Reads decisions logged since the last refresh, merges them into the
    existing digest via Claude, and saves the result. Returns the new
    digest text. If there are no new decisions, does nothing and returns
    the existing digest unchanged (no need to spend tokens on a no-op).
    """
    if not api_key:
        raise DigestError("No Anthropic API key configured. Add one in Settings.")

    existing = db.get_digest(conn)
    existing_text = existing["digest_text"] if existing else ""
    since_date = existing["covered_through_date"] if existing else None

    new_decisions = db.get_decisions_since(conn, since_date)
    if not new_decisions:
        return existing_text  # nothing new to fold in, don't waste a call

    decisions_payload = [
        {
            "date": d["plan_date"],
            "jobs": d["affected_jobs"],
            "suggestion_type": d["suggestion_type"],
            "reasoning": d["reasoning"],
            "planner_action": d["action"],
        }
        for d in new_decisions
    ]

    client = Anthropic(api_key=api_key)
    user_message = (
        f"CURRENT DIGEST:\n{existing_text or '(empty -- this is the first refresh)'}\n\n"
        f"NEW DECISIONS TO MERGE IN:\n{json.dumps(decisions_payload, indent=2)}"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=800,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as e:
        raise DigestError(f"Claude API call failed during digest refresh: {e}")

    text_parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    new_digest_text = "".join(text_parts).strip()

    latest_date = max(d["plan_date"] for d in new_decisions)
    db.save_digest(conn, new_digest_text, latest_date)
    return new_digest_text
