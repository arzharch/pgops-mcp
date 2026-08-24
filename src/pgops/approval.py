"""Human approval via MCP elicitation, with the token protocol as fallback.

The confirmation-token design (guardrails.py) has one structural weakness: the approval
round-trips **through the agent**. The server refuses, hands back a token and a reason,
and trusts the model to relay that reason faithfully to a human and only return with the
token once a human actually said yes. Nothing enforces the middle step. A model that is
confused, over-eager, or adversarially prompted can simply call again with the token it
was just given.

Elicitation closes that gap. It is a server→client request that asks the **user**
directly, outside the model's turn: the client renders a prompt, the human answers, and
the answer comes back to the server. The model cannot fabricate it because the model is
not in that path.

So the policy here is:

- If the client supports elicitation, ask the human directly.
- If it does not — elicitation is optional in MCP and plenty of clients lack it — fall
  back to the token protocol, which still works and is still audited.

Crucially the fallback is *not* "allow it". Losing elicitation degrades approval from
"the human was asked" to "the agent asserts the human was asked", never to "no approval
required". Both paths are recorded in the audit log with which one was used, so an
incident review can tell the two apart — that distinction is exactly what someone
reconstructing a bad day needs to know.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger("pgops.approval")


class ApprovalMethod(StrEnum):
    ELICITATION = "elicitation"
    TOKEN = "token"
    UNAVAILABLE = "unavailable"


@dataclass(slots=True)
class ApprovalResult:
    approved: bool
    method: ApprovalMethod
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"approved": self.approved, "method": self.method.value, "detail": self.detail}


async def request_approval(ctx: Any, action: str, reason: str) -> ApprovalResult:
    """Ask the human to approve a dangerous action.

    `ctx` is the FastMCP Context, or None when a tool is called outside a request (in
    tests, or through the direct function API).

    Returns UNAVAILABLE — never an approval — when elicitation cannot be used, so the
    caller falls back to the token flow rather than proceeding.
    """
    if ctx is None:
        return ApprovalResult(False, ApprovalMethod.UNAVAILABLE, "no request context")

    message = f"{action}\n\n{reason}"
    try:
        # An explicit response_type is required rather than the bare-confirmation form:
        # `response_type=None` produces an empty schema that is ambiguous under the MCP
        # spec, and some clients (VS Code among them) render it as an empty,
        # non-functional form — the user is asked to approve something and given no way
        # to answer. A two-option choice renders as a real prompt everywhere.
        result = await ctx.elicit(
            message,
            response_type=["approve", "cancel"],
            response_title="Approve this action?",
        )
    except Exception as exc:  # noqa: BLE001 - any failure means "client can't elicit"
        # Client does not support elicitation, or the transport refused. This is an
        # expected condition, not an error: degrade to the token protocol.
        logger.info("elicitation unavailable (%s); falling back to confirmation token", exc)
        return ApprovalResult(False, ApprovalMethod.UNAVAILABLE, str(exc))

    action_taken = getattr(result, "action", None)
    if action_taken == "accept":
        # The client accepted the *prompt*; the chosen option decides the answer, so
        # "accept" plus a "cancel" selection is still a refusal. Treating the envelope
        # as the answer would approve everything the user actively declined.
        choice = getattr(result, "data", None)
        if isinstance(choice, str) and choice.lower() != "approve":
            return ApprovalResult(False, ApprovalMethod.ELICITATION, f"user chose {choice!r}")
        return ApprovalResult(True, ApprovalMethod.ELICITATION, "user approved")
    if action_taken == "decline":
        return ApprovalResult(False, ApprovalMethod.ELICITATION, "user declined")
    return ApprovalResult(False, ApprovalMethod.ELICITATION, f"user {action_taken or 'cancelled'}")


async def report_progress(ctx: Any, current: float, total: float, message: str) -> None:
    """Best-effort progress notification.

    A migration that rebuilds an index on a large table can run for minutes with no
    output at all, which is indistinguishable from a hang. Progress notifications are
    optional in MCP, so failure to send one must never affect the operation itself —
    hence the blanket catch.
    """
    if ctx is None:
        return
    try:
        await ctx.report_progress(current, total, message)
    except Exception:
        logger.debug("progress notification failed", exc_info=True)


async def client_log(ctx: Any, level: str, message: str) -> None:
    """Send a log line to the client, in addition to the server's stderr log.

    Server-side logs go to stderr where only the operator sees them. For a long or
    surprising operation the *user* often wants to know what is happening, and that is
    what MCP logging notifications are for.
    """
    if ctx is None:
        return
    try:
        await ctx.log(message, level=level)
    except Exception:
        logger.debug("client log failed", exc_info=True)
