"""Conversation context building and budget-aware selection (phase 2.4).

This module is the *single* owner of how the context window is chosen. It is
deliberately **pure Python** — it imports none of Telegram, the OpenAI SDK,
SQLAlchemy, the filesystem, or :mod:`..attachments`. It works on lightweight
candidates (roles, text, attachment *metadata*) that carry **no image bytes**,
so planning never reads a blob from disk.

Two layers live here:

* **The legacy message-count window** — :func:`build_context` pins the system
  prompt to the front and keeps the most recent ``MAX_CONTEXT_MESSAGES``
  messages. It is unchanged in behaviour so existing callers keep working.

* **The phase-2.4 budget planner** — a conservative, *model-agnostic* estimated
  token budget (:func:`estimate_text_cost`, :func:`estimate_parts_cost`) and a
  :func:`plan_context` that, before any attachment bytes are read, selects
  complete conversation turns so that the system prompt, the current user
  request, and as much of the newest history as fits both the message cap and
  the estimated budget are kept. History images are an *optional* part of a
  turn: when a turn's full (image-bearing) form cannot fit, the planner downgrades
  that one turn to its text-only form (the image is dropped, its caption/reply
  kept) instead of skipping a newer turn. This is a transparent *estimate* for
  relative selection and protection — **not** a provider billing token count.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..memory.text import MemoryCandidate, build_memory_reference_text

# Fixed cost attributed to the per-message scaffolding (role, delimiters, …).
MESSAGE_ENVELOPE_UNITS = 4

# One ASCII "word chunk": a run of letters/digits/whitespace/punctuation counts
# as 1 unit per 4 characters, rounded up.
_ASCII_CHUNK = 4


@dataclass(frozen=True)
class ChatMessage:
    """A single chat message in an OpenAI-compatible shape.

    ``content`` is normally a plain ``str`` (every persisted message, and every
    text-only message). A *current* turn that carries an image — or a *history*
    turn whose image attachments were selected for rehydration (phase 2.2/2.4)
    — has a ``content`` that is a ``list`` of typed OpenAI content parts
    (``{"type": "text", ...}`` / ``{"type": "image_url", ...}``), produced by
    :func:`..llm.message_converter.agent_message_to_openai_content`.

    ``tool_calls`` / ``tool_call_id`` are only populated on the assistant and
    tool turns created by the tool loop (:mod:`.tool_loop`); they stay ``None``
    for every chat-only message, so ``to_dict()`` output — and the messages
    persisted to the database — are unchanged there.
    """

    role: str
    content: str | list[dict[str, Any]]
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls is not None:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        return d


def build_context(
    system_prompt: str,
    history: list[ChatMessage],
    max_messages: int = 50,
) -> list[ChatMessage]:
    """Return the messages to send to the model (legacy message-count window).

    Layout: ``[system, ...recent history...]``. The system prompt is always
    pinned to the front and ``max_messages`` is a message *count* taken from the
    most recent end, preserving chronological order. Kept byte-for-byte so the
    text-only / budget-unaware path is unchanged.
    """
    recent = history[-max_messages:] if max_messages > 0 else []
    return [ChatMessage(role="system", content=system_prompt), *recent]


# ---------------------------------------------------------------------------
# Estimated-token accounting (phase 2.4). A conservative, deterministic,
# model-agnostic approximation — NOT a provider billing token count.
# ---------------------------------------------------------------------------
def _is_ascii_token_char(codepoint: int) -> bool:
    """True for ASCII space, letters, digits, and punctuation (0x20–0x7e)."""
    return 0x20 <= codepoint <= 0x7E


def estimate_text_cost(text: str) -> int:
    """A conservative, model-agnostic estimated unit cost of a piece of text.

    * each CJK (Han / Hiragana / Katakana / Hangul) codepoint counts as 1 unit;
    * contiguous ASCII letters/digits/whitespace/punctuation run as ``ceil(n/4)``
      units (1 unit per 4 characters, rounded up);
    * every other codepoint (accented Latin, emoji, …) conservatively counts as
      1 unit each;
    * empty / ``None`` text costs 0.

    The result is intentionally simple and lockable in unit tests; it is used
    for *relative selection and protection*, not for billing-accurate tokens.
    """
    if not text:
        return 0
    total = 0
    run = 0
    for ch in text:
        cp = ord(ch)
        if _is_ascii_token_char(cp):
            run += 1
            continue
        # A non-ASCII codepoint flushes any open ASCII run, then counts itself.
        total += (run + _ASCII_CHUNK - 1) // _ASCII_CHUNK
        run = 0
        total += 1
    total += (run + _ASCII_CHUNK - 1) // _ASCII_CHUNK
    return total


def estimate_parts_cost(parts: list[dict[str, Any]], image_cost: int) -> int:
    """Estimated unit cost of a rendered OpenAI ``content`` list of parts.

    ``{"type": "text"}`` parts use :func:`estimate_text_cost`; each
    ``{"type": "image_url"}`` part costs ``image_cost``. A bare ``str`` is
    delegated to :func:`estimate_text_cost`. Unknown part types cost 0 (they are
    never produced by this codebase).
    """
    if isinstance(parts, str):
        return estimate_text_cost(parts)
    total = 0
    for part in parts:
        ptype = part.get("type")
        if ptype == "text":
            total += estimate_text_cost(str(part.get("text", "")))
        elif ptype == "image_url":
            total += image_cost
    return total


def message_cost(content: str | list[dict[str, Any]], image_cost: int) -> int:
    """Estimated cost of one chat message: its per-message envelope + its parts."""
    return MESSAGE_ENVELOPE_UNITS + estimate_parts_cost(content, image_cost)


# ---------------------------------------------------------------------------
# Turn grouping and budget-aware selection (phase 2.4).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TurnCandidate:
    """A single persisted message as a candidate for context selection.

    Carries only role, text, a stable id (for rehydration), the *metadata* of
    its image attachments (``sha256`` / ``mime_type`` / ``filename`` /
    ``position``) and ``image_count`` — **never** image bytes. ``image_count``
    is the number of attachments that carry image cost; the ``attachments``
    tuple is the same, used to build the rehydrated wire content.
    """

    role: str
    text: str
    message_id: int
    attachments: tuple[tuple[str, str, str | None, int], ...] = ()
    image_count: int = 0


@dataclass(frozen=True)
class _PlannedTurn:
    """A selected turn: its (text-only or full) messages plus whether images kept."""

    messages: tuple[TurnCandidate, ...]
    keep_images: bool


@dataclass(frozen=True)
class ContextPlan:
    """The outcome of :func:`plan_context` — what to send and why, as metadata.

    ``status`` is ``"ok"`` or ``"current_over_budget"`` (system + current user
    already exceed a budget, so the request must not be sent). ``selected`` is
    the history to send, in chronological order (oldest first). Every
    ``PlannedMessage.keep_images`` is the *decision* the caller honours: when
    ``False`` the caller sends that message as plain text and does **not** read
    its attachment bytes.
    """

    status: str
    selected: tuple[PlannedMessage, ...]
    estimated_cost: int
    system_cost: int
    current_cost: int
    budget: int
    cap: int
    # Phase 2.5: the reference memories selected for injection, in ranked
    # order, plus the estimated cost of the single reference message that carries
    # them (a user-role message — see the service for why not a second system
    # message). ``selected_memories`` is empty (and ``memory_cost`` is 0) whenever
    # no memory is injected — e.g. no scope, empty query, or no matches — in
    # which case the rest of the plan is byte-for-byte the phase-2.4 plan.
    selected_memories: tuple[MemoryCandidate, ...] = ()
    memory_cost: int = 0


@dataclass(frozen=True)
class PlannedMessage:
    """One message the planner decided to include, with its image decision."""

    role: str
    text: str
    message_id: int
    keep_images: bool
    attachments: tuple[tuple[str, str, str | None, int], ...] = ()


def group_turns(history: list[TurnCandidate]) -> list[list[TurnCandidate]]:
    """Group persisted history messages into complete conversation turns.

    A turn is a ``user`` message plus every ``assistant`` message that follows
    it before the next ``user`` message. This keeps a normal user/assistant pair
    together so selection never splits it. Anomalous rows degrade safely and
    deterministically: a leading ``assistant`` (no preceding user) starts its own
    turn; consecutive ``assistant`` messages fold into the current turn; a
    trailing unanswered ``user`` is its own turn. Never raises.
    """
    turns: list[list[TurnCandidate]] = []
    current: list[TurnCandidate] | None = None
    for msg in history:
        if msg.role == "user":
            current = [msg]
            turns.append(current)
        else:  # assistant (or any non-user role) attaches to the open turn
            if current is None:
                current = [msg]
                turns.append(current)
            else:
                current.append(msg)
    return turns


def plan_context(
    system_prompt: str,
    current_user: TurnCandidate,
    history: list[TurnCandidate],
    *,
    max_messages: int,
    max_estimated_tokens: int,
    image_cost: int,
    memories: list[MemoryCandidate] | None = None,
    max_memory_estimated_tokens: int = 0,
) -> ContextPlan:
    """Choose the history to send so the request fits *both* budgets.

    Priority (fixed): (1) system prompt always kept; (2) the current user
    request always kept — its images are **never** downgraded; (3) **phase 2.5**
    reference memories, from the already-ranked ``memories``, selected within the
    ``max_memory_estimated_tokens`` sub-budget (a memory that won't fit is skipped,
    lower-scored ones still tried; content is never truncated or reworded);
    (4) history as complete turns, newest first; (5) a turn whose full
    (image-bearing) form does not fit in the remaining budget is downgraded to
    text-only (all its images skipped, no blob read); (6) if the text-only form
    also does not fit, stop — never reach past a newer turn to include an older
    one.

    The memory reference is a *single* reference message (rendered by the
    service as a user-role message), so it does not consume the message cap; its
    estimated cost is committed to the token budget before the history is
    selected, so memory can never push the total over budget. When
    ``memories`` is empty/absent (no scope, no valid query, no matches) the plan
    is byte-for-byte the phase-2.4 plan.

    ``history`` must be oldest-first; the returned ``selected`` is chronological
    (system stays first when the caller assembles the wire messages). ``budget``
    and ``max_messages`` both count system-excluding messages: the cap includes
    the current user message but not the system message.
    """
    system_cost = estimate_text_cost(system_prompt)
    current_cost = message_cost(current_user.text, image_cost) + current_user.image_count * image_cost

    # The two mandatory pieces. The current user request is exactly one message
    # (fits any cap >= 1), but if system + current user's *estimate* already
    # exceeds the token budget, do not call the LLM — the caller turns this into
    # a user-safe context_limit error. No memory is selected in this case.
    if system_cost + current_cost > max_estimated_tokens or max_messages < 1:
        return ContextPlan(
            status="current_over_budget",
            selected=(),
            estimated_cost=system_cost + current_cost,
            system_cost=system_cost,
            current_cost=current_cost,
            budget=max_estimated_tokens,
            cap=max_messages,
        )

    # Phase 2.5: pick reference memories from the already-ranked candidates. The
    # injected reference is one message whose content is the fixed wrapper plus
    # one bullet per selected memory, so we measure the *whole* message cost each
    # time (it carries a single per-message envelope, shared by all bullets).
    # A candidate that would exceed the memory sub-budget — or the total budget —
    # is skipped (never truncated), and lower-scored candidates are still tried.
    mandatory = system_cost + current_cost
    selected_memories: list[MemoryCandidate] = []
    memory_cost = 0
    for cand in (memories or []):
        trial = selected_memories + [cand]
        candidate_cost = message_cost(build_memory_reference_text(trial), image_cost)
        if candidate_cost > max_memory_estimated_tokens:
            continue  # over the memory sub-budget; skip, keep trying lower-scored
        if mandatory + candidate_cost > max_estimated_tokens:
            continue  # would overflow the total budget; skip
        selected_memories = trial
        memory_cost = candidate_cost

    # The memory reference (when present) is committed to the token budget; history
    # is then planned against whatever is left. It is one injected scaffold
    # message, not conversation history, so it does not consume the message cap
    # (``remaining_messages`` only bounds history turns). With no memory selected
    # this is exactly the phase-2.4 arithmetic.
    remaining_messages = max_messages - 1  # minus the current user message
    remaining_tokens = max_estimated_tokens - mandatory - memory_cost

    selected: list[_PlannedTurn] = []
    selected_cost = 0
    for turn in reversed(group_turns(history)):
        turn_messages = len(turn)
        if turn_messages > remaining_messages:
            break  # even the newest unselected turn cannot fit the message cap
        full_cost = sum(message_cost(m.text, image_cost) for m in turn) + image_cost * sum(m.image_count for m in turn)
        if full_cost <= remaining_tokens:
            selected.append(_PlannedTurn(tuple(turn), keep_images=True))
            turn_kept_cost = full_cost
        else:
            text_only_cost = sum(message_cost(m.text, image_cost) for m in turn)
            if text_only_cost > remaining_tokens:
                break  # full and text-only both overflow -> stop
            selected.append(_PlannedTurn(tuple(turn), keep_images=False))
            turn_kept_cost = text_only_cost
        remaining_messages -= turn_messages
        remaining_tokens -= turn_kept_cost
        selected_cost += turn_kept_cost
        if remaining_messages == 0:
            break  # message cap fully consumed

    selected.reverse()  # newest-first -> chronological

    return ContextPlan(
        status="ok",
        selected=tuple(
            PlannedMessage(
                role=m.role,
                text=m.text,
                message_id=m.message_id,
                keep_images=turn.keep_images,
                attachments=m.attachments,
            )
            for turn in selected
            for m in turn.messages
        ),
        estimated_cost=system_cost + current_cost + memory_cost + selected_cost,
        system_cost=system_cost,
        current_cost=current_cost,
        budget=max_estimated_tokens,
        cap=max_messages,
        selected_memories=tuple(selected_memories),
        memory_cost=memory_cost,
    )
