#!/usr/bin/env python3
"""Review a pull request with an LLM and post one comment.

Two transports, one pipeline. The original is a local model on the homelab's
Ollama (CFOP-236), run from the self-hosted runner that can reach it, which
homelab-infra still does with this file fetched from cfoperator@main; the
second is any OpenAI-compatible chat API, run from a hosted runner (CFOP-400,
Reflection AI's Beam), which is what cfoperator runs. cfoperator's own Ollama
job was dropped (CFOP-402): on real diffs gemma4 without thinking proposed
nothing on 46 of 48 PRs, found a planted shell injection but not the
off-by-one beside it, and Beam found both. Both reviews are advisory.

It reads the PR from the GitHub API and never checks out, imports or runs the
PR's code: the diff is only text handed to a model. The workflows run under
pull_request_target, which is safe only for that reason: the script that
runs is always the base branch's, so a PR cannot swap the reviewer out while
an API key is in scope.

A small local model asked for a review in one shot mostly produces noise:
"should be verified", complaints about comments, and defects in lines the PR
deletes. So a review is three steps, and a finding has to survive all three:

  1. propose  -- the model lists findings as JSON, each quoting the added line
                 it is about as evidence;
  2. ground   -- mechanical: the evidence must be an added line of that file,
                 and not a comment. This drops misread and invented findings
                 and fixes the cited line number;
  3. verify   -- the model judges each surviving claim on its own and must say
                 it is a real defect. The diff is the same prefix as step 1,
                 so Ollama reuses its cache and this costs little.

The comment is created once and edited on every later push, so a PR carries
one review per reviewer for its current head, not a stack of them. Each
reviewer is named, and the name is what tells its comment apart.

Environment:
    GITHUB_TOKEN            token with pull-requests read and issues write
    GITHUB_REPOSITORY       owner/name (set by Actions)
    PR_NUMBER               the pull request to review
    REVIEW_BACKEND          ollama (default) or openai
    REVIEW_NAME             how the comment is titled; default "Local LLM"
                            for ollama and "LLM" for openai
    REVIEW_MAX_DIFF_CHARS   default 60000
    REVIEW_SCOPE            defects (default) or thorough; see SCOPES
    REVIEW_DRY_RUN          1 = print the comment instead of posting it
  ollama:
    OLLAMA_URL              required, e.g. http://<ollama-host>:11434
    REVIEW_MODEL            default gemma4:26b
  openai:
    REVIEW_API_URL          required, the base URL ending in /v1
    REVIEW_API_KEY          required, sent as a bearer token
    REVIEW_MODEL            required
    REVIEW_REASONING_EFFORT optional, passed through as reasoning_effort
  fallback (optional, openai-compatible; used when the primary cannot answer):
    REVIEW_FALLBACK_API_URL, REVIEW_FALLBACK_API_KEY, REVIEW_FALLBACK_MODEL
    REVIEW_FALLBACK_REASONING_EFFORT
"""
import collections
import copy
import fnmatch
import functools
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GITHUB_API = "https://api.github.com"

#: A reviewer, as its comment shows it: the title, where the model ran, and
#: the hidden marker that finds the comment again on the next push.
Reviewer = collections.namedtuple("Reviewer", "name served marker")


def reviewer(name, served):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "llm"
    return Reviewer(name, served, f"<!-- {slug}-review -->")


LOCAL = reviewer("Local LLM", "on the homelab's Ollama")
#: The marker the Ollama review has always used; derived, so it cannot drift.
MARKER = LOCAL.marker

# The model cfoperator already keeps resident on the 24 GB card. A dedicated
# coder model was tried (qwen3-coder:30b, CFOP-236): through this pipeline it
# found exactly what gemma4 found, but at ~19 GB it cannot share the card, and
# gemma4 is busy often enough (20-110 chats per 15 min) that each review waited
# minutes for a swap while cfoperator's calls queued behind it.
DEFAULT_MODEL = "gemma4:26b"
# The model runs with a 32k-token window. The budget counts numbered patch
# text only; the prompt, per-file headers and up to 4000 chars of description
# come on top. ~60k chars of diff is ~17k tokens, so the whole prompt stays
# near 20k, which leaves the answer room but not a wide margin.
DEFAULT_MAX_DIFF_CHARS = 60000
# Per model call. A review makes at most two per backend, and with a fallback
# configured a run can make four; all must fit the job's timeout-minutes.
MODEL_TIMEOUT_SECONDS = 480

#: Files whose diff costs context and carries nothing a reviewer can judge.
SKIP_PATTERNS = (
    "*.lock", "*-lock.json", "*-lock.yaml", "package-lock.json", "go.sum",
    "*.min.js", "*.min.css", "*.map", "*.svg",
    "ui/vendor/*", "vendor/*", "*/vendor/*",
    "*sealed-secrets/*", "*sealedsecret*.y*ml", "*sealed-secret*.y*ml",
)

#: A line whose code part is only a comment. Findings anchored on one are
#: dropped: they are nearly always about wording, not behaviour.
#: "#" and a bare "*" count only when followed by space, "!" or the end:
#: "#include", "#[derive]" and "*ptr = x" are code, not comments.
COMMENT_LINE = re.compile(r"^\s*(#(\s|!|$)|//|/\*|\*(\s|/|$)|<!--|--\s)")

READING_THE_DIFF = """\
How to read the diff: each line starts with its line number in the NEW file,
then a marker. "+" is added, " " is unchanged context, and "-" lines (shown
without a number) were REMOVED and no longer exist. Judge the code as it is
after the change. A problem that only exists in a "-" line is not a problem:
the pull request already removed it."""

PROPOSE_PROMPT = f"""\
You are a senior engineer reviewing a pull request.

{READING_THE_DIFF}

Report only defects that would matter if this merged: bugs, security problems,
broken or dangerous configuration, data loss, races, wrong error handling, and
changes that do not do what the description says they do.

Do not report:
- style, naming, formatting or missing tests;
- comments or documentation;
- things the pull request does not do (missing automation, missing guidance,
  possible future improvements);
- anything that only says something "should be verified" or "confirmed";
- anything you are not confident is a real defect.

An empty list is the right answer for most pull requests.

For each finding give: path; line (the NEW line number); severity (high,
medium or low); problem (what goes wrong, and when); and evidence: one added
("+") line from the diff, copied exactly, without its number and marker, that
the defect is on."""

VERIFY_PROMPT = f"""\
You check claimed defects in a pull request, each one on its own.

{READING_THE_DIFF}

The claims came from a first pass that is often wrong: it misreads the diff,
flags harmless code, asks for things to be "verified", or describes something
the pull request does not do. For each claim, answer real=true only if the
claimed behaviour would actually go wrong once this is merged.

Code that is wrong or unsafe as written is real even when you cannot see its
callers: a condition that is always true, SQL or a shell command built from
unescaped input, a secret in the code, an exception that is swallowed. Answer
real=false when the claim only holds under assumptions about code you cannot
see, or when you are unsure. Give the reason in one sentence."""

THOROUGH_PROPOSE_PROMPT = f"""\
You are a senior engineer reviewing a pull request. Read the whole diff
before deciding anything.

{READING_THE_DIFF}

Report what a careful maintainer would raise before merging:

- defects (severity high or medium): bugs, security problems, broken or
  dangerous configuration, data loss, races, wrong error handling, and
  changes that do not do what the description says they do;
- concrete risks (severity low): an error path that is swallowed or
  misreported, a resource that is not released, input trusted further than
  it should be, a cost that grows with data (a query per row, a read repeated
  per call), behaviour that changes for existing callers, and a guarantee the
  description makes that the code does not quite keep.

Say what goes wrong and when, concretely enough that the author can act
without asking.

Do not report:
- style, naming, formatting or missing tests;
- comments or documentation;
- things the pull request does not do (missing automation, missing guidance,
  possible future improvements), unless the description claims them;
- anything that only says something "should be verified" or "confirmed";
- a concern the description already states and argues.

An empty list is fine when the diff is clean; do not pad it.

For each finding give: path; line (the NEW line number); severity (high,
medium or low); problem (what goes wrong, and when); and evidence: one added
("+") line from the diff, copied exactly, without its number and marker, that
the finding is on."""

THOROUGH_VERIFY_PROMPT = f"""\
You check claims about a pull request, each one on its own.

{READING_THE_DIFF}

The claims came from a first pass that is sometimes wrong: it misreads the
diff, flags harmless code, asks for things to be "verified", restates what
the description already argues, or describes something the pull request
does not do. For each claim, answer real=true if a careful maintainer would
want it raised before merging: the behaviour goes wrong, or the risk is
concrete and the fix is clear. Answer real=false for a claim that is wrong
about the code, only holds under assumptions about code you cannot see, is
a matter of taste, or is already addressed in the description or the diff.
Give the reason in one sentence."""

#: The prompt pairs a review can run with. ``defects`` is for a small local
#: model, which proposes noise under anything looser and verifies weakly;
#: ``thorough`` is for a strong model, which under ``defects`` obeys "only
#: what you are confident is a defect" literally and never raises the
#: medium-grade concerns a human reviewer would (CFOP-403).
SCOPES = {
    "defects": (PROPOSE_PROMPT, VERIFY_PROMPT, "Claimed defects:"),
    "thorough": (THOROUGH_PROPOSE_PROMPT, THOROUGH_VERIFY_PROMPT, "Claims:"),
}

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "problem": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["path", "line", "severity", "problem", "evidence"],
            },
        },
    },
    "required": ["findings"],
}

VERDICTS_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "real": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "real", "reason"],
            },
        },
    },
    "required": ["verdicts"],
}


def env(name, default=""):
    """Unset and empty both mean default: an unset repo variable reaches the
    step as an empty string, not as a missing key."""
    return os.environ.get(name) or default


def github(method, path, token, body=None, accept="application/vnd.github+json"):
    req = urllib.request.Request(
        GITHUB_API + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cfoperator-local-llm-review",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read() or b"null")


def paged(path, token):
    out, page = [], 1
    while True:
        sep = "&" if "?" in path else "?"
        batch = github("GET", f"{path}{sep}per_page=100&page={page}", token)
        out.extend(batch)
        if len(batch) < 100:
            return out
        page += 1


def skip_reason(f):
    name = f["filename"]
    if any(fnmatch.fnmatch(name, p) for p in SKIP_PATTERNS):
        return "generated or vendored"
    if f.get("status") == "removed":
        return "deleted"
    if not f.get("patch"):
        # GitHub omits the patch for binaries and for very large diffs.
        return "no text diff"
    return None


def select_files(files, budget):
    """Split the PR's files into those that go to the model and those that do
    not, with a reason for each one left out. Files go in PR order until the
    budget runs out; a file that alone exceeds what is left is skipped, and
    smaller ones after it still get a chance."""
    included, skipped, used = [], [], 0
    for f in files:
        reason = skip_reason(f)
        if reason is None:
            # Measured as the model will see it, line numbers included.
            size = len(number_patch(f["patch"]))
            if used + size > budget:
                reason = "over the size budget"
            else:
                included.append(f)
                used += size
                continue
        skipped.append((f["filename"], reason))
    return included, skipped


def added_lines(patch):
    """{new line number: text} for every added line of a unified-diff patch."""
    out, new_no = {}, 0
    for line in patch.splitlines():
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            new_no = int(m.group(1)) if m else 0
        elif line.startswith("-") or line.startswith("\\"):
            continue
        else:
            if line.startswith("+"):
                out[new_no] = line[1:]
            new_no += 1
    return out


def number_patch(patch):
    """Prefix each line of a unified-diff patch with its line number in the
    new file, so the model can cite path:line without counting hunks, and so
    removed lines are visibly not part of the result."""
    out, new_no = [], 0
    for line in patch.splitlines():
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            new_no = int(m.group(1)) if m else 0
            out.append(line)
        elif line.startswith("-"):
            out.append(f"{'':>6} {line}")
        elif line.startswith("\\"):
            out.append(line)  # "\ No newline at end of file"
        else:
            out.append(f"{new_no:>6} {line}")
            new_no += 1
    return "\n".join(out)


def build_diff(repo, pr, files):
    parts = [
        f"Repository: {repo}",
        f"Pull request #{pr['number']}: {pr['title']}",
        "",
        "Description:",
        (pr.get("body") or "(none)")[:4000],
        "",
        "Diff:",
    ]
    for f in files:
        parts.append(f"### {f['filename']} ({f['status']}, +{f['additions']} -{f['deletions']})")
        parts.append("```diff")
        parts.append(number_patch(f["patch"]))
        parts.append("```")
    return "\n".join(parts)


def _norm(text):
    return " ".join(text.split())


#: Evidence shorter than this must equal a line, not merely occur in one:
#: "return" or "}" would otherwise match half the diff.
MIN_SUBSTRING_EVIDENCE = 12


def _evidence_matches(evidence, line):
    line = _norm(line)
    if not line:
        return False
    if evidence == line:
        return True
    # A quote of part of a line, or a multi-line quote containing the line.
    return ((len(evidence) >= MIN_SUBSTRING_EVIDENCE and evidence in line) or
            (len(line) >= MIN_SUBSTRING_EVIDENCE and line in evidence))


def ground(findings, files):
    """Keep a finding only if its evidence is an added line of the file it
    names, and that line is code, not a comment. The line number is taken
    from where the evidence actually is: models miscount."""
    added = {f["filename"]: added_lines(f["patch"]) for f in files}
    kept = []
    for finding in findings:
        lines = added.get(finding.get("path", ""))
        evidence = _norm(finding.get("evidence", ""))
        if not lines or not evidence:
            continue
        matches = [n for n, text in lines.items() if _evidence_matches(evidence, text)]
        if not matches:
            continue
        try:
            cited = int(finding.get("line") or 0)
        except (TypeError, ValueError):
            cited = 0
        line_no = min(matches, key=lambda n: abs(n - cited))
        if COMMENT_LINE.match(lines[line_no]):
            continue
        kept.append({**finding, "line": line_no})
    return kept


def ask_ollama(url, model, system, user, schema):
    # No num_ctx and no keep_alive, on purpose. Ollama reloads the model when a
    # request's num_ctx differs from the loaded runner's, and keep_alive would
    # override how long other callers' models stay (CFOP-168). Temperature is a
    # sampling option and does not trigger a reload.
    body = {
        "model": model,
        "stream": False,
        # gemma4 thinks by default: 21k thinking tokens and 4 minutes on a
        # 6-file PR, holding cfoperator's model the whole time, for the same
        # answer. False is ignored by models that cannot think.
        "think": False,
        "format": schema,
        "options": {"temperature": 0.1},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        url.rstrip("/") + "/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    # Every call must fit the job's timeout with the others, or the job is
    # killed mid-call and leaves no comment; a timeout here fails it visibly.
    with urllib.request.urlopen(req, timeout=MODEL_TIMEOUT_SECONDS) as resp:
        data = json.loads(resp.read())
    content = (data.get("message") or {}).get("content", "")
    return parse_answer(content), {
        "prompt_tokens": data.get("prompt_eval_count", "?"),
        "generated_tokens": data.get("eval_count", "?"),
        "note": f"load {data.get('load_duration', 0) / 1e9:.1f}s",
    }


def strict(schema):
    """The schema with additionalProperties: false on every object, which
    OpenAI-style strict structured output requires and Ollama does not."""
    out = copy.deepcopy(schema)

    def close(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
            for v in node.values():
                close(v)
        elif isinstance(node, list):
            for v in node:
                close(v)
    close(out)
    return out


def ask_openai(url, key, model, effort, system, user, schema):
    """One chat completion against an OpenAI-compatible endpoint, answered as
    JSON matching ``schema``. ``effort`` is passed as reasoning_effort when
    set; a model that reasons by default (Beam) otherwise picks its own."""
    body = {
        "model": model,
        "temperature": 0.1,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "answer", "strict": True, "schema": strict(schema)}},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if effort:
        body["reasoning_effort"] = effort
    req = urllib.request.Request(
        url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "cfoperator-llm-review"},
    )
    # Unredirected: urllib copies ordinary headers onto a redirect's target,
    # which would hand the key to whatever host the endpoint pointed at.
    req.add_unredirected_header("Authorization", f"Bearer {key}")
    data = _open_with_retry(req)
    choices = data.get("choices") or [{}]
    content = (choices[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", "?")
    return parse_answer(content), {
        "prompt_tokens": usage.get("prompt_tokens", "?"),
        "generated_tokens": usage.get("completion_tokens", "?"),
        "note": f"reasoning {reasoning}",
    }


#: 429 from the API. Two kinds, told apart by the headers (CFOP-403, seen on
#: Reflection): a per-minute limit (30 requests) that two PRs pushed together
#: can trip, worth a short wait; and a daily output-token allowance
#: (``retry-after`` of many hours, ``x-should-retry: false``) that no wait
#: inside a job can outlive. Bounded, so a real outage still fails inside the
#: job's timeout and the PR gets its "No review" comment.
RETRY_DELAYS_SECONDS = (5, 15, 45)
MAX_RETRY_AFTER_SECONDS = 60
#: The most one model call can spend paused on 429s. Four calls' worth (two
#: per backend, primary then fallback), plus four calls' MODEL_TIMEOUT_SECONDS,
#: must fit llm-review.yml's timeout-minutes, or a run whose primary hung to
#: its timeout and whose fallback then did too is killed before it can post.
RETRY_PAUSE_BUDGET_SECONDS = len(RETRY_DELAYS_SECONDS) * MAX_RETRY_AFTER_SECONDS


class RateLimited(RuntimeError):
    """A 429 the job cannot wait out. Carries the API's own message, which
    on a daily allowance says when it renews."""


def _open_with_retry(req, sleep=time.sleep):
    """The JSON body, retrying a short 429 a few times with a growing pause.
    Anything else propagates as before."""
    for delay in (*RETRY_DELAYS_SECONDS, None):
        try:
            with urllib.request.urlopen(req, timeout=MODEL_TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            headers = e.headers or {}
            try:
                retry_after = float(headers.get("Retry-After", 0))
            except (TypeError, ValueError):
                retry_after = 0
            if (headers.get("x-should-retry", "").lower() == "false"
                    or retry_after > MAX_RETRY_AFTER_SECONDS or delay is None):
                message = _api_error_message(e)
                e.close()
                raise RateLimited(message) from e
            e.close()  # the 429's socket, not left to the garbage collector
            sleep(max(delay, retry_after))


def _api_error_message(e):
    """The API's error message out of a failed response, or the status line.
    One line: it ends up inside an italic span in the PR comment."""
    try:
        body = json.loads(e.read())
        message = str((body.get("error") or {}).get("message") or body)
    except Exception:
        return f"HTTP {e.code}"
    return " ".join(message.split())[:300]


def parse_answer(content):
    """The JSON object in a model's answer, or None. A fenced block is
    unwrapped: some endpoints wrap structured output in ```json anyway."""
    text = content.strip()
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class UnreadableAnswer(RuntimeError):
    """The model's answer was not the JSON asked for (truncated, or the schema
    ignored). Reported as a failed review, never as a clean one."""

    def __init__(self, step):
        super().__init__(f"the model's {step} answer was not valid JSON; no review")


def review(ask, diff, files, log=print, prompts=SCOPES["defects"]):
    """Propose, ground, verify. ``ask(system, user, schema)`` is the model,
    whichever transport; ``prompts`` is a (propose, verify, claims heading)
    entry from SCOPES. Returns (kept findings, counts)."""
    propose_prompt, verify_prompt, heading = prompts
    proposed, usage = ask(propose_prompt, diff, FINDINGS_SCHEMA)
    log(f"propose: {usage.get('prompt_tokens', '?')} prompt tokens, "
        f"{usage.get('generated_tokens', '?')} generated, {usage.get('note', '')}")
    if proposed is None:
        raise UnreadableAnswer("propose")
    proposed = proposed.get("findings") or []
    grounded = ground(proposed, files)
    verified = []
    if grounded:
        # One call for every claim, not one each: the review shares
        # cfoperator's model, and every call is time its requests queue.
        claims = "\n".join(
            f"[{i}] {f['path']}:{f['line']} ({f['severity']}) {f['problem']}\n"
            f"    evidence: {f['evidence']}" for i, f in enumerate(grounded))
        answer, _ = ask(verify_prompt, f"{diff}\n\n{heading}\n{claims}", VERDICTS_SCHEMA)
        if answer is None:
            # Not "no findings": every grounded claim would vanish unseen.
            raise UnreadableAnswer("verify")
        real = {}
        for v in answer.get("verdicts") or []:
            log(f"verify [{v.get('id')}]: real={v.get('real')} {v.get('reason', '')}")
            try:
                real[int(v.get("id"))] = v.get("real") is True
            except (TypeError, ValueError):
                continue
        verified = [f for i, f in enumerate(grounded) if real.get(i)]
    return verified, {"proposed": len(proposed), "grounded": len(grounded), "kept": len(verified),
                      "prompt_tokens": usage.get("prompt_tokens", "?")}


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

ZWSP = "​"


def defang(text):
    """Model text goes into a public comment, and the model has read a PR
    that anyone can write. Keep it inert: no @mentions (they notify), no
    links or images, no raw HTML. A zero-width space breaks each without
    changing how the text reads."""
    return (str(text).replace("<", "&lt;").replace("@", "@" + ZWSP)
            .replace("](", "]" + ZWSP + "(").replace("![", "!" + ZWSP + "[")
            .replace("://", ":" + ZWSP + "//").replace("www.", "www" + ZWSP + ".")
            # "#123" cross-links another issue or PR and adds an event there.
            .replace("#", "#" + ZWSP))


def render(findings, counts, model, head_sha, included, skipped, seconds, who=LOCAL):
    lines = [who.marker, f"### {who.name} review", ""]
    if findings:
        for f in sorted(findings, key=lambda f: SEVERITY_ORDER.get(f["severity"], 3)):
            # Clamped: the schema asks for one of these, but it is model output.
            severity = f["severity"] if f["severity"] in SEVERITY_ORDER else "low"
            lines.append(f"- **[{severity}] {code(f['path'])}:{f['line']}** {defang(f['problem'])}")
    elif included:
        lines.append("No significant issues found.")
    else:
        lines.append("_Nothing reviewable in this diff._")
    lines.append("")
    if skipped:
        lines.append("<details><summary>Not reviewed ({})</summary>\n".format(len(skipped)))
        lines.extend(f"- {code(name)}: {reason}" for name, reason in skipped)
        lines.append("\n</details>\n")
    tally = (f"{counts['proposed']} proposed, {counts['grounded']} grounded, {counts['kept']} kept"
             if counts else "not run")
    lines.append(
        f"<sub>{model} {who.served} · {head_sha[:7]} · "
        f"{len(included)} file(s) · findings {tally} · {seconds:.0f}s. "
        "Advisory only; the other reviews are separate.</sub>"
    )
    return "\n".join(lines)


#: Who posts the review under Actions' GITHUB_TOKEN.
DEFAULT_BOT_LOGIN = "github-actions[bot]"


#: What ends a review attempt and, with a fallback configured, hands the diff
#: to the other backend. OSError covers URLError, timeouts and a reset
#: connection; ValueError a truncated body that is not JSON; HTTPException a
#: bad status line.
FAILURES = (UnreadableAnswer, RateLimited, OSError, ValueError, http.client.HTTPException)


class BothFailed(RuntimeError):
    """The primary and the fallback both failed; the comment names both."""

    def __init__(self, primary_model, primary, fallback_model, fallback):
        super().__init__(f"{primary_model}: {primary}; fallback {fallback_model}: {fallback}")
        self.primary_model, self.primary = primary_model, primary
        self.fallback_model, self.fallback = fallback_model, fallback


def describe_failure(error, short=False):
    """What to say on the PR: the class, plus the status for an HTTP error
    and the API's own words for a rate limit (it says when the allowance
    renews). ``short`` drops the API's words, for the footer of a review the
    fallback served. Model text is not involved; the API's message is
    defanged."""
    if isinstance(error, BothFailed):
        return (f"{code(error.primary_model)} {describe_failure(error.primary, short)}; "
                f"fallback {code(error.fallback_model)} {describe_failure(error.fallback, short)}")
    if isinstance(error, RateLimited):
        if short:
            return "rate limited by the API"
        return f"rate limited by the API ({defang(str(error)).replace('_', chr(92) + '_')})"
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code} from the API"
    return type(error).__name__


def render_failure(error, model, head_sha, who=LOCAL):
    return "\n".join([
        who.marker, f"### {who.name} review", "",
        f"_No review for {head_sha[:7]}: {describe_failure(error)}. "
        "See the workflow run's log. Any earlier review on this PR is for an older commit._",
        "", f"<sub>{model} {who.served} · {head_sha[:7]}.</sub>",
    ])


def code(name):
    """A path as an inline code span. PR authors choose file names, so a
    backtick in one could close the span and let the rest (an @mention, a
    link) render; drop backticks, and defang what is left."""
    return "`" + defang(str(name).replace("`", "'")) + "`"


def upsert_comment(repo, number, token, body, bot_login=DEFAULT_BOT_LOGIN, marker=MARKER):
    """Edit this reviewer's earlier comment if there is one, else create it.
    Only a comment the bot itself wrote counts: anyone can paste the marker
    into their own comment, and that must not get it overwritten. Two
    reviewers post under the same bot login; the marker tells them apart."""
    for c in paged(f"/repos/{repo}/issues/{number}/comments", token):
        if marker in (c.get("body") or "") and (c.get("user") or {}).get("login") == bot_login:
            edited = github("PATCH", f"/repos/{repo}/issues/comments/{c['id']}", token, {"body": body})
            return "updated", edited["html_url"]
    c = github("POST", f"/repos/{repo}/issues/{number}/comments", token, {"body": body})
    return "created", c["html_url"]


def main():
    token = env("GITHUB_TOKEN")
    repo = env("GITHUB_REPOSITORY")
    number = env("PR_NUMBER")
    if not (token and repo and number):
        sys.exit("GITHUB_TOKEN, GITHUB_REPOSITORY and PR_NUMBER are required")
    ask, model, who = backend_from_env()
    fallback = fallback_from_env()
    scope = env("REVIEW_SCOPE", "defects")
    if scope not in SCOPES:
        sys.exit(f"REVIEW_SCOPE={scope!r}; want one of {', '.join(SCOPES)}")
    budget = int(env("REVIEW_MAX_DIFF_CHARS", str(DEFAULT_MAX_DIFF_CHARS)))

    pr = github("GET", f"/repos/{repo}/pulls/{number}", token)
    files = paged(f"/repos/{repo}/pulls/{number}/files", token)
    included, skipped = select_files(files, budget)
    print(f"{repo}#{number} @ {pr['head']['sha'][:7]}: {len(included)} file(s) to review, "
          # Not the URL: this log is public on cfoperator, and vars.* are not masked.
          f"{len(skipped)} skipped, model {model}")

    started = time.monotonic()
    findings, counts, failure = [], None, None
    if included:
        diff = build_diff(repo, pr, included)
        try:
            findings, counts = review(ask, diff, included, prompts=SCOPES[scope])
        except FAILURES as e:
            failure = e
            if fallback is not None:
                # The same diff to the other backend, and the comment says so:
                # a review that Haiku wrote must not read as Beam's (CFOP-404).
                fb_ask, fb_model, fb_served = fallback
                print(f"{model} could not review ({describe_failure(e)}); trying {fb_model}")
                try:
                    findings, counts = review(fb_ask, diff, included, prompts=SCOPES[scope])
                except FAILURES as e2:
                    failure = BothFailed(model, e, fb_model, e2)
                else:
                    who = who._replace(served=f"{fb_served}, standing in for {model} "
                                              f"({describe_failure(e, short=True)})")
                    model, failure = fb_model, None
    seconds = time.monotonic() - started

    if failure is None:
        body = render(findings, counts, model, pr["head"]["sha"], included, skipped, seconds, who)
    else:
        # Say so on the PR too: otherwise the previous push's review stays up
        # looking current, and nothing shows this head went unreviewed.
        body = render_failure(failure, model, pr["head"]["sha"], who)
    if env("REVIEW_DRY_RUN") == "1":
        print(body)
    else:
        action, url = upsert_comment(repo, number, token, body, marker=who.marker)
        print(f"{action} {url}")
    if failure is not None:
        sys.exit(f"review failed: {failure}")


def backend_from_env():
    """(ask, model, reviewer) for REVIEW_BACKEND. Secrets stay out of the
    reviewer: its text goes into a public comment."""
    backend = env("REVIEW_BACKEND", "ollama")
    if backend == "ollama":
        ollama = env("OLLAMA_URL")
        if not ollama:
            sys.exit("OLLAMA_URL is required (the workflows set it from the LOCAL_REVIEW_OLLAMA_URL "
                     "repo variable)")
        model = env("REVIEW_MODEL", DEFAULT_MODEL)
        return (functools.partial(ask_ollama, ollama, model), model,
                reviewer(env("REVIEW_NAME", LOCAL.name), LOCAL.served))
    if backend == "openai":
        url, key, model = env("REVIEW_API_URL"), env("REVIEW_API_KEY"), env("REVIEW_MODEL")
        if not (url and key and model):
            sys.exit("REVIEW_API_URL, REVIEW_API_KEY and REVIEW_MODEL are required for the "
                     "openai backend")
        host = urllib.parse.urlsplit(url).hostname or "an OpenAI-compatible API"
        return (functools.partial(ask_openai, url, key, model, env("REVIEW_REASONING_EFFORT")),
                model, reviewer(env("REVIEW_NAME", "LLM"), f"at {host}"))
    sys.exit(f"REVIEW_BACKEND={backend!r}; want ollama or openai")


def fallback_from_env():
    """(ask, model, served) for an optional second OpenAI-compatible backend,
    or None. All three of URL, key and model, or nothing: a half-set fallback
    would fail exactly when it is needed."""
    url, key, model = env("REVIEW_FALLBACK_API_URL"), env("REVIEW_FALLBACK_API_KEY"), env("REVIEW_FALLBACK_MODEL")
    if not (url or key or model):
        return None
    if not (url and key and model):
        sys.exit("REVIEW_FALLBACK_API_URL, REVIEW_FALLBACK_API_KEY and REVIEW_FALLBACK_MODEL go together")
    host = urllib.parse.urlsplit(url).hostname or "an OpenAI-compatible API"
    return (functools.partial(ask_openai, url, key, model, env("REVIEW_FALLBACK_REASONING_EFFORT")),
            model, f"at {host}")


if __name__ == "__main__":
    main()
