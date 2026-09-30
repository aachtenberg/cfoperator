"""The local-LLM PR reviewer (CFOP-236), without GitHub or Ollama.

What is worth guarding here is not the prompt wording but the behaviour that
costs something when it drifts: which files reach the model, that nothing
forces Ollama to reload, and that a PR ends up with one review comment."""
import io
import json

import pytest

import scripts.local_llm_review as review


def _file(name, patch="@@ -1 +1 @@\n-a\n+b", status="modified"):
    return {"filename": name, "status": status, "additions": 1, "deletions": 1, "patch": patch}


def test_noise_and_unreviewable_files_are_left_out_with_a_reason():
    files = [
        _file("agent.py"),
        _file("package-lock.json"),
        _file("ui/vendor/marked.min.js"),
        _file("k3s/base/plane/sealed-secrets/plane-app-secrets.yml"),
        _file("logo.png", patch=None),
        _file("gone.py", status="removed"),
    ]
    included, skipped = review.select_files(files, budget=10_000)
    assert [f["filename"] for f in included] == ["agent.py"]
    reasons = dict(skipped)
    assert reasons["package-lock.json"] == "generated or vendored"
    assert reasons["ui/vendor/marked.min.js"] == "generated or vendored"
    assert reasons["k3s/base/plane/sealed-secrets/plane-app-secrets.yml"] == "generated or vendored"
    assert reasons["logo.png"] == "no text diff"
    assert reasons["gone.py"] == "deleted"


def test_the_budget_skips_a_file_that_does_not_fit_but_keeps_later_small_ones():
    files = [_file("a.py", "x" * 60), _file("huge.py", "x" * 100), _file("b.py", "x" * 30)]
    # Measured with line numbers added (7 chars per one-line patch here).
    included, skipped = review.select_files(files, budget=67 + 37)
    assert [f["filename"] for f in included] == ["a.py", "b.py"]
    assert skipped == [("huge.py", "over the size budget")]


def test_the_budget_counts_what_the_model_sees_not_the_raw_patch():
    """number_patch adds a column per line; a budget on the raw patch lets the
    prompt run past it."""
    patch = "\n".join(f"+line {i}" for i in range(50))
    assert len(review.number_patch(patch)) > len(patch)
    included, skipped = review.select_files([_file("m.py", patch)], budget=len(patch))
    assert included == [] and skipped == [("m.py", "over the size budget")]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_ollama_request_never_sets_num_ctx_or_keep_alive(monkeypatch):
    """A num_ctx that differs from the loaded runner's makes Ollama reload the
    model (CFOP-168); keep_alive would override how long other callers' model
    stays. The reviewer sends neither."""
    sent = {}

    def fake_urlopen(req, timeout):
        sent.update(json.loads(req.data))
        return _Resp(json.dumps({"message": {"content": '{"findings": []}'}}).encode())

    monkeypatch.setattr(review.urllib.request, "urlopen", fake_urlopen)
    answer, _ = review.ask_ollama("http://ollama:11434/", "qwen3-coder:30b", "sys", "diff",
                                  review.FINDINGS_SCHEMA)
    assert answer == {"findings": []}
    assert sent["model"] == "qwen3-coder:30b" and sent["stream"] is False
    assert sent["format"] == review.FINDINGS_SCHEMA
    assert "num_ctx" not in sent.get("options", {})
    assert "keep_alive" not in sent


PATCH = """@@ -10,3 +10,6 @@ def f():
 a = 1
-b = 2
+# a comment
+b = 3
+c = compute(b)
 d = 4"""


def test_lines_are_numbered_as_the_new_file_and_removed_lines_have_none():
    numbered = review.number_patch(PATCH).splitlines()
    assert numbered[1].split() == ["10", "a", "=", "1"]
    assert numbered[2].strip() == "-b = 2"
    assert numbered[3].split()[0] == "11" and numbered[5].split()[0] == "13"
    assert numbered[6].split()[0] == "14"
    assert review.added_lines(PATCH) == {11: "# a comment", 12: "b = 3", 13: "c = compute(b)"}


def _finding(evidence, path="m.py", line=1):
    return {"path": path, "line": line, "severity": "high", "problem": "p", "evidence": evidence}


def test_grounding_keeps_only_findings_anchored_on_added_code():
    files = [{"filename": "m.py", "patch": PATCH}]
    kept = review.ground([
        _finding("c = compute(b)", line=99),   # real, line miscounted
        _finding("b = 2"),                      # a removed line
        _finding("# a comment"),                # a comment
        _finding("x = nowhere"),                # invented
        _finding("c = compute(b)", path="other.py"),  # wrong file
    ], files)
    assert kept == [_finding("c = compute(b)", line=13)]


def test_short_evidence_must_be_the_whole_line():
    """ "return" would otherwise match every return in the diff and pin the
    finding to whichever is nearest the model's guessed line."""
    patch = "@@ -1,0 +1,3 @@\n+def f():\n+    return compute(x)\n+    return"
    files = [{"filename": "m.py", "patch": patch}]
    assert review.ground([_finding("return", line=2)], files) == [_finding("return", line=3)]
    assert review.ground([_finding("compute", line=1)], files) == []
    assert review.ground([_finding("return compute(x)", line=9)], files) == [
        _finding("return compute(x)", line=2)]


def test_model_text_cannot_mention_link_or_inject_html():
    out = review.defang("ping @aachtenberg, see [here](https://evil.example) ![x](y) <img src=x>")
    assert "@aachtenberg" not in out
    assert "](" not in out and "![" not in out and "https://" not in out
    assert "<img" not in out
    body = review.render([{"path": "m.py", "line": 1, "severity": "high", "problem": "cc @someone"}],
                         {"proposed": 1, "grounded": 1, "kept": 1}, "m", "abcdef0", [1], [], 1.0)
    assert "@someone" not in body


def test_every_claim_is_verified_in_one_call_and_only_confirmed_ones_are_kept(monkeypatch):
    """One verify call per review, not one per claim: the review shares
    cfoperator's model, and every call is time cfoperator's requests queue."""
    files = [{"filename": "m.py", "patch": PATCH}]
    calls = []

    def fake_ask(url, model, system, user, schema):
        calls.append(schema)
        if schema is review.FINDINGS_SCHEMA:
            return {"findings": [_finding("b = 3", line=12), _finding("c = compute(b)", line=13)]}, {}
        return {"verdicts": [{"id": 0, "real": False, "reason": "fine"},
                             {"id": 1, "real": True, "reason": "bug"}]}, {}

    monkeypatch.setattr(review, "ask_ollama", fake_ask)
    kept, counts = review.review("u", "m", "diff", files, log=lambda *_: None)
    assert calls == [review.FINDINGS_SCHEMA, review.VERDICTS_SCHEMA]
    assert [f["line"] for f in kept] == [13]
    assert counts["proposed"] == 2 and counts["grounded"] == 2 and counts["kept"] == 1


@pytest.mark.parametrize("broken", ["propose", "verify"])
def test_an_unreadable_model_answer_fails_the_review_instead_of_passing_it(monkeypatch, broken):
    """A truncated or schema-ignoring answer parses to None. Read as "no
    findings" it would post a clean review, or silently drop every grounded
    claim at the verify step."""
    files = [{"filename": "m.py", "patch": PATCH}]

    def fake_ask(url, model, system, user, schema):
        if schema is review.FINDINGS_SCHEMA:
            return (None, {}) if broken == "propose" else (
                {"findings": [_finding("c = compute(b)", line=13)]}, {})
        return None, {}

    monkeypatch.setattr(review, "ask_ollama", fake_ask)
    with pytest.raises(review.UnreadableAnswer, match=broken):
        review.review("u", "m", "diff", files, log=lambda *_: None)


def test_a_file_name_cannot_break_out_of_its_code_span():
    out = review.code("a`@team [x](y).py")
    assert out.count("`") == 2 and "@team" not in out and "](" not in out
    body = review.render([], {"proposed": 0, "grounded": 0, "kept": 0}, "m", "abcdef0", [],
                         [("evil`@team.lock", "generated or vendored")], 0.0)
    assert "@team" not in body


def test_ollama_url_is_required_not_defaulted(monkeypatch):
    """The script is public; the homelab's address belongs in a repo variable."""
    assert not hasattr(review, "DEFAULT_OLLAMA_URL")
    for k, v in {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r", "PR_NUMBER": "1",
                 "OLLAMA_URL": ""}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit, match="OLLAMA_URL is required"):
        review.main()


def test_no_grounded_claim_means_no_verify_call(monkeypatch):
    calls = []
    monkeypatch.setattr(review, "ask_ollama", lambda *a: calls.append(a[-1]) or ({"findings": []}, {}))
    kept, counts = review.review("u", "m", "diff", [{"filename": "m.py", "patch": PATCH}],
                                 log=lambda *_: None)
    assert kept == [] and calls == [review.FINDINGS_SCHEMA]


def test_a_later_push_edits_the_existing_review_instead_of_adding_one(monkeypatch):
    calls = []

    def fake_github(method, path, token, body=None, accept=None):
        calls.append((method, path))
        return {"html_url": "https://example/c"}

    monkeypatch.setattr(review, "github", fake_github)
    monkeypatch.setattr(review, "paged", lambda path, token: [
        {"id": 1, "body": "an unrelated comment", "user": {"login": "someone"}},
        {"id": 7, "body": review.MARKER + "\nold review", "user": {"login": "github-actions[bot]"}},
    ])
    action, _ = review.upsert_comment("o/r", "5", "t", review.MARKER + "\nnew")
    assert action == "updated"
    assert calls == [("PATCH", "/repos/o/r/issues/comments/7")]


def test_a_pasted_marker_does_not_get_someone_elses_comment_overwritten(monkeypatch):
    calls = []
    monkeypatch.setattr(review, "github",
                        lambda method, path, token, body=None, accept=None:
                        calls.append((method, path)) or {"html_url": "u"})
    monkeypatch.setattr(review, "paged", lambda path, token: [
        {"id": 3, "body": "my notes " + review.MARKER, "user": {"login": "someone"}},
    ])
    action, _ = review.upsert_comment("o/r", "5", "t", "body")
    assert action == "created"
    assert calls == [("POST", "/repos/o/r/issues/5/comments")]


def test_the_first_review_creates_the_comment(monkeypatch):
    calls = []
    monkeypatch.setattr(review, "github",
                        lambda method, path, token, body=None, accept=None:
                        calls.append((method, path)) or {"html_url": "u"})
    monkeypatch.setattr(review, "paged", lambda path, token: [{"id": 1, "body": "hi"}])
    action, _ = review.upsert_comment("o/r", "5", "t", "body")
    assert action == "created"
    assert calls == [("POST", "/repos/o/r/issues/5/comments")]


@pytest.mark.parametrize("value, expected", [("", "gemma4:26b"), ("qwen3-coder:30b", "qwen3-coder:30b")])
def test_an_unset_repo_variable_falls_back_to_the_default(monkeypatch, value, expected):
    """Actions passes an unset vars.X as an empty string, not a missing key."""
    monkeypatch.setenv("REVIEW_MODEL", value)
    assert review.env("REVIEW_MODEL", review.DEFAULT_MODEL) == expected
