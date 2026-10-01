"""Every database layer boots against a real Postgres and round-trips a write.

Runs scripts/db_smoke.py, the same check the build workflow runs inside the
just-built image before the deploy bump (CFOP-249). Here it catches a driver,
schema or query change on the PR; there it catches what the image actually
installed. It runs as a subprocess because the agent's modules import each
other bare and need agent/ on sys.path, which must not leak into this shared
test process.

Needs CFOP_TEST_PG_DSN with CREATE DATABASE and CREATE EXTENSION rights. CI
provides it (tests.yml, a pgvector/pgvector:pg15 service) and these FAIL there
without it rather than skipping. Locally, without it, they skip.
"""
import os
import subprocess
import sys

import pytest
import yaml

from repo_paths import REPO_ROOT

DSN = os.getenv("CFOP_TEST_PG_DSN", "")
SMOKE = REPO_ROOT / "scripts" / "db_smoke.py"
BUILD = REPO_ROOT / ".github" / "workflows" / "build-cfoperator-main.yml"


@pytest.mark.skipif(not DSN and not os.getenv("CI"),
                    reason="set CFOP_TEST_PG_DSN to run the database smoke")
def test_every_database_layer_boots_and_round_trips():
    assert DSN, ("CFOP_TEST_PG_DSN is unset. CI must provide it (tests.yml postgres service); "
                 "without it the database smoke would pass having tested nothing.")
    run = subprocess.run([sys.executable, str(SMOKE), DSN], cwd=REPO_ROOT,
                         capture_output=True, text=True, timeout=300,
                         env={**os.environ, "CFOP_DB_SMOKE_TRACEBACK": "1"})
    report = run.stdout + run.stderr
    assert run.returncode == 0, report
    for step in ("driver", "knowledge base", "auth store", "event runtime", "timescale tool"):
        assert f"ok   {step}:" in run.stdout, f"{step} did not run:\n{report}"


def test_the_deploy_bump_waits_for_the_database_smoke():
    """The gate is only a gate if the bump needs it, and only meaningful if it
    runs the smoke inside the image that build-and-push just built."""
    jobs = yaml.safe_load(BUILD.read_text(encoding="utf-8"))["jobs"]
    needs = jobs["bump-deploy-repo"]["needs"]
    assert "db-smoke" in ([needs] if isinstance(needs, str) else needs), \
        "bump-deploy-repo no longer waits for db-smoke"
    smoke = jobs["db-smoke"]
    assert smoke["needs"] == "build-and-push"
    assert smoke["services"]["postgres"]["image"].startswith("pgvector/pgvector:"), \
        "db-smoke's Postgres lacks pgvector, which the knowledge base needs"
    script = "\n".join(step.get("run", "") for step in smoke["steps"])
    assert "scripts/db_smoke.py" in script and "docker run" in script
    image = "\n".join(str(step.get("env", {}).get("IMAGE", "")) for step in smoke["steps"])
    assert "needs.build-and-push.outputs.image-tag" in image, \
        "db-smoke does not run the image that was just built"
