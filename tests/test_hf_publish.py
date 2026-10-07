"""Guards for publishing the triage fine-tune to Hugging Face (CFOP-274).

Two things can go wrong silently and both end in a public repo:

1. The dataset scan says "clean" because a pattern has a hole, not because
   the data is clean. So the scanner is exercised with planted secrets of
   every shape it claims to catch, including the one shape that slipped the
   first version (``POSTGRES_PASSWORD=`` has a word character before the
   keyword, so a ``\\b`` anchored pattern misses it).

2. The published Modelfile drifts from the gated one in ``benchmarks/``. The
   publish script derives it from that file by rewriting ``FROM`` and nothing
   else, and this suite runs the script in ``--stage-only`` mode to confirm
   the rest is byte-identical.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from repo_paths import REPO_ROOT

HF_DIR = REPO_ROOT / "hf"
GATED_MODELFILE = REPO_ROOT / "benchmarks" / "Modelfile.cfop-triage-v5"


def _load_scanner():
    spec = importlib.util.spec_from_file_location("scan_dataset", HF_DIR / "scan_dataset.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


scan = _load_scanner()


def _row(text: str) -> str:
    return json.dumps({"messages": [{"role": "user", "content": text}]}) + "\n"


# One plant per pattern the scanner claims to catch. The expected name is the
# pattern that must fire; other patterns firing as well is fine.
PLANTS = [
    ("hf-token", "token hf_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"),
    ("github-token", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"),
    ("sk-token", "sk-ant-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"),
    ("gsk-token", "gsk_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"),
    ("slack-token", "xoxb-1234567890-abcdefghij"),
    ("aws-access-key", "AKIAABCDEFGHIJKLMNOP"),
    ("google-api-key", "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456"),  # AIza + exactly 35
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijklmnop"),
    ("password-assignment", "POSTGRES_PASSWORD=hunter2hunter2"),
    ("password-assignment", "password: s3cretvalue"),
    ("password-assignment", "api_key=\"abcdef123456\""),
    ("password-assignment", "token=abcdefghijklmnop"),
    ("password-assignment", "GITHUB_TOKEN: ghx_notaprefixedtoken"),
    ("bearer-header", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123"),
    ("token-header", "Authorization: token abcdefghijklmnopqrstuvwxyz0123"),
    ("basic-auth-header", "Authorization: Basic dXNlcjpwYXNzd29yZDEyMw=="),
    ("private-key-block", "-----BEGIN OPENSSH PRIVATE KEY-----"),
    ("url-with-userinfo", "https://user:pass@db.internal/x"),
    ("email", "paged ops@example.com"),
    ("public-ipv4", "peer 8.8.8.8 unreachable"),
]


@pytest.mark.parametrize("expected, plant", PLANTS, ids=[p[1][:24] for p in PLANTS])
def test_scanner_catches_each_planted_secret(tmp_path: Path, expected: str, plant: str):
    f = tmp_path / "plant.jsonl"
    f.write_text(_row("alert on raspberrypi3 " + plant), encoding="utf-8")
    findings = scan.scan_file(f)
    assert any(name == expected for _, name, _ in findings), findings


def test_scanner_passes_a_real_shaped_clean_row(tmp_path: Path):
    f = tmp_path / "clean.jsonl"
    f.write_text(
        _row(
            "Alert severity: warning\n"
            "Alert summary: pod paperless-ngx-7d9c4b8f5-nq2wm in namespace apps restarted on raspberrypi4 (192.168.0.116)\n"
            "Labels: {\"namespace\": \"apps\"}\n"
            "Similar past investigations:\n- [resolved  ] monitoring_cycle: OOM on node (similarity: 0.94)\n\nClassify."
        ),
        encoding="utf-8",
    )
    assert scan.scan_file(f) == []


def test_scanner_ignores_private_and_loopback_addresses():
    for addr in ("10.0.0.1", "192.168.0.150", "172.16.5.5", "127.0.0.1", "0.0.0.0"):
        assert not scan._is_public_ipv4(addr), addr
    assert scan._is_public_ipv4("8.8.8.8")


def test_scanner_cli_exit_codes(tmp_path: Path):
    clean = tmp_path / "clean.jsonl"
    clean.write_text(_row("nothing to see"), encoding="utf-8")
    dirty = tmp_path / "dirty.jsonl"
    dirty.write_text(_row("hf_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"), encoding="utf-8")
    assert subprocess.run([sys.executable, str(HF_DIR / "scan_dataset.py"), str(clean)]).returncode == 0
    assert subprocess.run([sys.executable, str(HF_DIR / "scan_dataset.py"), str(dirty)]).returncode == 1
    assert subprocess.run([sys.executable, str(HF_DIR / "scan_dataset.py"), str(tmp_path / "missing.jsonl")]).returncode == 2


def test_scanner_redacts_what_it_reports(tmp_path: Path):
    secret = "hf_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"
    f = tmp_path / "dirty.jsonl"
    f.write_text(_row(secret), encoding="utf-8")
    out = subprocess.run(
        [sys.executable, str(HF_DIR / "scan_dataset.py"), str(f)], capture_output=True, text=True
    ).stdout
    assert secret not in out
    assert "hf-token" in out


def _run_stage(tmp_path: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    for name in ("triage_train.jsonl", "triage_val.jsonl"):
        (dataset / name).write_text(_row("clean row"), encoding="utf-8")
    stage = tmp_path / "stage"
    env = dict(
        os.environ,
        HF_REPO="someone/cfop-triage-ministral3-14b-v5",
        DATASET_DIR=str(dataset),
        STAGE_DIR=str(stage),
        SRC_DIR=str(tmp_path / "nonexistent-src"),  # must not be touched in --stage-only
    )
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(HF_DIR / "publish.sh"), "--stage-only"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )


def test_staged_modelfile_is_the_gated_one_with_only_from_rewritten(tmp_path: Path):
    proc = _run_stage(tmp_path)
    assert proc.returncode == 0, proc.stderr
    staged = (tmp_path / "stage" / "Modelfile").read_text(encoding="utf-8").splitlines()
    gated = GATED_MODELFILE.read_text(encoding="utf-8").splitlines()
    assert len(staged) == len(gated)
    diffs = [(g, s) for g, s in zip(gated, staged) if g != s]
    assert len(diffs) == 1, diffs
    assert diffs[0][0].startswith("FROM /mnt/nas-backup/")
    assert diffs[0][1] == "FROM ./ministral-3-14b-instruct-2512.Q4_K_M.gguf"


def test_staged_card_has_repo_id_filled_in_and_no_placeholder(tmp_path: Path):
    proc = _run_stage(tmp_path)
    assert proc.returncode == 0, proc.stderr
    card = (tmp_path / "stage" / "README.md").read_text(encoding="utf-8")
    assert "REPO_ID" not in card
    assert "hf.co/someone/cfop-triage-ministral3-14b-v5:Q4_K_M" in card
    # Frontmatter the Hub needs to file it correctly.
    assert card.startswith("---\nlicense: apache-2.0\n")
    assert "base_model: mistralai/Ministral-3-14B-Instruct-2512" in card


def test_stage_refuses_dirty_dataset(tmp_path: Path):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "triage_train.jsonl").write_text(_row("POSTGRES_PASSWORD=hunter2hunter2"), encoding="utf-8")
    (dataset / "triage_val.jsonl").write_text(_row("clean"), encoding="utf-8")
    env = dict(os.environ, HF_REPO="someone/x", DATASET_DIR=str(dataset), STAGE_DIR=str(tmp_path / "stage"))
    proc = subprocess.run(
        ["bash", str(HF_DIR / "publish.sh"), "--stage-only"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    assert proc.returncode != 0
    assert not (tmp_path / "stage" / "Modelfile").exists()


def test_stage_without_adapter_dir_says_so(tmp_path: Path):
    proc = _run_stage(tmp_path)
    assert "adapter will not be published" in proc.stderr
    assert not (tmp_path / "stage" / "adapter").exists()


def test_stage_with_adapter_dir_copies_both_files(tmp_path: Path):
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "adapter_model.safetensors").write_bytes(b"\0" * 16)
    (adapter / "adapter_config.json").write_text("{}", encoding="utf-8")
    proc = _run_stage(tmp_path, {"ADAPTER_DIR": str(adapter)})
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "stage" / "adapter" / "adapter_model.safetensors").exists()
    assert (tmp_path / "stage" / "adapter" / "adapter_config.json").exists()


def test_stage_refuses_a_non_empty_stage_dir(tmp_path: Path):
    # Everything in the stage is uploaded, so a leftover from an earlier run
    # (say, an adapter that is no longer meant to ship) would go public.
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "adapter").mkdir()
    (stage / "adapter" / "leftover.bin").write_bytes(b"\0")
    proc = _run_stage(tmp_path)
    assert proc.returncode != 0
    assert "not empty" in proc.stderr
    assert not (stage / "Modelfile").exists()


@pytest.mark.parametrize("bad", ["nouser", "user/repo/extra", "user/re po", "user/re#po", "user/re&po"])
def test_publish_rejects_malformed_repo_id(tmp_path: Path, bad: str):
    proc = _run_stage(tmp_path, {"HF_REPO": bad})
    assert proc.returncode == 2, proc.stderr
    assert "not <user>/<repo>" in proc.stderr


# --- the artifact gate: --dry-run runs everything but the upload -------------

Q4 = "ministral-3-14b-instruct-2512.Q4_K_M.gguf"
Q8 = "ministral-3-14b-instruct-2512.Q8_0.gguf"


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_src(tmp_path: Path) -> tuple[Path, Path]:
    """Two small stand-in GGUFs and a manifest that matches them."""
    src = tmp_path / "src"
    src.mkdir()
    (src / Q4).write_bytes(b"q4 " * 100)
    (src / Q8).write_bytes(b"q8 " * 100)
    manifest = tmp_path / "v5.sha256"
    manifest.write_text(f"{_sha256(src / Q4)}  {Q4}\n{_sha256(src / Q8)}  {Q8}\n", encoding="utf-8")
    return src, manifest


def _run_dry(tmp_path: Path, src: Path, manifest: Path, extra_env: dict[str, str] | None = None, stage: str = "stage"):
    dataset = tmp_path / "dataset"
    dataset.mkdir(exist_ok=True)
    for name in ("triage_train.jsonl", "triage_val.jsonl"):
        (dataset / name).write_text(_row("clean row"), encoding="utf-8")
    env = dict(
        os.environ,
        HF_REPO="someone/cfop-triage-ministral3-14b-v5",
        DATASET_DIR=str(dataset),
        STAGE_DIR=str(tmp_path / stage),
        SRC_DIR=str(src),
        MANIFEST=str(manifest),
    )
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(HF_DIR / "publish.sh"), "--dry-run"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )


def test_dry_run_passes_when_every_artifact_matches_the_manifest(tmp_path: Path):
    src, manifest = _fake_src(tmp_path)
    proc = _run_dry(tmp_path, src, manifest)
    assert proc.returncode == 0, proc.stderr
    assert "nothing uploaded" in proc.stderr
    assert f"ok  {Q4}" in proc.stderr and f"ok  {Q8}" in proc.stderr


def test_dry_run_fails_on_a_gguf_that_does_not_match_the_manifest(tmp_path: Path):
    src, manifest = _fake_src(tmp_path)
    # Same size, different bytes: exactly the case a size check cannot see.
    (src / Q4).write_bytes(b"Q4 " * 100)
    proc = _run_dry(tmp_path, src, manifest)
    assert proc.returncode != 0
    assert "does not match manifest" in proc.stderr
    assert "nothing uploaded" not in proc.stderr


def test_dry_run_fails_without_a_manifest(tmp_path: Path):
    src, manifest = _fake_src(tmp_path)
    manifest.unlink()
    proc = _run_dry(tmp_path, src, manifest)
    assert proc.returncode != 0
    assert "manifest" in proc.stderr and "missing" in proc.stderr


def test_dry_run_requires_the_adapter_in_the_manifest_too(tmp_path: Path):
    src, manifest = _fake_src(tmp_path)
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "adapter_model.safetensors").write_bytes(b"\0" * 16)
    (adapter / "adapter_config.json").write_text("{}", encoding="utf-8")
    # Not in the manifest: refused.
    proc = _run_dry(tmp_path, src, manifest, {"ADAPTER_DIR": str(adapter)})
    assert proc.returncode != 0
    assert "adapter_model.safetensors is not in" in proc.stderr
    # In the manifest: passes (fresh stage, the first run's is non-empty and refused).
    with manifest.open("a", encoding="utf-8") as fh:
        fh.write(f"{_sha256(adapter / 'adapter_model.safetensors')}  adapter_model.safetensors\n")
        fh.write(f"{_sha256(adapter / 'adapter_config.json')}  adapter_config.json\n")
    proc = _run_dry(tmp_path, src, manifest, {"ADAPTER_DIR": str(adapter)}, stage="stage2")
    assert proc.returncode == 0, proc.stderr
    assert "ok  adapter_model.safetensors" in proc.stderr
