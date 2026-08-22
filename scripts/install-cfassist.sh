#!/bin/sh
# Install cfassist, the CFOperator CLI, on this machine.
#
#   curl -fsSL https://raw.githubusercontent.com/aachtenberg/cfoperator/main/scripts/install-cfassist.sh | sh
#
# Written because the documented install was `gh release download`, and `gh` is
# not on a stock Raspberry Pi OS — the machine where this is most often needed.
# The hand-written curl fallback asked the operator for three things the machine
# already knows: its OS, its CPU architecture, and the current version.
#
# POSIX sh, not bash: /bin/sh on Debian and Raspberry Pi OS is dash.
#
# Knobs, all optional:
#   CFASSIST_VERSION      pin an exact release (0.10.0 or v0.10.0); default is
#                         the moving `cfassist-latest` pointer
#   CFASSIST_INSTALL_DIR  where to put the binary (default /usr/local/bin)
#   CFASSIST_OS           override uname -s detection (linux|darwin)
#   CFASSIST_ARCH         override uname -m detection (amd64|arm64|arm)
#   CFASSIST_BASE_URL     override the release host (tests point this at a stub)
#   --dry-run             print what would happen and exit
set -eu

REPO="aachtenberg/cfoperator"
BASE_URL="${CFASSIST_BASE_URL:-https://github.com/${REPO}/releases/download}"
INSTALL_DIR="${CFASSIST_INSTALL_DIR:-/usr/local/bin}"

# The default is the pointer, not a number, so this script does not go stale and
# needs no API call to find out what "current" means. GitHub's own
# /releases/latest is NOT usable here: it returns the newest release across all
# tag series in this repo (cfassist-v*, llm-gateway-v*, v*), so it would hand out
# the wrong artifact the first time a non-cfassist release is cut.
# A pin is written as 0.10.0, but people copy tags, so v0.10.0 has to work too —
# otherwise it silently becomes cfassist-vv0.10.0 and 404s.
version="${CFASSIST_VERSION:-}"
version="${version#v}"
TAG="cfassist-${version:+v}${version:-latest}"

DRY_RUN=0
for arg in "$@"; do
	case "$arg" in
		--dry-run) DRY_RUN=1 ;;
		-h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "install-cfassist: unknown argument: $arg" >&2; exit 2 ;;
	esac
done

die() { echo "install-cfassist: $*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# --- what to download --------------------------------------------------------

os="${CFASSIST_OS:-$(uname -s | tr '[:upper:]' '[:lower:]')}"
case "$os" in
	linux|darwin) ;;
	*) die "no cfassist build for OS '${os}' (linux and darwin are published)" ;;
esac

arch="${CFASSIST_ARCH:-$(uname -m)}"
case "$arch" in
	x86_64|amd64)          arch=amd64 ;;
	aarch64|arm64)         arch=arm64 ;;
	armv6l|armv7l|armv8l|arm) arch=arm ;;
	*) die "no cfassist build for architecture '${arch}' (amd64, arm64 and arm are published)" ;;
esac

# darwin/arm is not built — say so here rather than 404 at the download.
if [ "$os" = darwin ] && [ "$arch" = arm ]; then
	die "no cfassist build for darwin/arm (darwin ships amd64 and arm64)"
fi

asset="cfassist-${os}-${arch}"
url="${BASE_URL}/${TAG}/${asset}"

if [ "$DRY_RUN" = 1 ]; then
	echo "asset:   ${asset}"
	echo "url:     ${url}"
	echo "install: ${INSTALL_DIR}/cfassist"
	exit 0
fi

# --- fetch -------------------------------------------------------------------

if have curl; then
	fetch() { curl -fsSL "$1" -o "$2"; }
elif have wget; then
	fetch() { wget -qO "$2" "$1"; }
else
	die "need curl or wget"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT INT TERM

echo "Downloading cfassist (${os}/${arch}, ${TAG})…"
fetch "$url" "$tmp/cfassist" || die "could not download ${url}
  If you pinned CFASSIST_VERSION, check that release exists.
  Otherwise the cfassist-latest pointer may be missing — report it."

# --- verify ------------------------------------------------------------------
#
# A curl-pipe-sh installer that does not verify what it just downloaded is the
# one thing this must not be. Missing checksums.txt is fatal, not a warning:
# "could not check" and "checked, fine" must never look the same.

if have sha256sum; then
	sum() { sha256sum "$1" | cut -d' ' -f1; }
elif have shasum; then
	sum() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
	die "need sha256sum or shasum to verify the download"
fi

fetch "${BASE_URL}/${TAG}/checksums.txt" "$tmp/checksums.txt" \
	|| die "could not download checksums for ${TAG}; refusing to install unverified"

expected="$(grep " ${asset}\$" "$tmp/checksums.txt" | cut -d' ' -f1)"
[ -n "$expected" ] || die "${asset} is not listed in checksums.txt for ${TAG}"

actual="$(sum "$tmp/cfassist")"
[ "$actual" = "$expected" ] || die "checksum mismatch for ${asset}
  expected ${expected}
  got      ${actual}
  Nothing was installed."

# --- install -----------------------------------------------------------------

chmod +x "$tmp/cfassist"

# sudo only when the target is not already writable, and only when it exists:
# a laptop user with a writable /usr/local/bin should never be asked, and a
# machine without sudo should get a working install rather than an error.
installed=0
if [ -w "$INSTALL_DIR" ] || { [ ! -e "$INSTALL_DIR" ] && mkdir -p "$INSTALL_DIR" 2>/dev/null; }; then
	install -m 755 "$tmp/cfassist" "$INSTALL_DIR/cfassist" && installed=1
elif have sudo; then
	# `|| true`, deliberately, because set -e would otherwise abort here. sudo
	# existing is not sudo working: no tty (which `curl … | sh` can mean), a
	# denied password, or an account outside sudoers all fail, and the home
	# fallback below is written exactly for those.
	sudo install -m 755 "$tmp/cfassist" "$INSTALL_DIR/cfassist" && installed=1 || true
fi

if [ "$installed" = 0 ]; then
	INSTALL_DIR="$HOME/.local/bin"
	mkdir -p "$INSTALL_DIR"
	install -m 755 "$tmp/cfassist" "$INSTALL_DIR/cfassist"
	echo "Could not write to the system directory: installed to ${INSTALL_DIR} instead."
	case ":$PATH:" in
		*":$INSTALL_DIR:"*) ;;
		*) echo "Add it to your PATH:  export PATH=\"${INSTALL_DIR}:\$PATH\"" ;;
	esac
fi

echo "Installed $("$INSTALL_DIR/cfassist" --version) to ${INSTALL_DIR}/cfassist"
echo
echo "Next:"
echo "  cfassist                    # interactive session (writes ~/.cfassist/config.yaml on first run)"
echo "  cfassist attach <id>        # brief a session on a CFOperator investigation"
echo
echo "Set your LLM in ~/.cfassist/config.yaml. If CFOperator runs here, the session"
echo "notices it automatically; add cfoperator.token to let it read investigations."
