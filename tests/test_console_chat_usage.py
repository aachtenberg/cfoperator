"""What a turn cost is stored with its reply (CFOP-718).

The live view draws its stats line from the ``done`` event. A reloaded session
has only the stored message, so the same numbers ride on it, and the session
totals in the header survive a reload. What is copied out of the event into
the JSONB row is checked here: whole numbers in a sane range, nothing else.
"""

from repo_paths import REPO_ROOT  # noqa: F401  (puts the repository root on the path)

from web_server import chat_turn_stats, persist_chat_event


class _Kb:
    def __init__(self):
        self.rows = []

    def append_chat_message(self, session_id, role, content, backend="", model="", extra=None):
        self.rows.append({"session_id": session_id, "role": role, "content": content,
                          "backend": backend, "model": model, "extra": extra})
        return True


def _done(**data):
    return {"event": "done", "data": {"response": "All good.", "backend": "ollama",
                                      "model": "gemma4:26b", **data}}


def test_the_reply_is_stored_with_its_usage():
    kb = _Kb()
    persist_chat_event(kb, 7, _done(tool_calls=3, duration_s=9.63, usage={
        "input_tokens": 1840, "output_tokens": 412, "last_prompt_tokens": 12300, "context_window": 32768}))
    (row,) = kb.rows
    assert row["role"] == "assistant" and row["content"] == "All good."
    assert row["extra"] == {
        "usage": {"input_tokens": 1840, "output_tokens": 412, "last_prompt_tokens": 12300,
                  "context_window": 32768},
        "duration_s": 9.6, "tool_calls": 3}


def test_a_turn_that_reported_nothing_stores_nothing_extra():
    """Not zeros: a reloaded line reading "0↑ 0↓" would be a claim."""
    kb = _Kb()
    persist_chat_event(kb, 7, _done())
    assert kb.rows[0]["extra"] is None


def test_only_whole_numbers_in_range_are_copied():
    stats = chat_turn_stats({
        "tool_calls": "3", "duration_s": -1,
        "usage": {"input_tokens": 12.5, "output_tokens": True, "last_prompt_tokens": -4,
                  "context_window": 10 ** 12, "anything_else": "text", "response": "secret"}})
    assert stats == {}
    assert chat_turn_stats({"usage": "not a dict", "duration_s": "9"}) == {}
    assert chat_turn_stats(None) == {} and chat_turn_stats({}) == {}


def test_unknown_keys_in_usage_do_not_reach_the_row():
    """A number under a name this does not know is still not copied: the row
    holds the four counts and nothing a later payload happens to add."""
    stats = chat_turn_stats({"usage": {"input_tokens": 5, "prompt": "what was asked",
                                       "cache_read_tokens": 900, "anything": 7}})
    assert stats == {"usage": {"input_tokens": 5}}


def test_zero_tool_calls_is_a_fact_and_is_kept():
    assert chat_turn_stats({"tool_calls": 0, "duration_s": 0}) == {"tool_calls": 0, "duration_s": 0.0}


def test_a_stopped_turn_and_an_error_carry_no_stats():
    kb = _Kb()
    persist_chat_event(kb, 7, {"event": "done", "data": {"response": "", "stopped": True,
                                                         "usage": {"input_tokens": 5}}})
    persist_chat_event(kb, 7, {"event": "error", "data": {"error": "boom"}})
    assert [r["role"] for r in kb.rows] == ["assistant", "error"]
    assert kb.rows[0]["extra"] == {"stopped": True} and kb.rows[1]["extra"] is None
