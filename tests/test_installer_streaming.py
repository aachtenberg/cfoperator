"""A `curl … | sh` installer must read the whole script before any of it runs.

sh executes a script as it streams in. Without a wrapper, a download that is
cut off halfway runs whatever complete lines had arrived: for install.sh the
prerequisite checks and the download, then a stop somewhere in the unpack; for
install-cfassist.sh a fetch with the checksum step never reached. Wrapping the
body in main() and calling it on the last line makes a truncated copy an
unterminated function: a syntax error that executes nothing (CFOP-280).

The guard is behavioural and covers every scripts/install*.sh, not today's two.
Truncated copies are fed to sh the way curl would feed them, on a PATH with no
docker, no curl and no wget, and nothing may have run.
"""

from repo_paths import REPO_ROOT
import shutil
import subprocess

import pytest

INSTALLERS = sorted((REPO_ROOT / "scripts").glob("install*.sh"))
#: Fractions of the script's lines to keep. The later cuts land after the first
#: side effect in both scripts (the download); the earlier ones inside the
#: argument parsing and prerequisite checks.
CUTS = (0.25, 0.5, 0.75, 0.9)
TOOLS = ("uname", "tr", "mktemp", "rm", "mkdir", "grep", "cut", "sed", "head", "tail",
         "cat", "id", "date", "find", "sort", "mv", "cp", "tar", "gzip", "rmdir",
         "sha256sum", "install", "chmod")


@pytest.fixture
def bare_machine(tmp_path):
    """sh and coreutils only: no docker, no curl, no wget, no network."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # dash where available: it is /bin/sh on Debian and Raspberry Pi OS.
    (bin_dir / "sh").symlink_to(shutil.which("dash") or shutil.which("sh"))
    for tool in TOOLS:
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    home = tmp_path / "home"
    home.mkdir()
    return {
        "PATH": str(bin_dir),
        "HOME": str(home),
        # Were anything to run, its first network step must fail fast.
        "CFOP_BASE_URL": "http://127.0.0.1:9",
        "CFASSIST_BASE_URL": "http://127.0.0.1:9",
    }


def _pipe(script_text, env, *args):
    return subprocess.run([f"{env['PATH']}/sh", "-s", "--", *args], input=script_text,
                          capture_output=True, text=True, env=env, timeout=60)


def _nothing_ran(script, proc):
    prefix = f"{script.stem}: "  # both scripts prefix die() with their own name
    assert proc.stdout == "", f"{script.name}: a truncated copy printed:\n{proc.stdout}"
    reached = [ln for ln in proc.stderr.splitlines() if ln.startswith(prefix)]
    assert not reached, f"{script.name}: a truncated copy reached its own code:\n{reached}"


@pytest.mark.parametrize("cut", CUTS)
@pytest.mark.parametrize("script", INSTALLERS, ids=lambda p: p.name)
def test_a_truncated_download_is_a_syntax_error_not_a_partial_install(script, cut, bare_machine):
    lines = script.read_text().splitlines(keepends=True)
    truncated = "".join(lines[: int(len(lines) * cut)])
    proc = _pipe(truncated, bare_machine)
    _nothing_ran(script, proc)
    assert proc.returncode != 0
    assert "syntax error" in proc.stderr.lower(), (
        f"{script.name} cut at {cut:.0%}: sh accepted it as complete, so the lines "
        f"before the cut were executed one by one as they arrived:\n{proc.stderr}")


@pytest.mark.parametrize("script", INSTALLERS, ids=lambda p: p.name)
def test_everything_but_the_final_call_does_nothing(script, bare_machine):
    """The last line is `main "$@"`. A copy missing only that line is a
    complete script that does nothing, which is the proof that nothing above
    it has an effect of its own."""
    lines = script.read_text().splitlines(keepends=True)
    assert lines[-1].strip() == 'main "$@"', f"{script.name} must end by calling main"
    proc = _pipe("".join(lines[:-1]), bare_machine)
    _nothing_ran(script, proc)
    assert proc.returncode == 0 and proc.stderr == "", proc.stderr


@pytest.mark.parametrize("script", INSTALLERS, ids=lambda p: p.name)
def test_the_whole_script_still_runs_through_the_pipe(script, bare_machine):
    """The wrapper must not change how the one-liner invokes the script."""
    proc = _pipe(script.read_text(), bare_machine, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "url:" in proc.stdout, proc.stdout
