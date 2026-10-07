"""Guards for scripts/scrub_triage_dataset.py (CFOP-277).

The scrub exists so a fine-tune never learns the operator's domain. The
failure that matters is the quiet one: an output that still carries the
domain somewhere the regex did not look, or an output that is "clean"
because the domain was misspelt and nothing matched. Both are pinned here.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from repo_paths import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "scrub_triage_dataset.py"


def _load():
    spec = importlib.util.spec_from_file_location("scrub_triage_dataset", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


scrub = _load()

DOMAIN = "operator-example.net"


def _row(user: str, assistant: str, meta: dict | None = None) -> dict:
    row = {
        "messages": [
            {"role": "system", "content": "You are a triage classifier."},
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ]
    }
    if meta is not None:
        row["meta"] = meta
    return row


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _run(in_dir: Path, out_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--domain", DOMAIN, "--in-dir", str(in_dir), "--out-dir", str(out_dir), *extra],
        capture_output=True, text=True,
    )


def _dataset(tmp_path: Path, trap: bool = False) -> tuple[Path, Path]:
    """Two train rows and one val row. With ``trap``, a third train row carries
    ``not<domain>``, which the boundary rule leaves alone and which therefore
    must make the run fail rather than pass with residual."""
    in_dir, out_dir = tmp_path / "in", tmp_path / "out"
    in_dir.mkdir()
    rows = [
        _row(f"Alert summary: freshet.{DOMAIN} unreachable through the public ingress\nLabels: {{}}",
             json.dumps({"action": "investigate", "reason": f"\"freshet.{DOMAIN} unreachable\": nothing similar listed", "confidence": 0.6}),
             {"investigation_id": 1, "label": "investigate"}),
        _row("Alert summary: pod paperless-ngx restarted on raspberrypi4\nLabels: {}",
             json.dumps({"action": "notify", "reason": "paperless-ngx: repeats an earlier investigation", "confidence": 0.9}),
             {"investigation_id": 2, "label": "notify"}),
        _row(f"Alert summary: cert for {DOMAIN.upper()} expires; also api.{DOMAIN}:443\nLabels: {{}}",
             json.dumps({"action": "notify", "reason": "cert renewal", "confidence": 0.8}),
             {"investigation_id": 3, "label": "notify"}),
    ]
    if trap:
        rows.append(_row(f"Alert summary: host not{DOMAIN} flapping\nLabels: {{}}",
                         json.dumps({"action": "investigate", "reason": "flapping", "confidence": 0.5}),
                         {"investigation_id": 4, "label": "investigate"}))
    _write(in_dir / "triage_train.jsonl", rows)
    _write(in_dir / "triage_val.jsonl", [
        _row("Alert summary: node raspberrypi3 down\nLabels: {}",
             json.dumps({"action": "escalate", "reason": "node down", "confidence": 0.9}),
             {"investigation_id": 5, "label": "escalate"}),
    ])
    return in_dir, out_dir


def test_domain_is_gone_and_leftmost_labels_survive(tmp_path: Path):
    in_dir, out_dir = _dataset(tmp_path)
    proc = _run(in_dir, out_dir)
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "triage_train.jsonl").read_text(encoding="utf-8")
    assert DOMAIN not in text.lower()
    assert "freshet.homelab.example unreachable" in text
    assert "api.homelab.example:443" in text
    assert "cert for homelab.example expires" in text  # bare domain, upper-cased in the input
    rows = [json.loads(l) for l in text.splitlines()]
    assert rows[0]["meta"]["scrub"] == {"replacements": 2, "placeholder": "homelab.example"}
    assert rows[2]["meta"]["scrub"]["replacements"] == 2
    assert "scrub" not in rows[1].get("meta", {})
    assert "3 rows, 2 touched, 4 replacements" in proc.stdout


def test_adjacent_name_is_a_residual_and_fails_closed(tmp_path: Path):
    # "not<domain>" is a different name, so the boundary rule leaves it alone;
    # the output would then still contain the domain, and the only acceptable
    # outcome is a refusal with the output removed.
    in_dir, out_dir = _dataset(tmp_path, trap=True)
    proc = _run(in_dir, out_dir)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "residual" in proc.stderr
    assert not (out_dir / "triage_train.jsonl").exists()


def test_untouched_rows_are_byte_identical(tmp_path: Path):
    in_dir, out_dir = _dataset(tmp_path)
    rows = [json.loads(l) for l in (in_dir / "triage_train.jsonl").read_text(encoding="utf-8").splitlines()]
    _write(in_dir / "triage_train.jsonl", rows[:2])
    proc = _run(in_dir, out_dir)
    assert proc.returncode == 0, proc.stderr
    src_lines = (in_dir / "triage_train.jsonl").read_text(encoding="utf-8").splitlines(True)
    dst_lines = (out_dir / "triage_train.jsonl").read_text(encoding="utf-8").splitlines(True)
    assert len(src_lines) == len(dst_lines) == 2
    assert src_lines[1] == dst_lines[1]
    assert src_lines[0] != dst_lines[0]
    # The untouched val file is byte-identical as a whole.
    assert (in_dir / "triage_val.jsonl").read_bytes() == (out_dir / "triage_val.jsonl").read_bytes()


def test_nothing_to_scrub_is_an_error_not_a_pass(tmp_path: Path):
    in_dir, out_dir = _dataset(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--domain", "never-present.example.org", "--in-dir", str(in_dir), "--out-dir", str(out_dir)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 3
    assert "nothing matched" in proc.stderr
    assert not (out_dir / "triage_train.jsonl").exists()


def test_domain_never_appears_in_the_output_metadata(tmp_path: Path):
    in_dir, out_dir = _dataset(tmp_path)
    rows = [json.loads(l) for l in (in_dir / "triage_train.jsonl").read_text(encoding="utf-8").splitlines()]
    _write(in_dir / "triage_train.jsonl", rows[:2])
    proc = _run(in_dir, out_dir)
    assert proc.returncode == 0, proc.stderr
    for name in ("triage_train.jsonl", "triage_val.jsonl"):
        assert DOMAIN not in (out_dir / name).read_text(encoding="utf-8").lower()
    # The sha256 lines are printed for the manifest; stdout must not leak the domain either.
    assert DOMAIN not in proc.stdout.lower()


def test_refuses_in_place(tmp_path: Path):
    in_dir, _ = _dataset(tmp_path)
    proc = _run(in_dir, in_dir)
    assert proc.returncode == 2
    assert "must differ" in proc.stderr


def test_parent_of_a_longer_name_is_rewritten_too():
    # "<domain>.evil" is rewritten to "<placeholder>.evil": the right boundary
    # allows "." on purpose, and the residual check is what guarantees nothing
    # is left. Pinned so the comment and the regex cannot drift apart again.
    rx = scrub._pattern(DOMAIN)
    out, n = scrub.scrub_value(f"see {DOMAIN}.evil and {DOMAIN}.", rx, "homelab.example")
    assert n == 2
    assert out == "see homelab.example.evil and homelab.example."


def test_marker_lands_even_when_meta_is_not_a_dict(tmp_path: Path):
    in_dir, out_dir = tmp_path / "in", tmp_path / "out"
    in_dir.mkdir()
    rows = [_row(f"freshet.{DOMAIN} down", json.dumps({"action": "investigate", "reason": "x", "confidence": 0.5}))]
    rows[0]["meta"] = None
    _write(in_dir / "triage_train.jsonl", rows)
    _write(in_dir / "triage_val.jsonl", [_row("clean", json.dumps({"action": "notify", "reason": "y", "confidence": 0.5}))])
    proc = _run(in_dir, out_dir)
    assert proc.returncode == 0, proc.stderr
    out = json.loads((out_dir / "triage_train.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert out["meta"]["scrub"]["replacements"] == 1


def test_unparseable_input_leaves_no_partial_output(tmp_path: Path):
    in_dir, out_dir = _dataset(tmp_path)
    with (in_dir / "triage_val.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    proc = _run(in_dir, out_dir)
    assert proc.returncode == 2
    assert not (out_dir / "triage_val.jsonl").exists()
    assert not (out_dir / "triage_train.jsonl").exists(), "earlier outputs of the same run must be removed too"


def test_scrub_value_handles_keys_lists_and_case():
    rx = scrub._pattern(DOMAIN)
    obj = {f"host.{DOMAIN}": [f"A.{DOMAIN.upper()}", {"x": f"{DOMAIN}"}], "plain": "untouched"}
    out, n = scrub.scrub_value(obj, rx, "homelab.example")
    assert n == 3
    assert out == {"host.homelab.example": ["A.homelab.example", {"x": "homelab.example"}], "plain": "untouched"}
