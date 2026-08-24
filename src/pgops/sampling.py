"""MCP sampling — the server asking the *client's* model for a completion.

Worth stating the direction plainly because it is commonly assumed backwards: sampling
does not let the server run its own model, and it is not a way to stop the client's model
running. It is the reverse — a server→client request where the client's model does the
inference. The practical consequence is the good one: **pgops ships no API key, no model
config and no inference bill.** It borrows whatever model the user already has, and the
user's client remains in control of approving each request.

That property is also the reason to use it sparingly. Every sampling call spends the
user's tokens on their model, and the caller is usually a model already — asking a model
to explain something to a model earns its keep only where a real translation happens.

Two places here do:

1. `migration.describe` — turning English into the structured `target` schema that
   `migration.plan` consumes. The target format is fiddly and hand-writing it is the
   main friction in the migration workflow.
2. `query.explain(summarize=True)` — turning a plan tree into prose.

**The safety rule for every use: sampled text is never executed.** A model's output is
untrusted input, no different from a user's. In `migration.describe` the sampled value
is a *structured target*, which goes through the same `_validate_target` and the same
deterministic differ, lock analysis and confirmation gate as a hand-written one. The
model proposes a destination; it never writes the SQL, and it cannot skip a guardrail
that a human-authored target would have hit. If sampling produced SQL directly it would
route around the entire safety architecture, which is why it does not.

Sampling is optional in MCP and many clients do not implement it. Every helper here
returns `None` rather than raising, so a missing capability degrades the feature and
never breaks the tool.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("pgops.sampling")

# Long enough for a schema target or a paragraph of prose; short enough that a confused
# model cannot spend an unbounded amount of the user's budget on one tool call.
DEFAULT_MAX_TOKENS = 1200

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class SamplingUnavailable(Exception):
    """Raised only by callers that cannot degrade; helpers return None instead."""


async def sample_text(
    ctx: Any,
    prompt: str,
    system_prompt: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.0,
) -> str | None:
    """Ask the client's model. Returns None when sampling is unavailable.

    temperature=0 by default: these are translation and summarisation tasks over data
    the server already holds, where the useful answer is the deterministic one.
    """
    if ctx is None:
        return None
    try:
        result = await ctx.sample(
            prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as exc:  # noqa: BLE001 - any failure means the client cannot sample
        logger.info("sampling unavailable (%s)", exc)
        return None

    text = getattr(result, "text", None)
    if text is None:
        # SamplingResult wraps content; fall back to whatever str() yields rather than
        # failing a best-effort feature on a shape change.
        text = str(getattr(result, "content", result))
    return text.strip() or None


async def sample_json(
    ctx: Any, prompt: str, system_prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS
) -> dict[str, Any] | None:
    """Sample and parse a JSON object, tolerating a fenced code block around it.

    Returns None on anything unparseable. A model that returns prose where JSON was
    asked for is a normal occurrence, not an error condition to propagate — and the
    caller has a better message to give than a JSONDecodeError.
    """
    text = await sample_text(ctx, prompt, system_prompt, max_tokens=max_tokens)
    if text is None:
        return None
    return parse_json_object(text)


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Extract a JSON object from model output.

    Split out from sampling so it is testable without a client: the fenced-block and
    surrounding-prose cases are exactly what breaks in practice, and they are worth
    covering directly rather than through a mocked sampling round trip.
    """
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    else:
        # Model prefaced the JSON with a sentence: take from the first brace to the last.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            candidate = candidate[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        logger.info("sampled output was not valid JSON")
        return None
    return parsed if isinstance(parsed, dict) else None


TARGET_SYSTEM_PROMPT = """\
You translate a requested database change into a pgops target-schema JSON object.

Output ONLY a JSON object, no prose and no SQL. The shape is:

  {"tables": {"<table>": {"columns": {"<col>": {"type": "<pg type>",
                                                "nullable": true|false,
                                                "default": "<sql literal>"}},
                          "indexes": [{"name": "...", "columns": ["..."],
                                       "unique": false, "concurrent": true}]}}}

Rules:
- The target describes the DESIRED FINAL STATE, not the change. Include every column the
  table should end up with, including the ones it already has, copied from the current
  schema you are given.
- Only include tables that the request touches.
- Use real PostgreSQL types (text, bigint, timestamptz, jsonb, numeric(12,2), ...).
- Omit a column to leave it alone; the planner never drops anything unless explicitly
  asked to.
- If the request is ambiguous or you cannot express it in this shape, output
  {"error": "<what is unclear>"} instead of guessing.
"""

SUMMARY_SYSTEM_PROMPT = """\
You explain PostgreSQL query plans to an engineer who knows SQL but not planner
internals. Be concrete and brief: what the plan actually does, where the time goes, and
what would change it. Cite node names and numbers from the plan. Do not invent indexes,
columns or statistics that are not in the data you were given. If the plan is already
efficient, say so plainly instead of manufacturing a recommendation.
"""
