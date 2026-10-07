#!/bin/sh
# Install the CFOperator trial stack (Postgres, agent, event runtime, console)
# on this machine, with Docker as the only prerequisite.
#
#   curl -fsSL https://raw.githubusercontent.com/aachtenberg/cfoperator/main/scripts/install.sh | sh
#
# What it does (CFOP-273):
#   1. checks Docker, the compose plugin and the daemon before downloading anything;
#   2. downloads the release's compose bundle and refuses it unless its SHA-256
#      matches the release's checksums.txt;
#   3. unpacks it into $CFOP_INSTALL_DIR (default ~/cfoperator);
#   4. on a first install, runs `cfoperator init` INSIDE the release image, so
#      every URL is probed from the network the stack will actually run on —
#      the trial is bridged, and `localhost` in its containers is the container;
#   5. starts the stack with `docker compose up -d`.
#
# Re-running it upgrades: the compose files are replaced, .env and the database
# volume are kept, init is skipped, and a bundled file edited here is kept as
# <name>.bak. The images are pinned by the bundle; no
# container ever fetches code when it starts.
#
# POSIX sh, not bash: /bin/sh on Debian and Raspberry Pi OS is dash.
#
# Knobs, all optional:
#   CFOP_VERSION       pin an exact release (1.2.3 or v1.2.3); default is the
#                      moving `cfoperator-latest` pointer
#   CFOP_INSTALL_DIR   where the stack lives (default ~/cfoperator)
#   OLLAMA_URL         defaults offered by init; both default to this machine
#   PROMETHEUS_URL     as the containers see it, host.docker.internal
#   CFOP_BASE_URL      override the release host (tests point this at a stub)
#   CFOP_TTY           the terminal init reads answers from (default /dev/tty)
#   --dry-run          print what would happen and exit
#   --no-start         install and configure, but do not start the stack
set -eu

REPO="aachtenberg/cfoperator"
BASE_URL="${CFOP_BASE_URL:-https://github.com/${REPO}/releases/download}"
INSTALL_DIR="${CFOP_INSTALL_DIR:-$HOME/cfoperator}"
TTY="${CFOP_TTY:-/dev/tty}"
# scripts/release_bundle.py ASSET; tests/test_install_stack.py keeps them equal.
ASSET="cfoperator-compose.tar.gz"

# The default is the pointer, not a number, for the reason install-cfassist.sh
# gives: GitHub's /releases/latest is the newest release across every tag
# series in this repo, so it would hand out a cfassist release.
version="${CFOP_VERSION:-}"
version="${version#v}"
if [ -n "$version" ]; then TAG="v${version}"; else TAG="cfoperator-latest"; fi

# Inline, not read back from "$0": under `curl … | sh -s -- --help`, $0 is
# "sh" and there is no file to read (claude-review).
usage() {
	cat <<'EOF'
Install the CFOperator trial stack with Docker as the only prerequisite.

  curl -fsSL https://raw.githubusercontent.com/aachtenberg/cfoperator/main/scripts/install.sh | sh
  curl -fsSL .../install.sh | sh -s -- [--dry-run] [--no-start]

Re-running it upgrades: compose files are replaced, .env and the database
volume are kept, and setup is not asked again.

Environment, all optional:
  CFOP_VERSION      pin a release (1.2.3 or v1.2.3); default cfoperator-latest
  CFOP_INSTALL_DIR  where the stack lives (default ~/cfoperator)
  OLLAMA_URL        setup defaults; both default to this machine as the
  PROMETHEUS_URL    containers see it, host.docker.internal
EOF
}

DRY_RUN=0
NO_START=0
for arg in "$@"; do
	case "$arg" in
		--dry-run) DRY_RUN=1 ;;
		--no-start) NO_START=1 ;;
		-h|--help) usage; exit 0 ;;
		*) echo "install: unknown argument: $arg" >&2; exit 2 ;;
	esac
done

die() { echo "install: $*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

url="${BASE_URL}/${TAG}/${ASSET}"

if [ "$DRY_RUN" = 1 ]; then
	echo "release: ${TAG}"
	echo "url:     ${url}"
	echo "install: ${INSTALL_DIR}"
	exit 0
fi

# --- prerequisites -----------------------------------------------------------
#
# All of them before the first download, so a machine that cannot run the stack
# says so without leaving half an install behind.

have docker || die "Docker is required: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 \
	|| die "the Docker Compose plugin is required (\`docker compose\`, v2): https://docs.docker.com/compose/install/"
if ! err="$(docker info 2>&1 >/dev/null)"; then
	case "$err" in
		*"permission denied"*)
			die "this user cannot reach the Docker daemon.
  Add yourself to the docker group, then log in again:
    sudo usermod -aG docker \"\$USER\"" ;;
		*) die "the Docker daemon is not answering:
  ${err}" ;;
	esac
fi

if have curl; then
	fetch() { curl -fsSL "$1" -o "$2"; }
elif have wget; then
	fetch() { wget -qO "$2" "$1"; }
else
	die "need curl or wget"
fi

if have sha256sum; then
	sum() { sha256sum "$1" | cut -d' ' -f1; }
elif have shasum; then
	sum() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
	die "need sha256sum or shasum to verify the download"
fi

# --- fetch and verify ----------------------------------------------------------
#
# A missing checksums.txt is fatal, not a warning: in anything run as
# `curl … | sh`, "could not check" and "checked, fine" must never look the same.

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT INT TERM

echo "Downloading the CFOperator stack (${TAG})…"
fetch "$url" "$tmp/$ASSET" || die "could not download ${url}
  If you pinned CFOP_VERSION, check that release exists.
  Otherwise the cfoperator-latest pointer may be missing — report it."

fetch "${BASE_URL}/${TAG}/checksums.txt" "$tmp/checksums.txt" \
	|| die "could not download checksums for ${TAG}; refusing to install unverified"

expected="$(grep " ${ASSET}\$" "$tmp/checksums.txt" | cut -d' ' -f1)"
[ -n "$expected" ] || die "${ASSET} is not listed in checksums.txt for ${TAG}"

actual="$(sum "$tmp/$ASSET")"
[ "$actual" = "$expected" ] || die "checksum mismatch for ${ASSET}
  expected ${expected}
  got      ${actual}
  Nothing was installed."

# --- unpack ----------------------------------------------------------------------
#
# Into a staging directory first, so a bundle that does not name its image is
# refused before anything in the install directory changes.

mkdir -p "$tmp/stage"
tar -xzf "$tmp/$ASSET" -C "$tmp/stage"

# The image every cfoperator service pulls — init runs in the same one, never
# a floating tag, so the questions asked match the release being installed.
image="$(sed -n 's|^[[:space:]]*image:[[:space:]]*\([^[:space:]]*/cfoperator:[^[:space:]]*\)[[:space:]]*$|\1|p' \
	"$tmp/stage/docker-compose.yml" | head -n 1)"
[ -n "$image" ] || die "the bundle's docker-compose.yml names no cfoperator image; refusing it"

fresh=1
[ -f "$INSTALL_DIR/.env" ] && fresh=0

mkdir -p "$INSTALL_DIR"
# Absolute from here on: `docker run -v cfoperator:/out` reads a bare relative
# name as a named volume, so init would write .env where nothing looks for it.
INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd)"

# .env is never in the bundle, so an upgrade cannot overwrite it. Bundled files
# can be edited too (deploy/compose/config.yaml is mounted into the services),
# and a release's copy replaces them — so the checksums of what was installed
# are recorded, and a file that no longer matches them was edited here and is
# kept as <name>.bak instead of being lost silently. A file that merely changed
# between releases is not an edit and gets no .bak.
manifest="$INSTALL_DIR/.cfoperator-bundle.sha256"
kept=""
if [ -f "$manifest" ]; then
	while read -r recorded name; do
		[ -f "$INSTALL_DIR/$name" ] || continue
		if [ "$(sum "$INSTALL_DIR/$name")" != "$recorded" ]; then
			cp -p "$INSTALL_DIR/$name" "$INSTALL_DIR/$name.bak"
			kept="${kept}    ${name} -> ${name}.bak
"
		fi
	done < "$manifest"
fi
cp -R "$tmp/stage/." "$INSTALL_DIR/"
(cd "$tmp/stage" && find . -type f | sed 's|^\./||' | sort) | while read -r name; do
	echo "$(sum "$INSTALL_DIR/$name") $name"
done > "$manifest"

if [ "$fresh" = 1 ]; then
	echo "Installed ${image} into ${INSTALL_DIR}"
else
	echo "Upgraded ${INSTALL_DIR} to ${image} (your .env is unchanged)"
fi
if [ -n "$kept" ]; then
	printf '  These had local edits; the release replaced them and your versions are kept:\n%s' "$kept"
fi

echo "Pulling ${image}…"
docker pull "$image" >/dev/null || die "could not pull ${image}
  An arm64 or amd64 build is published for each release; another
  architecture is not supported."

# --- configure -----------------------------------------------------------------
#
# Init runs in the release image, as this user (so .env is yours, mode 0600),
# with the same host-gateway entry the compose services get. The defaults point
# at this machine the way the containers will reach it. `curl … | sh` owns
# stdin, so answers are read from the terminal.

init_cmd="docker run --rm -it --user $(id -u):$(id -g) --add-host host.docker.internal:host-gateway -e HOME=/tmp -v \"${INSTALL_DIR}:/out\" ${image} python scripts/setup_wizard.py --dir /out"

if [ "$fresh" = 1 ]; then
	if ! ( : <"$TTY" ) 2>/dev/null; then
		echo
		echo "No terminal to answer the setup questions. Finish with:"
		echo "  ${init_cmd}"
		echo "  cd \"${INSTALL_DIR}\" && docker compose up -d"
		exit 0
	fi
	echo
	echo "Configuring. Every answer is checked from inside the stack's network,"
	echo "where this machine is host.docker.internal, not localhost."
	docker run --rm -it \
		--user "$(id -u):$(id -g)" \
		--add-host host.docker.internal:host-gateway \
		-e HOME=/tmp \
		-e "OLLAMA_URL=${OLLAMA_URL:-http://host.docker.internal:11434}" \
		-e "PROMETHEUS_URL=${PROMETHEUS_URL:-http://host.docker.internal:9090}" \
		-v "${INSTALL_DIR}:/out" \
		"$image" python scripts/setup_wizard.py --dir /out <"$TTY" \
		|| die "setup did not finish, so nothing was started. Run the installer again to retry."
	[ -f "$INSTALL_DIR/.env" ] || die "setup wrote no .env, so nothing was started"
fi

# --- start ---------------------------------------------------------------------

port="$(sed -n 's/^CFOP_CONSOLE_PORT=//p' "$INSTALL_DIR/.env" 2>/dev/null | tail -n 1)"
port="${port:-8083}"

if [ "$NO_START" = 1 ]; then
	echo
	echo "Not started (--no-start). When ready:"
	echo "  cd \"${INSTALL_DIR}\" && docker compose up -d"
	exit 0
fi

(cd "$INSTALL_DIR" && docker compose up -d) || die "docker compose up failed; see: cd \"${INSTALL_DIR}\" && docker compose logs"

echo
echo "CFOperator is starting: http://localhost:${port}"
echo "  Admin login: CFOP_ADMIN_USERNAME / CFOP_ADMIN_PASSWORD in ${INSTALL_DIR}/.env"
echo "  Logs:        cd \"${INSTALL_DIR}\" && docker compose logs -f"
echo "  Reconfigure: ${init_cmd}"
echo "               then: cd \"${INSTALL_DIR}\" && docker compose up -d"
echo "  Upgrade:     re-run this installer (CFOP_VERSION=x.y.z to pin)"
echo "  CLI:         curl -fsSL https://raw.githubusercontent.com/${REPO}/main/scripts/install-cfassist.sh | sh"
