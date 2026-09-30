#!/usr/bin/env python3
"""Review a pull request with a local model on Ollama and post one comment.

Runs on the homelab's self-hosted runner, which can reach Ollama on the LAN
(CFOP-236). CodeRabbit and the Claude review both need a cloud model; this is
the one that does not. It is advisory and runs beside the Claude review, not
instead of it.

It reads the PR from the GitHub API and never checks out, imports or runs the
PR's code: the diff is only text handed to a model. cfoperator runs it under
pull_request_target, which is safe only for that reason (see the workflow).

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
one local review for its current head, not a stack of them.

Environment:
    GITHUB_TOKEN          token with pull-requests read and issues write
    GITHUB_REPOSITORY     owner/name (set by Actions)
    PR_NUMBER             the pull request to review
    OLLAMA_URL            default http://192.168.0.150:11434
    REVIEW_MODEL          default gemma4:26b
    REVIEW_MAX_DIFF_CHARS default 60000
    REVIEW_DRY_RUN        1 = print the comment instead of posting it
"""
import fnmatch
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

MARKER = "<!-- local-llm-review -->"
GITHUB_API = "https://api.github.com"

DEFAULT_OLLAMA_URL = "http://192.168.0.150:11434"
# The model cfoperator already keeps resident on the 24 GB card. A dedicated
# coder model was tried (qwen3-coder:30b, CFOP-236): through this pipeline it
# found exactly what gemma4 found, but at ~19 GB it cannot share the card, and
# gemma4 is busy often enough (20-110 chats per 15 min) that each review waited
# minutes for a swap while cfoperator's calls queued behind it.
DEFAULT_MODEL = "gemma4:26b"
# The model runs with a 32k-token window. ~60k chars of diff is ~17k tokens,
# which leaves room for the prompt, the description and the answer.
DEFAULT_MAX_DIFF_CHARS = 60000

#: Files whose diff costs context and carries nothing a reviewer can judge.
SKIP_PATTERNS = (
    "*.lock", "*-lock.json", "*-lock.yaml", "package-lock.json", "go.sum",
    "*.min.js", "*.min.css", "*.map", "*.svg",
    "ui/vendor/*", "vendor/*", "*/vendor/*",
    "*sealed-secrets/*", "*sealedsecret*.y*ml", "*sealed-secret*.y*ml",
)

#: A line whose code part is only a comment. Findings anchored on one are
#: dropped: they are nearly always about wording, not behaviour.
COMMENT_LINE = re.compile(r"^\s*(#|//|/\*|\*|<!--|--\s)")

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
        line_no = min(matches, key=lambda n: abs(n - int(finding.get("line") or 0)))
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
    with urllib.request.urlopen(req, timeout=900) as resp:
        data = json.loads(resp.read())
    content = (data.get("message") or {}).get("content", "")
    try:
        return json.loads(content), data
    except json.JSONDecodeError:
        return None, data


def review(url, model, diff, files, log=print):
    """Propose, ground, verify. Returns (kept findings, counts)."""
    proposed, raw = ask_ollama(url, model, PROPOSE_PROMPT, diff, FINDINGS_SCHEMA)
    log(f"propose: {raw.get('prompt_eval_count', '?')} prompt tokens, "
        f"{raw.get('eval_count', '?')} generated, load {raw.get('load_duration', 0) / 1e9:.1f}s")
    proposed = (proposed or {}).get("findings") or []
    grounded = ground(proposed, files)
    verified = []
    if grounded:
        # One call for every claim, not one each: the review shares
        # cfoperator's model, and every call is time its requests queue.
        claims = "\n".join(
            f"[{i}] {f['path']}:{f['line']} ({f['severity']}) {f['problem']}\n"
            f"    evidence: {f['evidence']}" for i, f in enumerate(grounded))
        answer, _ = ask_ollama(url, model, VERIFY_PROMPT,
                               f"{diff}\n\nClaimed defects:\n{claims}", VERDICTS_SCHEMA)
        real = {}
        for v in (answer or {}).get("verdicts") or []:
            log(f"verify [{v.get('id')}]: real={v.get('real')} {v.get('reason', '')}")
            real[v.get("id")] = v.get("real") is True
        verified = [f for i, f in enumerate(grounded) if real.get(i)]
    return verified, {"proposed": len(proposed), "grounded": len(grounded), "kept": len(verified),
                      "prompt_tokens": raw.get("prompt_eval_count", "?")}


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

ZWSP = "​"


def defang(text):
    """Model text goes into a public comment, and the model has read a PR
    that anyone can write. Keep it inert: no @mentions (they notify), no
    links or images, no raw HTML. A zero-width space breaks each without
    changing how the text reads."""
    return (str(text).replace("<", "&lt;").replace("@", "@" + ZWSP)
            .replace("](", "]" + ZWSP + "(").replace("![", "!" + ZWSP + "[")
            .replace("://", ":" + ZWSP + "//"))


def render(findings, counts, model, head_sha, included, skipped, seconds):
    lines = [MARKER, "### Local LLM review", ""]
    if findings:
        for f in sorted(findings, key=lambda f: SEVERITY_ORDER.get(f["severity"], 3)):
            lines.append(f"- **[{f['severity']}] `{f['path']}:{f['line']}`** {defang(f['problem'])}")
    elif included:
        lines.append("No significant issues found.")
    else:
        lines.append("_Nothing reviewable in this diff._")
    lines.append("")
    if skipped:
        lines.append("<details><summary>Not reviewed ({})</summary>\n".format(len(skipped)))
        lines.extend(f"- `{name}`: {reason}" for name, reason in skipped)
        lines.append("\n</details>\n")
    tally = (f"{counts['proposed']} proposed, {counts['grounded']} grounded, {counts['kept']} kept"
             if counts else "not run")
    lines.append(
        f"<sub>{model} on the homelab's Ollama · {head_sha[:7]} · "
        f"{len(included)} file(s) · findings {tally} · {seconds:.0f}s. "
        "Advisory only; the Claude review is separate.</sub>"
    )
    return "\n".join(lines)


#: Who posts the review under Actions' GITHUB_TOKEN.
DEFAULT_BOT_LOGIN = "github-actions[bot]"


def upsert_comment(repo, number, token, body, bot_login=DEFAULT_BOT_LOGIN):
    """Edit this workflow's earlier comment if there is one, else create it.
    Only a comment the bot itself wrote counts: anyone can paste the marker
    into their own comment, and that must not get it overwritten."""
    for c in paged(f"/repos/{repo}/issues/{number}/comments", token):
        if MARKER in (c.get("body") or "") and (c.get("user") or {}).get("login") == bot_login:
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
    ollama = env("OLLAMA_URL", DEFAULT_OLLAMA_URL)
    model = env("REVIEW_MODEL", DEFAULT_MODEL)
    budget = int(env("REVIEW_MAX_DIFF_CHARS", str(DEFAULT_MAX_DIFF_CHARS)))

    pr = github("GET", f"/repos/{repo}/pulls/{number}", token)
    files = paged(f"/repos/{repo}/pulls/{number}/files", token)
    included, skipped = select_files(files, budget)
    print(f"{repo}#{number} @ {pr['head']['sha'][:7]}: {len(included)} file(s) to review, "
          f"{len(skipped)} skipped, model {model} at {ollama}")

    started = time.monotonic()
    findings, counts = [], None
    if included:
        findings, counts = review(ollama, model, build_diff(repo, pr, included), included)
    seconds = time.monotonic() - started

    body = render(findings, counts, model, pr["head"]["sha"], included, skipped, seconds)
    if env("REVIEW_DRY_RUN") == "1":
        print(body)
        return
    action, url = upsert_comment(repo, number, token, body)
    print(f"{action} {url}")


if __name__ == "__main__":
    main()
