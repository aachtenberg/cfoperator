"""The release's compose bundle (scripts/release_bundle.py, CFOP-273).

The bundle is what scripts/install.sh installs in place of a clone, so two
things must hold for every future edit of docker-compose.yml, not just today's:

* the rendered compose is the repo's compose with exactly one change — every
  built service pulls the release image — and an edit the transform does not
  understand fails the release instead of shipping a half-rendered file;
* every file the compose files mount from the install directory is in the
  bundle, derived from the mounts rather than listed, so a new mount cannot be
  forgotten.
"""

from repo_paths import REPO_ROOT
import io
import sys
import tarfile

import pytest
import yaml

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import release_bundle as rb  # noqa: E402

IMAGE = "ghcr.io/aachtenberg/cfoperator:v9.9.9"


def _services(text):
    return yaml.safe_load(text)["services"]


def test_the_repo_compose_renders_to_pull_only_with_nothing_else_changed():
    source_text = (REPO_ROOT / rb.COMPOSE).read_text()
    source, rendered = _services(source_text), _services(rb.render(source_text, IMAGE))
    built = [n for n, s in source.items() if "build" in s]
    assert built, "the trial compose builds its cfoperator services; if it stopped, revisit this bundle"
    assert not [n for n, s in rendered.items() if "build" in s]
    for name in built:
        assert rendered[name]["image"] == IMAGE, name
        expected = {k: v for k, v in source[name].items() if k not in ("build", "image")}
        assert {k: v for k, v in rendered[name].items() if k != "image"} == expected, name
    for name in set(source) - set(built):
        assert rendered[name] == source[name], f"{name} does not build and must render unchanged"


def test_rendering_keeps_the_compose_files_comments():
    source_text = (REPO_ROOT / rb.COMPOSE).read_text()
    comments = [ln.strip() for ln in source_text.splitlines() if ln.strip().startswith("#")]
    rendered = rb.render(source_text, IMAGE)
    missing = [c for c in comments if c not in rendered]
    assert not missing, f"the compose file's comments are its documentation: {missing[:3]}"


@pytest.mark.parametrize("compose,why", [
    ("services:\n  a:\n    build:\n      context: .\n    image: cfoperator:local\n",
     "a multi-line build: block is not stripped by the line transform"),
    ("services:\n  a:\n    build: .\n    image: other:local\n",
     "a built service whose image is not cfoperator:local would keep its old name"),
    ("services:\n  a:\n    build: .\n",
     "a built service with no image line would end up with no image at all"),
])
def test_a_compose_shape_the_transform_does_not_understand_fails_loudly(compose, why):
    with pytest.raises(rb.BundleError):
        rb.render(compose, IMAGE)


def test_the_bundle_carries_every_relative_mount_in_the_repo():
    files = rb.bundle_files()
    assert files[0] == rb.COMPOSE
    assert ".env.example" in files
    for name in [rb.COMPOSE, *rb.OVERLAYS]:
        for source in rb.relative_mounts((REPO_ROOT / name).read_text()):
            assert source in files, f"{name} mounts ./{source} but the bundle would not carry it"
    assert "deploy/compose/config.yaml" in files, "the trial config the services mount"


def test_a_newly_added_mount_travels_without_this_script_changing(tmp_path):
    """The mutation the derivation exists for: add a mount, ship it."""
    (tmp_path / rb.COMPOSE).write_text(
        "services:\n  agent:\n    build: .\n    image: cfoperator:local\n"
        "    volumes:\n      - ./plugins/extra.yaml:/app/extra.yaml:ro\n"
        "      - type: bind\n        source: ./certs\n        target: /certs\n"
        "      - named-volume:/data\n      - ~/.ssh:/root/.ssh:ro\n")
    (tmp_path / ".env.example").write_text("X=1\n")
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "extra.yaml").write_text("a: 1\n")
    (tmp_path / "certs").mkdir()
    (tmp_path / "certs" / "ca.pem").write_text("pem\n")
    assert rb.bundle_files(tmp_path) == [rb.COMPOSE, ".env.example", "certs", "plugins/extra.yaml"]

    out = rb.build(IMAGE, tmp_path / "dist", root=tmp_path)
    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert "certs/ca.pem" in names and "plugins/extra.yaml" in names, names


def test_a_mount_whose_source_is_missing_fails_the_release(tmp_path):
    (tmp_path / rb.COMPOSE).write_text(
        "services:\n  agent:\n    image: x:1\n    volumes:\n      - ./gone.yaml:/x\n")
    (tmp_path / ".env.example").write_text("")
    with pytest.raises(rb.BundleError, match="gone.yaml"):
        rb.bundle_files(tmp_path)


def test_the_bundle_is_reproducible_and_pulls_only(tmp_path):
    first = rb.build(IMAGE, tmp_path / "a").read_bytes()
    second = rb.build(IMAGE, tmp_path / "b").read_bytes()
    assert first == second, "the same commit and image must give the same checksum"

    with tarfile.open(fileobj=io.BytesIO(first)) as tar:
        assert set(tar.getnames()) >= set(rb.bundle_files()) - {"deploy"}
        assert ".env" not in tar.getnames(), "an upgrade must never be able to overwrite .env"
        compose = tar.extractfile(rb.COMPOSE).read().decode()
    assert "build:" not in compose and IMAGE in compose


def test_a_floating_image_is_refused(capsys):
    assert rb.main(["--image", "ghcr.io/aachtenberg/cfoperator", "--out", "unused"]) == 2
    assert "explicit tag" in capsys.readouterr().err


@pytest.mark.parametrize("tag,expected", [
    ("v1.10.0", True),       # newest, compared numerically (1.10 > 1.2)
    ("v1.2.0", False),       # a re-run of an older tag
    ("v1.3.0-rc1", False),   # a pre-release never moves it
    ("v1.11.0", True),       # first build of a new tag
    ("1.11.0", False),       # not a release tag at all
])
def test_the_pointer_moves_only_for_the_newest_final_release(tag, expected):
    existing = ["v0.1.0", "v1.2.0", "v1.10.0", "v1.3.0-rc1", "cfassist-v9.0.0"]
    assert rb.should_move_pointer(tag, existing + [tag]) is expected


def test_a_digest_pinned_image_renders_whole():
    pinned = IMAGE + "@sha256:" + "ab" * 32
    rendered = rb.render((REPO_ROOT / rb.COMPOSE).read_text(), pinned)
    assert f"image: {pinned}\n" in rendered
