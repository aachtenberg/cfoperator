"""Record how an investigation got to its answer (CFOP-324).

An investigation used to keep its conclusion and nothing of the road to it:
when the conclusion was wrong there was no way to see which tool result, or
which learning in the prompt, sent it there. This recorder rides the tool
loop's ``event_callback`` — the same stream the console chat renders — and
writes each step as a row of ``investigation_events``, a table that had the
columns for exactly this and no writer but the cockpit write-back.

Rows, by ``event_type`` and the kind tag in ``action_type``:

- ``tool_call`` — one row per call, written when its result arrives: the
  arguments (``redact_tool_args``), the exact string the model was handed in
  ``reasoning_text``, and ``{status, length, cached}`` in ``tool_output``.
- ``reasoning`` / ``prompt`` — what the model was told: system and user
  message, through the CFOP-272 text rules.
- ``reasoning`` / ``assistant`` — text the model wrote alongside tool calls.
- ``reasoning`` / ``fallback`` — a provider switch, so each turn's model is
  legible.
- ``reasoning`` / ``answer`` — the final answer and the model that wrote it.
- ``reasoning`` / ``truncated`` — the row cap was reached; nothing after it.

Nothing raw reaches a row. Tool results arrive already redacted (the event
carries the copy ``_cached_tool_exec`` handed the model); the prompt and the
model's own text go through the same redactor here, because a learning or an
alert can quote a secret as easily as a tool can.

The recorder never raises into the loop. The trace is evidence about the
investigation, not part of it: a failed write is logged and counted, and the
investigation goes on.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from cfshared.tool_args import redact_tool_args, redact_tool_result

logger = logging.getLogger(__name__)

#: Rows per investigation. A runaway loop is bounded; normal use is far
#: below this (the tool cap is ``chat.max_tool_iterations``, default 10).
MAX_ROWS = 200
#: The prompt holds the learnings and similar-past blocks (CFOP-377); it is
#: the one row allowed to be long.
PROMPT_CHARS = 20_000
#: What a tool row keeps of the string the model was handed. The model itself
#: saw at most ``chat.max_tool_result_chars``; this is a ceiling, not a cut.
RESULT_CHARS = 10_000
TEXT_CHARS = 10_000
ARG_CHARS = 2_000

#: The kind tags written into ``action_type`` on reasoning rows. The route and
#: the console read these, so they are one list, here.
KIND_PROMPT = 'prompt'
KIND_ASSISTANT = 'assistant'
KIND_FALLBACK = 'fallback'
KIND_ANSWER = 'answer'
KIND_TRUNCATED = 'truncated'
REASONING_KINDS = (KIND_PROMPT, KIND_ASSISTANT, KIND_FALLBACK, KIND_ANSWER, KIND_TRUNCATED)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n[… {len(text) - limit} more chars]"


def _redacted_text(text: Any, limit: int) -> str:
    """The text with secret-shaped values replaced, then clipped. Redact
    first: a clip can cut a ``KEY=value`` in half, and half a key no longer
    matches the rule that would have hidden its value."""
    out, _ = redact_tool_result(str(text or ''))
    return _clip(out, limit)


class InvestigationTrace:
    """An ``event_callback`` that writes one investigation's steps.

    ``wants_full_result`` asks ``_dispatch_tool_call`` to put the string the
    model received on the ``tool_result`` event. The chat callback does not
    set it, so the console's live stream stays the 500-char preview it was.
    """

    wants_full_result = True

    def __init__(self, kb, investigation_id: Optional[int],
                 on_row: Optional[Callable[[str, str], None]] = None):
        self.kb = kb
        self.investigation_id = investigation_id
        # (kind, result) per attempted row, result in recorded/failed/truncated.
        self._on_row = on_row
        self._rows = 0
        self._truncated = False
        # tool_call events waiting for their result, oldest first. Calls in
        # one turn execute in order, so the first pending one is this result's.
        self._pending: List[Dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        # ResilientKB hands out a negative placeholder id while offline; no
        # row matches it, so there is nothing to attach a trace to.
        return bool(self.investigation_id) and self.investigation_id > 0

    # -- the event_callback interface ------------------------------------

    def __call__(self, event_type: str, data: Dict[str, Any]) -> None:
        try:
            data = data if isinstance(data, dict) else {}
            if event_type == 'tool_call':
                self._pending.append(data)
            elif event_type == 'tool_result':
                self._tool_result(data)
            elif event_type == 'assistant':
                text = data.get('text')
                if str(text or '').strip():
                    self._reasoning(KIND_ASSISTANT, _redacted_text(text, TEXT_CHARS),
                                    target=data.get('model'))
            elif event_type == 'fallback':
                frm, to = data.get('from') or '?', data.get('to') or '?'
                reason = data.get('reason')
                self._reasoning(KIND_FALLBACK,
                                f"{frm} → {to}" + (f" — {reason}" if reason else ''),
                                target=to)
        except Exception as e:  # pragma: no cover - the guard is the point
            logger.warning("investigation trace: %s event not recorded: %s", event_type, e)

    # -- calls the investigation makes directly --------------------------

    def prompt(self, system_context: str, user_message: str) -> None:
        """What the model was told, before the loop starts."""
        try:
            text = f"--- system ---\n{system_context or ''}\n\n--- user ---\n{user_message or ''}"
            self._reasoning(KIND_PROMPT, _redacted_text(text, PROMPT_CHARS))
        except Exception as e:  # pragma: no cover
            logger.warning("investigation trace: prompt not recorded: %s", e)

    def answer(self, result: Dict[str, Any]) -> None:
        """The loop's answer, and the provider/model that wrote it."""
        try:
            result = result if isinstance(result, dict) else {}
            who = f"{result.get('backend') or '?'}/{result.get('model') or '?'}"
            self._reasoning(KIND_ANSWER, _redacted_text(result.get('response'), TEXT_CHARS),
                            target=who)
        except Exception as e:  # pragma: no cover
            logger.warning("investigation trace: answer not recorded: %s", e)

    # -- rows ------------------------------------------------------------

    def _tool_result(self, data: Dict[str, Any]) -> None:
        call = self._pending.pop(0) if self._pending else {}
        tool = str(data.get('tool') or call.get('tool') or '')
        args = call.get('args')
        if not isinstance(args, (dict, list, str)):
            args = {} if args is None else str(args)
        args = redact_tool_args(args, limit=ARG_CHARS)
        if isinstance(args, str):
            args = {'value': args}
        # `content` is what the model received: already redacted and capped
        # by _cached_tool_exec. `result` (the 500-char preview) is the
        # fallback for a caller that did not ask for the full string.
        content = data.get('content')
        if content is None:
            content = data.get('result')
        if not isinstance(content, str):
            content = json.dumps(content, default=str, ensure_ascii=False)
        status = str(data.get('status') or 'success')
        self._write('tool_call', dict(
            tool_name=tool[:100],
            tool_input=args,
            tool_output={'status': status, 'length': len(content),
                         'cached': bool(data.get('cached'))},
            reasoning_text=_clip(content, RESULT_CHARS),
            duration_ms=data.get('duration_ms') if isinstance(data.get('duration_ms'), int) else None,
            success=(status == 'success'),
        ))

    def _reasoning(self, kind: str, text: str, target: Any = None) -> None:
        self._write(kind, dict(reasoning_text=text, action_type=kind,
                               action_target=str(target)[:255] if target else None),
                    event_type='reasoning')

    def _write(self, kind: str, fields: Dict[str, Any], event_type: Optional[str] = None) -> None:
        if not self.enabled or self._truncated:
            return
        if self._rows >= MAX_ROWS - 1:
            # The last slot says why the trace stops, so a capped trace does
            # not read as an investigation that simply ended there.
            self._truncated = True
            self._insert(KIND_TRUNCATED, 'reasoning', dict(
                reasoning_text=f"Trace truncated at {MAX_ROWS} steps. The investigation went on; "
                               "later steps were not recorded.",
                action_type=KIND_TRUNCATED), result='truncated')
            return
        self._insert(kind, event_type or kind, fields)

    def _insert(self, kind: str, event_type: str, fields: Dict[str, Any],
                result: str = 'recorded') -> None:
        self._rows += 1
        try:
            self.kb.record_investigation_event(
                investigation_id=self.investigation_id, event_type=event_type, **fields)
        except Exception as e:
            logger.warning("investigation trace: #%s %s row not written: %s",
                           self.investigation_id, kind, e)
            result = 'failed'
        if self._on_row:
            try:
                self._on_row(kind, result)
            except Exception:  # pragma: no cover
                pass
