"""The stack one-liner (scripts/install.sh, CFOP-273) has to install a working
trial on a machine with only Docker on it, and must never install anything it
could not verify.

Run against the real script, not read: a stub release served over HTTP, built
by the real ``scripts/release_bundle.py``, and a stub ``docker`` on a curated
PATH that records every invocation. The PATH is curated so the host's own
Docker — present on CI runners — can never be the one that answers.

The classes of regression guarded:

* **verification** — a tampered bundle or a missing checksums file installs
  nothing and starts nothing;
* **pinning** — init and the stack run the image the bundle names, never a
  floating tag;
* **init from the stack's network** — the wizard runs in that image as the
  invoking user with the compose services' host-gateway entry (the localhost
  bug this exists to close);
* **upgrade keeps state** — a second run keeps .env byte-for-byte and does not
  ask the questions again;
* **release plumbing** — the asset the script asks for is the one the workflow
  publishes, from a job that waits for the image, onto a pointer that is never
  deleted first.
"""

from repo_paths import REPO_ROOT
import hashlib
import http.server
import os
import shutil
import subprocess
import sys
import threading

import pytest
import yaml

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import release_bundle  # noqa: E402

SCRIPT = REPO_ROOT / "scripts" / "install.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-cfoperator-main.yml"
IMAGE = "ghcr.io/aachtenberg/cfoperator:v9.9.9"

#: What the script needs besides docker. Linked into a private bin dir so the
#: test controls exactly which docker exists.
TOOLS = ("sh", "curl", "sha256sum", "tar", "gzip", "sed", "grep", "cut", "mktemp",
         "rm", "mkdir", "cp", "head", "tail", "id", "cat", "dirname")

STUB_DOCKER = r"""#!/bin/sh
# Records argv, one line per call; behaves like enough of docker for install.sh.
echo "$*" >> "$DOCKER_LOG"
case "$1" in
	compose) exit 0 ;;
	info)
		if [ -n "${DOCKER_INFO_ERR:-}" ]; then echo "$DOCKER_INFO_ERR" >&2; exit 1; fi
		exit 0 ;;
	pull) exit 0 ;;
	run)
		# Play the wizard: write .env into whatever is mounted at /out.
		prev=""
		for a in "$@"; do
			if [ "$prev" = "-v" ]; then out="${a%%:/out}"; fi
			prev="$a"
		done
		[ "${DOCKER_RUN_EXIT:-0}" = 0 ] || exit "$DOCKER_RUN_EXIT"
		printf 'CFOP_ADMIN_USERNAME=admin\nCFOP_ADMIN_PASSWORD=from-the-wizard\n' > "$out/.env"
		exit 0 ;;
esac
exit 0
"""


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def release(tmp_path):
    """A GitHub-shaped release download path holding a real bundle."""
    www = tmp_path / "www"
    pointer = www / "cfoperator-latest"
    release_bundle.build(IMAGE, pointer)
    digest = hashlib.sha256((pointer / release_bundle.ASSET).read_bytes()).hexdigest()
    (pointer / "checksums.txt").write_text(f"{digest}  {release_bundle.ASSET}\n")
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), lambda *a, **k: _Handler(*a, directory=str(www), **k))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield pointer, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def machine(tmp_path):
    """A home, a curated PATH with the stub docker, and the env to run in."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in TOOLS:
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    home = tmp_path / "home"
    home.mkdir()
    log = tmp_path / "docker.log"
    log.touch()
    env = {
        "PATH": str(bin_dir),
        "HOME": str(home),
        "DOCKER_LOG": str(log),
        # Openable, so the "is there a terminal" check passes; the stub
        # wizard never reads it.
        "CFOP_TTY": os.devnull,
    }
    return {"bin": bin_dir, "home": home, "log": log, "env": env,
            "dir": home / "cfoperator"}


def _with_docker(machine):
    stub = machine["bin"] / "docker"
    stub.write_text(STUB_DOCKER)
    stub.chmod(0o755)


def run(machine, *args, base_url="http://127.0.0.1:9", **env_extra):
    env = dict(machine["env"], CFOP_BASE_URL=base_url, **env_extra)
    return subprocess.run([str(machine["bin"] / "sh"), str(SCRIPT), *args],
                          capture_output=True, text=True, env=env)


def docker_calls(machine):
    return [ln for ln in machine["log"].read_text().splitlines() if ln]


# --- the script itself ---------------------------------------------------------


def test_the_script_is_executable_posix_sh():
    assert SCRIPT.stat().st_mode & 0o111, "the one-liner pipes it to sh, but it is also run directly"
    subprocess.run(["dash" if shutil.which("dash") else "sh", "-n", str(SCRIPT)], check=True)


def test_the_default_is_the_moving_pointer_and_a_version_pins_it(machine):
    default = run(machine, "--dry-run").stdout
    assert "/cfoperator-latest/" + release_bundle.ASSET in default, default
    for pin in ("1.2.3", "v1.2.3"):
        out = run(machine, "--dry-run", CFOP_VERSION=pin).stdout
        assert "/v1.2.3/" + release_bundle.ASSET in out, f"CFOP_VERSION={pin}: {out}"


# --- prerequisites come first ----------------------------------------------------


def test_no_docker_fails_before_anything_is_downloaded(machine, release):
    _, base_url = release
    proc = run(machine, base_url=base_url)
    assert proc.returncode != 0
    assert "Docker is required" in proc.stderr, proc.stderr
    assert not machine["dir"].exists(), "nothing may be unpacked on a machine that cannot run it"


def test_a_daemon_this_user_cannot_reach_names_the_fix(machine, release):
    _with_docker(machine)
    _, base_url = release
    proc = run(machine, base_url=base_url,
               DOCKER_INFO_ERR="permission denied while trying to connect to the Docker daemon socket")
    assert proc.returncode != 0
    assert "docker group" in proc.stderr, proc.stderr
    assert not machine["dir"].exists()


# --- verification ------------------------------------------------------------------


def test_a_tampered_bundle_is_refused_and_nothing_runs(machine, release):
    _with_docker(machine)
    pointer, base_url = release
    with open(pointer / release_bundle.ASSET, "ab") as fh:
        fh.write(b"tampered")
    proc = run(machine, base_url=base_url)
    assert proc.returncode != 0
    assert "checksum mismatch" in proc.stderr, proc.stderr
    assert not (machine["dir"] / "docker-compose.yml").exists()
    calls = docker_calls(machine)
    assert not [c for c in calls if c.startswith(("pull", "run", "compose up"))], calls


def test_a_missing_checksums_file_is_fatal_not_a_warning(machine, release):
    _with_docker(machine)
    pointer, base_url = release
    (pointer / "checksums.txt").unlink()
    proc = run(machine, base_url=base_url)
    assert proc.returncode != 0
    assert "refusing to install unverified" in proc.stderr, proc.stderr
    assert not (machine["dir"] / "docker-compose.yml").exists()


# --- a first install ------------------------------------------------------------------


def test_a_first_install_configures_in_the_pinned_image_then_starts(machine, release):
    _with_docker(machine)
    _, base_url = release
    proc = run(machine, base_url=base_url)
    assert proc.returncode == 0, proc.stderr

    for name in release_bundle.bundle_files():
        assert (machine["dir"] / name).exists(), f"{name} was in the bundle but not installed"
    compose = yaml.safe_load((machine["dir"] / "docker-compose.yml").read_text())
    assert not [n for n, s in compose["services"].items() if "build" in s], \
        "an installed compose file must pull, not build — there is no source tree"

    calls = docker_calls(machine)
    assert f"pull {IMAGE}" in calls, calls
    [init] = [c for c in calls if c.startswith("run ")]
    uid_gid = f"{os.getuid()}:{os.getgid()}"
    assert f"--user {uid_gid}" in init, f"init must write .env as the invoking user: {init}"
    assert "--add-host host.docker.internal:host-gateway" in init, (
        "init must probe from the stack's network, where this machine is "
        f"host.docker.internal: {init}")
    assert f"-v {machine['dir']}:/out" in init, init
    assert f" {IMAGE} python scripts/setup_wizard.py --dir /out" in init, (
        f"init must run in the image the bundle pins, never a floating tag: {init}")
    assert calls.index(init) < calls.index("compose up -d"), "configure, then start"
    assert "http://localhost:8083" in proc.stdout


def test_no_terminal_installs_but_neither_configures_nor_starts(machine, release):
    _with_docker(machine)
    _, base_url = release
    proc = run(machine, base_url=base_url, CFOP_TTY=str(machine["home"] / "no-such-tty"))
    assert proc.returncode == 0, proc.stderr
    assert "No terminal" in proc.stdout and "setup_wizard.py" in proc.stdout, proc.stdout
    calls = docker_calls(machine)
    assert not [c for c in calls if c.startswith(("run ", "compose up"))], calls


def test_a_failed_setup_starts_nothing(machine, release):
    _with_docker(machine)
    _, base_url = release
    proc = run(machine, base_url=base_url, DOCKER_RUN_EXIT="1")
    assert proc.returncode != 0
    assert "compose up -d" not in docker_calls(machine)


def test_no_start_configures_but_does_not_start(machine, release):
    _with_docker(machine)
    _, base_url = release
    proc = run(machine, "--no-start", base_url=base_url)
    assert proc.returncode == 0, proc.stderr
    calls = docker_calls(machine)
    assert [c for c in calls if c.startswith("run ")], "configuring is still the point"
    assert "compose up -d" not in calls


# --- an upgrade -------------------------------------------------------------------------


def test_a_second_run_keeps_env_skips_init_and_restores_compose_files(machine, release):
    _with_docker(machine)
    _, base_url = release
    assert run(machine, base_url=base_url).returncode == 0
    env_file = machine["dir"] / ".env"
    env_file.write_text("CFOP_ADMIN_PASSWORD=edited-by-the-operator\n")
    compose = machine["dir"] / "docker-compose.yml"
    compose.write_text("services: {}\n")
    machine["log"].write_text("")

    proc = run(machine, base_url=base_url)
    assert proc.returncode == 0, proc.stderr
    assert env_file.read_text() == "CFOP_ADMIN_PASSWORD=edited-by-the-operator\n", \
        "an upgrade must never touch .env"
    assert IMAGE in compose.read_text(), "the compose files are the release's, not whatever was left"
    calls = docker_calls(machine)
    assert not [c for c in calls if c.startswith("run ")], f"an upgrade must not re-ask the questions: {calls}"
    assert "compose up -d" in calls


# --- release plumbing ---------------------------------------------------------------------


def _jobs():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _live(text):
    return [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_the_asset_the_script_asks_for_is_the_one_the_workflow_publishes():
    script = SCRIPT.read_text()
    assert f'ASSET="{release_bundle.ASSET}"' in script
    job = _jobs()["release-stack"]
    published = "\n".join(str(step.get("with", {}).get("files", "")) for step in job["steps"])
    assert f"dist/{release_bundle.ASSET}" in published and "dist/checksums.txt" in published, published
    run_text = "\n".join(step.get("run", "") for step in job["steps"])
    assert "scripts/release_bundle.py" in run_text and f"sha256sum {release_bundle.ASSET}" in run_text


def test_the_release_waits_for_an_image_that_passed_db_smoke():
    """The bundle pins this tag's image; published first, it can name an image
    that is not pushed yet, or one whose agent cannot reach its database."""
    job = _jobs()["release-stack"]
    needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
    assert {"build-and-push", "db-smoke"} <= set(needs), needs
    assert job.get("if") == "github.ref_type == 'tag'", "main pushes publish no release"


def test_tag_builds_publish_arm64_and_main_builds_stay_amd64():
    [build] = [s for s in _jobs()["build-and-push"]["steps"] if s.get("name") == "Build and push"]
    platforms = build["with"]["platforms"]
    assert "github.ref_type == 'tag'" in platforms and "linux/arm64" in platforms, platforms
    assert platforms.rstrip(" }").endswith("'linux/amd64'"), (
        f"the non-tag branch must stay amd64-only — prod is pinned to the amd64 GPU node: {platforms}")


def test_the_pointer_is_refreshed_in_place_never_deleted_first():
    job = _jobs()["release-stack"]
    lines = _live("\n".join(step.get("run", "") for step in job["steps"]))
    assert not [ln for ln in lines if "gh release delete" in ln], \
        "a delete before the create 404s every install until the next tag"
    creates = [i for i, ln in enumerate(lines) if "gh release create cfoperator-latest" in ln]
    uploads = [i for i, ln in enumerate(lines) if "gh release upload cfoperator-latest" in ln]
    assert creates and uploads and creates[0] < uploads[0], "ensure the pointer exists, then clobber its assets"
    assert any("--clobber" in ln for ln in lines)
    assert "cfoperator-latest" in SCRIPT.read_text(), "the script's default must be the pointer the job maintains"
