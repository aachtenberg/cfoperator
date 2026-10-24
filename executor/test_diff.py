"""Tests for the executor's vendored unified-diff utilities."""

import pytest

from diff import apply_unified_diff, extract_diff_block, is_secret_path, parse_unified_diff

_DIFF = """--- a/k3s/base/apps/ollama.yaml
+++ b/k3s/base/apps/ollama.yaml
@@ -1,3 +1,3 @@
 a
-b
+B
 c
"""


def test_parse_and_apply_roundtrip():
    parsed = parse_unified_diff(_DIFF)
    assert parsed is not None
    path, hunks = parsed
    assert path == "k3s/base/apps/ollama.yaml"
    assert apply_unified_diff("a\nb\nc\n", hunks) == "a\nB\nc\n"


def test_parse_rejects_multifile():
    two = _DIFF + "--- a/other.yaml\n+++ b/other.yaml\n@@ -1 +1 @@\n-x\n+y\n"
    assert parse_unified_diff(two) is None


def test_apply_declines_on_drift():
    _, hunks = parse_unified_diff(_DIFF)
    # file no longer contains the expected context block -> decline, not guess
    assert apply_unified_diff("totally\ndifferent\nfile\n", hunks) is None


def test_is_secret_path():
    assert is_secret_path("k3s/base/secrets/sealed.yaml")
    assert is_secret_path("apps/.env")
    assert not is_secret_path("k3s/base/apps/ollama.yaml")


def test_extract_diff_block():
    report = f"some reasoning\n\n```diff\n{_DIFF}```\ntrailing"
    block = extract_diff_block(report)
    assert block is not None and block.startswith("--- a/")
    assert extract_diff_block("no diff here") is None


def test_parse_and_apply_numberless_hunk_header():
    """The #42 shape (CFOP-51): a correct diff with a bare '@@' header must
    parse and apply via the unique-context fallback — the position numbers
    are a hint the applier never needed."""
    text = "\n".join(f"pad-{i}" for i in range(20)) + "\n  expr: old\n  for: 30m\n"
    d = ("--- a/rules.yml\n"
         "+++ b/rules.yml\n"
         "@@\n"
         "-  expr: old\n"
         "+  expr: new\n"
         "   for: 30m\n")
    parsed = parse_unified_diff(d)
    assert parsed is not None
    path, hunks = parsed
    assert path == "rules.yml" and hunks[0][0] is None  # no claimed position
    patched = apply_unified_diff(text, hunks)
    assert patched is not None and "expr: new" in patched and "expr: old" not in patched


def test_numberless_hunk_ambiguous_context_still_declines():
    # Tolerant parsing must not weaken the applier: two identical candidate
    # blocks -> ambiguous -> decline, exactly as with numbered headers.
    text = "a\nb\na\nb\n"
    d = "--- a/f\n+++ b/f\n@@\n-a\n+A\n b\n"
    path, hunks = parse_unified_diff(d)
    assert apply_unified_diff(text, hunks) is None


# Remediation #326 verbatim (CFOP-472): the model fused its first "-" line onto
# the hunk header. The other three old lines are unique in the file, so a
# parser that reads any "@@" line as a header applies this hunk and leaves the
# command start in the file twice.
_FUSED_HEADER_DIFF = """--- a/ansible/templates/docker-backup.sh.j2
+++ b/ansible/templates/docker-backup.sh.j2
@@ -        k3s etcd-snapshot save \\
-            --name "backup-${TIMESTAMP}" \\
-            --dir "${ETCD_SNAPSHOT_DIR}" 2>>"$LOG_FILE" || \\
-            log "    Warning: Failed to create etcd snapshot"
+        k3s etcd-snapshot save \\
+            --name "backup-${TIMESTAMP}" \\
+            --data-dir "${K3S_DATA_DIR}" \\
+            --dir "${ETCD_SNAPSHOT_DIR}" 2>>"$LOG_FILE" || \\
+            error_exit "Failed to create etcd snapshot"
"""


def test_unclosed_header_with_fused_body_line_declines():
    assert parse_unified_diff(_FUSED_HEADER_DIFF) is None


@pytest.mark.parametrize("header", [
    "@@",
    "@@ @@",
    "@@ ... @@",
    "@@ -2,2 +2,2 @@",
    "@@ -2,2 +2,2 @@ def section():",
])
def test_closed_and_bare_headers_still_parse(header):
    # The CFOP-51 shapes, plus git's section text after a closed header: the
    # CFOP-472 check must decline only a header that never closes.
    d = f"--- a/f\n+++ b/f\n{header}\n a\n-b\n+B\n"
    parsed = parse_unified_diff(d)
    assert parsed is not None
    assert apply_unified_diff("x\na\nb\n", parsed[1]) == "x\na\nB\n"
