"""The install one-liner has to work on a machine with nothing on it.

``gh release download`` was the documented install, and ``gh`` is not on a stock
Raspberry Pi OS — the machine where cfassist is most often wanted. This guards
the script that replaced it, and the two ways it can silently stop working:

* the asset names it builds drift from the ones the release workflow publishes
  (a 404 for whoever runs it), and
* the checksum verification stops being enforced (the failure that matters in
  anything invoked as ``curl … | sh``).

The second is checked by running the real script against a stub release served
over HTTP, not by looking for the word "sha256" in the source — a script that
computes a checksum and ignores the result would pass that.
"""

import http.server
import pathlib
import re
import shutil
import subprocess
import threading

import pytest

ROOT = pathlib.Path(__file__).parent
SCRIPT = ROOT / "scripts" / "install-cfassist.sh"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release-cfassist.yml"

# uname -m values seen in this fleet and what they must resolve to.
ARCH_CASES = [
    ("linux", "x86_64", "cfassist-linux-amd64"),
    ("linux", "amd64", "cfassist-linux-amd64"),
    ("linux", "aarch64", "cfassist-linux-arm64"),   # 64-bit Raspberry Pi OS
    ("linux", "arm64", "cfassist-linux-arm64"),
    ("linux", "armv7l", "cfassist-linux-arm"),      # 32-bit Raspberry Pi OS
    ("linux", "armv6l", "cfassist-linux-arm"),
    ("darwin", "x86_64", "cfassist-darwin-amd64"),
    ("darwin", "arm64", "cfassist-darwin-arm64"),
]


STUB_BINARY = b"""#!/bin/sh
# The installer probes `help init` before calling `init`. A stub that exits 0
# for every invocation would make that probe a lie, and `init` would not write.
case "$1" in
help)
	if [ "$2" = "init" ]; then exit 0; fi
	exit 1
	;;
init)
	mkdir -p "$HOME/.cfassist"
	if [ -f "$HOME/.cfassist/config.yaml" ]; then
		echo "Already exists: $HOME/.cfassist/config.yaml"
	else
		echo "# cfassist configuration" > "$HOME/.cfassist/config.yaml"
		echo "Wrote $HOME/.cfassist/config.yaml"
	fi
	exit 0
	;;
*)
	echo 'cfassist 9.9.9'
	exit 0
	;;
esac
"""


def run_script(*args, env_extra=None, expect_ok=True):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp"}
    env.update(env_extra or {})
    proc = subprocess.run(
        ["sh", str(SCRIPT), *args],
        capture_output=True, text=True, env=env,
    )
    if expect_ok:
        assert proc.returncode == 0, f"script failed: {proc.stderr}"
    return proc


def test_the_script_is_executable_posix_sh():
    assert SCRIPT.is_file(), "scripts/install-cfassist.sh is the documented install"
    assert SCRIPT.stat().st_mode & 0o111, "must be executable"
    # dash, not bash: /bin/sh on Debian and Raspberry Pi OS.
    subprocess.run(["dash" if shutil.which("dash") else "sh", "-n", str(SCRIPT)], check=True)


@pytest.mark.parametrize("os_name,machine,expected", ARCH_CASES)
def test_arch_detection(os_name, machine, expected):
    out = run_script("--dry-run", env_extra={
        "CFASSIST_OS": os_name, "CFASSIST_ARCH": machine,
    }).stdout
    assert f"asset:   {expected}" in out, out


def test_an_unsupported_architecture_says_so_instead_of_404ing():
    proc = run_script("--dry-run", env_extra={"CFASSIST_ARCH": "riscv64"}, expect_ok=False)
    assert proc.returncode != 0
    assert "riscv64" in proc.stderr and "published" in proc.stderr, proc.stderr


def test_the_default_is_the_moving_pointer_and_a_version_pins_it():
    """No version in the URL by default — that is the whole point of the tag."""
    default = run_script("--dry-run").stdout
    assert "/cfassist-latest/" in default, default

    pinned = run_script("--dry-run", env_extra={"CFASSIST_VERSION": "0.10.0"}).stdout
    assert "/cfassist-v0.10.0/" in pinned, pinned


# --- the asset-name contract -------------------------------------------------


def published_suffixes() -> set[str]:
    """The `suffix:` values release-cfassist.yml's build matrix produces."""
    text = RELEASE_WORKFLOW.read_text()
    return set(re.findall(r"^\s*suffix:\s*(\S+)\s*$", text, re.MULTILINE))


def test_every_asset_the_script_can_ask_for_is_one_the_workflow_builds():
    """Drift here is a 404 for whoever runs the installer, and nothing else catches it."""
    published = published_suffixes()
    assert published, "could not parse the build matrix"

    wanted = set()
    for os_name, machine, _ in ARCH_CASES:
        out = run_script("--dry-run", env_extra={
            "CFASSIST_OS": os_name, "CFASSIST_ARCH": machine,
        }).stdout
        wanted.add(out.split("asset:")[1].split("\n")[0].strip().removeprefix("cfassist-"))

    missing = wanted - published
    assert not missing, (
        f"the installer would download {missing}, which release-cfassist.yml does not build"
    )


def live_lines(text: str) -> list[str]:
    """Workflow lines that actually run — comments do not maintain a pointer."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _versioned_release_half() -> str:
    """Everything in the workflow before the pointer refresh.

    The numbered tag is what GitHub badges Latest (`--latest=false` on the
    pointer is load-bearing). Install instructions that only exist on
    cfassist-latest never appear on the page people land on.
    """
    text = RELEASE_WORKFLOW.read_text()
    versioned, sep, _ = text.partition("Refresh the cfassist-latest pointer")
    assert sep, "could not split the versioned release from the pointer refresh"
    return "\n".join(live_lines(versioned))


def test_the_versioned_release_notes_include_the_install_one_liner():
    """generate_release_notes alone is a changelog with no install — that is
    the current Latest page. The body is prepended to the generated notes, so
    both have to be present; dropping the body is how this regresses."""
    live = _versioned_release_half()

    assert "generate_release_notes: true" in live, (
        "keep the changelog; the install block is prepended, not a replacement"
    )
    # The Create release step itself, not a write-a-file-and-forget-to-attach-it
    # step: a prefix file that is never passed as body/body_path is a no-op.
    create = live.split("- name: Create release", 1)
    assert len(create) == 2, "the versioned release step is missing"
    create_step = create[1].split("- name:", 1)[0]
    assert "body:" in create_step, (
        "the numbered release must set `body:` (prepended to generated notes); "
        "a comment or the pointer-only NOTES does not reach the Latest page"
    )
    assert "install-cfassist.sh" in create_step, (
        "the Latest-badged page has to carry the install one-liner, not just "
        "a changelog of PRs"
    )
    assert "CFASSIST_VERSION=" in create_step, (
        "someone who opened this tag rather than latest needs a pin, not only "
        "the moving one-liner"
    )
    # The build job also strips GITHUB_REF_NAME#cfassist-v for ldflags, so
    # asserting that string against the whole file (or everything before the
    # pointer refresh) stays green if this pin step is deleted and the
    # rendered notes say `CFASSIST_VERSION=` with nothing after it.
    assert "steps.version.outputs.number" in create_step, (
        "the pin must come from the Version number step; the build job's "
        "ldflags strip is a different one"
    )
    version = live.split("- name: Version number for the pin", 1)
    assert len(version) == 2, "the pin step is missing"
    version_step = version[1].split("- name:", 1)[0]
    assert "GITHUB_REF_NAME#cfassist-v" in version_step, (
        "CFASSIST_VERSION=cfassist-v0.11.0 404s; strip the tag prefix so the "
        "pin is 0.11.0"
    )


def test_the_release_workflow_maintains_the_latest_pointer():
    """Freezing the pointer is worse than breaking it: the install would keep
    working and keep delivering something old.

    Asserted against executable lines, not the file as a whole: a commented-out
    `gh release upload` leaves every string this checks sitting in the comment
    block above it, and the pointer silently stops moving."""
    lines = live_lines(RELEASE_WORKFLOW.read_text())
    body = "\n".join(lines)

    assert any("gh release upload cfassist-latest" in ln for ln in lines), (
        "release-cfassist.yml must upload the new binaries onto cfassist-latest; "
        "without it the install one-liner keeps serving whatever it last pointed to"
    )
    assert any("gh release create cfassist-latest" in ln for ln in lines), (
        "the pointer has to be created the first time, or the upload above has "
        "nothing to clobber"
    )
    assert "--latest=false" in body, (
        "the pointer must not take the Latest badge from the numbered releases"
    )


def test_the_pointer_is_never_deleted_before_it_is_replaced():
    """A failed refresh must not be worse than a stale one.

    `gh release delete cfassist-latest` followed by a create leaves a window
    where the documented one-liner 404s — and if the create fails, that window
    lasts until the next successful version tag. Updating in place has no
    window at all."""
    lines = live_lines(RELEASE_WORKFLOW.read_text())
    deleting = [ln for ln in lines if "gh release delete" in ln and "cfassist-latest" in ln]
    assert not deleting, (
        f"the pointer is deleted before being recreated: {deleting}. "
        "Use `gh release upload --clobber` onto the standing release instead — "
        "a create that fails after a delete 404s every install until the next tag."
    )


# --- checksum enforcement, end to end ----------------------------------------


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def stub_release(tmp_path):
    """A directory served over HTTP, shaped like a GitHub release download path."""
    root = tmp_path / "www"
    (root / "cfassist-latest").mkdir(parents=True)
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), lambda *a, **k: _Handler(*a, directory=str(root), **k)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield root / "cfassist-latest", f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _stub_binary(release_dir, asset, body=STUB_BINARY):
    (release_dir / asset).write_bytes(body)
    digest = subprocess.run(
        ["sha256sum", str(release_dir / asset)], capture_output=True, text=True, check=True
    ).stdout.split()[0]
    return digest


def test_a_tampered_binary_is_refused_and_nothing_is_installed(stub_release, tmp_path):
    release_dir, base_url = stub_release
    asset = "cfassist-linux-amd64"
    _stub_binary(release_dir, asset)
    # A checksum for different content: what a swapped artifact looks like.
    (release_dir / "checksums.txt").write_text(f"{'0' * 64}  {asset}\n")

    bindir = tmp_path / "bin"
    proc = run_script(env_extra={
        "CFASSIST_OS": "linux", "CFASSIST_ARCH": "amd64",
        "CFASSIST_BASE_URL": base_url, "CFASSIST_INSTALL_DIR": str(bindir),
    }, expect_ok=False)

    assert proc.returncode != 0, "a checksum mismatch must not install"
    assert "checksum mismatch" in proc.stderr, proc.stderr
    assert not (bindir / "cfassist").exists(), "a refused install must leave nothing behind"


def test_a_missing_checksums_file_is_fatal_not_a_warning(stub_release, tmp_path):
    """"Could not check" and "checked, fine" must never look the same."""
    release_dir, base_url = stub_release
    _stub_binary(release_dir, "cfassist-linux-amd64")  # no checksums.txt alongside

    bindir = tmp_path / "bin"
    proc = run_script(env_extra={
        "CFASSIST_OS": "linux", "CFASSIST_ARCH": "amd64",
        "CFASSIST_BASE_URL": base_url, "CFASSIST_INSTALL_DIR": str(bindir),
    }, expect_ok=False)

    assert proc.returncode != 0
    assert "unverified" in proc.stderr, proc.stderr
    assert not (bindir / "cfassist").exists()


def test_a_good_download_installs_and_reports_its_version(stub_release, tmp_path):
    release_dir, base_url = stub_release
    asset = "cfassist-linux-amd64"
    digest = _stub_binary(release_dir, asset)
    (release_dir / "checksums.txt").write_text(f"{digest}  {asset}\n")

    bindir = tmp_path / "bin"
    home = tmp_path / "home"
    home.mkdir()
    proc = run_script(env_extra={
        "CFASSIST_OS": "linux", "CFASSIST_ARCH": "amd64",
        "CFASSIST_BASE_URL": base_url, "CFASSIST_INSTALL_DIR": str(bindir),
        "HOME": str(home),
    })

    installed = bindir / "cfassist"
    assert installed.is_file() and installed.stat().st_mode & 0o111
    assert "9.9.9" in proc.stdout, proc.stdout
    assert "cfassist attach" in proc.stdout, "the next steps are part of the install"
    assert (home / ".cfassist" / "config.yaml").is_file(), (
        "install must write ~/.cfassist/config.yaml; --version does not"
    )


def test_a_deleted_config_is_rewritten_on_install(stub_release, tmp_path):
    """The reported path: rm the config, rerun the one-liner, find no file.

    `--version` (the installer's only previous invocation) returns before the
    binary writes config.yaml. `init` is that write; without it, reinstall
    is a binary and nothing to edit."""
    release_dir, base_url = stub_release
    digest = _stub_binary(release_dir, "cfassist-linux-amd64")
    (release_dir / "checksums.txt").write_text(f"{digest}  cfassist-linux-amd64\n")

    bindir = tmp_path / "bin"
    home = tmp_path / "home"
    cfg = home / ".cfassist" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("llm:\n  model: old\n")

    env = {
        "CFASSIST_OS": "linux", "CFASSIST_ARCH": "amd64",
        "CFASSIST_BASE_URL": base_url, "CFASSIST_INSTALL_DIR": str(bindir),
        "HOME": str(home),
    }
    run_script(env_extra=env)
    assert "old" in cfg.read_text(), "install must not overwrite a present config"

    cfg.unlink()
    proc = run_script(env_extra=env)
    assert cfg.is_file(), proc.stdout + proc.stderr
    assert "old" not in cfg.read_text()
    assert "Wrote" in proc.stdout, proc.stdout


def test_the_installer_probes_help_before_calling_init():
    """`cfassist init` on a binary from before this verb is a one-shot LLM
    prompt, not a scaffold. The probe is load-bearing."""
    live = "\n".join(live_lines(SCRIPT.read_text()))
    assert "help init" in live, (
        "probe `help init` before calling init, or a CFASSIST_VERSION pin of "
        "an older binary will start a session named 'init'"
    )
    assert "cfassist\" init" in live or 'cfassist init' in live
