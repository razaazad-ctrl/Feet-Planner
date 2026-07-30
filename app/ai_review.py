"""
ai_review.py

The judgment-call layer that sits on top of the deterministic allocation
engine. It does NOT re-decide the hard-rule stuff (hours, off-days,
vehicle matching) -- the engine already got that right and is trustworthy
on its own. This layer looks at the engine's output plus extra context
the engine doesn't reason about, and produces SUGGESTIONS for the planner
to accept or reject -- it never silently changes anything on its own.

What it's given:
- The day's jobs, grouped by event, with their current assignments
- Each driver's current occupied hours / remaining capacity
- Real travel-time lookups for the relevant location pairs (from maps_client)
- The planner's free-text "day notes" for this specific day

What it returns: a list of structured suggestions, each with plain-language
reasoning, e.g. "Keep Deepak on-site for event 602102's teardown instead of
sending him back -- travel time back and forth is 45 min each way and the
gap between stages is only 90 min, so waiting is more efficient. Well
within his remaining hours."
"""
import json

from anthropic import Anthropic

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """You are assisting a fleet planner at a catering/events company who plans \
next-day vehicle and driver assignments. A deterministic rules engine has already produced a \
valid, rule-compliant assignment (no driver over their hour cap, no vehicle-type mismatches, \
off-days respected). Your job is NOT to re-check those hard rules -- trust that they're already \
correct. Your job is to reason about the judgment calls the rules engine can't make on its own:

1. For jobs that are part of the same event (same event_id) and assigned to the same driver at \
different times, decide whether it makes more sense for that driver/vehicle to WAIT ON-SITE \
between stages versus traveling back and being freed up for other work in between -- based on \
the travel time provided, the gap between stages, and how much spare capacity that driver has \
left that day. Each travel time lookup includes a "confidence" level -- "exact" means a precise, \
known address was used, "approximate (area-level)" means only a general area name was available. \
Be more cautious/conservative with suggestions based on approximate data -- e.g. don't force a \
tight-margin "cycle back" suggestion on an approximate estimate alone.
2. Read the planner's day notes (if any) and see whether they imply a specific, reasonable \
override to a normal rule for this one day (e.g. "VIP event, Deepak can go over his usual hours \
today"). Only propose an override if the note clearly supports it -- don't invent one.
3. Flag anything in the current plan that looks off given the real travel times -- e.g. a driver \
assigned two jobs back to back that are actually too far apart to make in time.
4. You may also be given a "planner preferences digest" -- a short summary of patterns in how \
this specific planner has actually decided similar situations before (which drivers they tend to \
keep on-site, which suppliers they favor, etc.). Treat this as useful guidance about this \
planner's real judgment, not a hard rule -- weigh it alongside the travel-time and hours data, \
and don't force a suggestion just because the digest mentions something if today's situation is \
genuinely different.

You NEVER change the plan yourself. You propose specific, individually-approvable suggestions, \
each with a short plain-language reason a busy planner can read in a few seconds. If nothing \
needs changing, say so plainly -- do not invent suggestions just to have something to say.

Respond ONLY with valid JSON (no markdown fences, no preamble) matching this shape:
{
  "suggestions": [
    {
      "type": "stay_on_site" | "cycle_back" | "day_note_override" | "flag_conflict" | "other",
      "affected_jobs": ["<sr numbers>"],
      "reasoning": "<one or two plain sentences a planner can read in a few seconds>"
    }
  ]
}
If there is nothing to suggest, return {"suggestions": []}.
"""


class AIReviewError(Exception):
    pass


def build_review_context(jobs, event_groups, driver_hours_summary, travel_time_lookups, day_notes,
                          preferences_digest=""):
    """
    Packs everything the AI needs into a compact JSON structure. Keeping
    this as plain data (not prose) keeps token usage predictable and
    makes the prompt easy to test without hitting the real API.

    preferences_digest: the small, fixed-size summary from
    digest_generator.py -- NOT the raw decision_log. This is what keeps
    daily token cost constant regardless of how much history exists.
    """
    events_payload = []
    for event_id, group_jobs in event_groups.items():
        if len(group_jobs) < 2:
            continue  # single-stage "events" have nothing to reason about
        events_payload.append({
            "event_id": event_id,
            "stages": [
                {
                    "sr": j.sr,
                    "start": j.start_dt.strftime("%H:%M") if j.start_dt else None,
                    "end": j.end_dt.strftime("%H:%M") if j.end_dt else None,
                    "location": j.order_location or j.pickup_location,
                    "assigned_driver": j.assignment_note,
                }
                for j in group_jobs
            ],
        })

    return {
        "events_with_multiple_stages": events_payload,
        "driver_occupied_hours_today": driver_hours_summary,
        "travel_time_lookups_minutes": travel_time_lookups,
        "planner_day_notes": day_notes or "",
        "planner_preferences_digest": preferences_digest or "(none yet)",
    }


def review_plan(api_key, context):
    """
    context: the dict produced by build_review_context().
    Returns a list of suggestion dicts. Raises AIReviewError on failure.
    """
    if not api_key:
        raise AIReviewError("No Anthropic API key configured. Add one in Settings.")

    client = Anthropic(api_key=api_key)

    user_message = (
        "Here is today's planning context as JSON:\n\n"
        + json.dumps(context, indent=2)
        + "\n\nReview it per your instructions and respond with the JSON shape described."
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as e:
        raise AIReviewError(f"Claude API call failed: {e}")

    text_parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    raw_text = "".join(text_parts).strip()

    # Defensive cleanup in case the model wraps the JSON in a code fence
    # despite instructions not to.
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.lower().startswith("json"):
            raw_text = raw_text[4:].strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise AIReviewError(f"Could not parse AI response as JSON: {e}\nRaw response: {raw_text[:500]}")

    return parsed.get("suggestions", [])
