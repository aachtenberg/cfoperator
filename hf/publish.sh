#!/usr/bin/env bash
# Publish the triage fine-tune to Hugging Face (CFOP-274, CFOP-277).
#
# Run on the host with the NAS mounted (ubuntu-llm-01). Nothing here needs
# the GPU. The upload is gated twice: the training data is scanned for
# anything secret-shaped, and every artifact that leaves the machine must
# match the sha256 manifest in hf/$VERSION.sha256. The script refuses to continue
# on either.
#
#   HF_REPO=<user>/cfop-triage-ministral3-14b-v6 hf/publish.sh              # scan, stage, verify, upload
#   HF_REPO=<user>/cfop-triage-ministral3-14b-v6 hf/publish.sh --dry-run    # everything except the upload
#   HF_REPO=<user>/cfop-triage-ministral3-14b-v6 hf/publish.sh --stage-only # scan and stage only; nothing verified, nothing uploaded
#
# Environment:
#   HF_REPO       required. The model repo id to create or update.
#   VERSION       which generation ships, vN. default v6. Picks the Modelfile
#                 (benchmarks/Modelfile.cfop-triage-$VERSION), the manifest
#                 (hf/$VERSION.sha256) and the NAS folder defaults below.
#   SRC_DIR       directory holding the two GGUFs.
#                 default /mnt/nas-backup/unsloth/cfoperator-v<N+1>/cfop-triage-$VERSION-gguf
#   DATASET_DIR   directory holding triage_train.jsonl and triage_val.jsonl (the
#                 set the weights were trained on). default /mnt/nas-backup/unsloth/cfoperator-v<N+1>
#   ADAPTER_DIR   optional. A directory with adapter_model.safetensors and
#                 adapter_config.json; published under adapter/ when set.
#   MANIFEST      sha256sum-format file naming the dataset the weights were
#                 trained on and every artifact that may be uploaded (GGUFs,
#                 and the adapter files when ADAPTER_DIR is set). default
#                 hf/$VERSION.sha256. The dataset lines are committed: they pin WHICH
#                 train/val set the scan gate is passing judgement on. The
#                 artifact lines are appended ONCE on the NAS host from the
#                 gated files and committed:
#                   (cd "$SRC_DIR" && sha256sum *.gguf) >> hf/$VERSION.sha256
#                   (cd "$ADAPTER_DIR" && sha256sum adapter_model.safetensors adapter_config.json) >> hf/$VERSION.sha256
#                 That glob hashes every GGUF in the folder (an mmproj side
#                 file, say); harmless, since only the two names above are
#                 ever looked up, but trim the file if you want it exact.
#   STAGE_DIR     where the small files are assembled. default: a fresh mktemp
#                 dir, removed when the run ends. A pre-existing non-empty
#                 directory is refused: whatever is in the stage gets uploaded,
#                 so leftovers from an earlier run would ship too. A caller-
#                 supplied directory is kept.
#   GGUF_STAGE_DIR where the GGUFs are copied before they are hashed and
#                 uploaded (~23 GB for a 14B Q4+Q8 pair). default: a fresh 0700 mktemp dir
#                 under ${TMPDIR:-/var/tmp}. The copy is what gets verified and
#                 what gets uploaded, so a NAS file changing between the two
#                 cannot ship unverified bytes. The copies are removed when the
#                 run ends, however it ends; a caller-supplied directory is kept.
#
# Authentication is whatever `hf auth login` left behind, or HF_TOKEN in the
# environment. The token is never written anywhere by this script.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

# Which generation ships. The NAS folder numbering runs one ahead of the data
# generation (cfoperator-v7/ holds the v6 data and export), so the defaults
# below are derived from VERSION rather than typed twice.
VERSION="${VERSION:-v6}"
[[ "$VERSION" =~ ^v[0-9]+$ ]] || { echo "VERSION '$VERSION' is not vN" >&2; exit 2; }
NAS_FOLDER="cfoperator-v$(( ${VERSION#v} + 1 ))"
SRC_DIR="${SRC_DIR:-/mnt/nas-backup/unsloth/$NAS_FOLDER/cfop-triage-$VERSION-gguf}"
DATASET_DIR="${DATASET_DIR:-/mnt/nas-backup/unsloth/$NAS_FOLDER}"
ADAPTER_DIR="${ADAPTER_DIR:-}"
MANIFEST="${MANIFEST:-$HERE/$VERSION.sha256}"
GGUF_STAGE_DIR="${GGUF_STAGE_DIR:-}"
MODE=upload
case "${1:-}" in
  "") ;;
  --stage-only) MODE=stage ;;
  --dry-run) MODE=dry ;;
  *) echo "unknown argument: $1 (expected --stage-only or --dry-run)" >&2; exit 2 ;;
esac

: "${HF_REPO:?set HF_REPO to <user>/<repo>}"
# A Hub repo id is <namespace>/<name>, each a run of [A-Za-z0-9._-]. Anything
# else is a typo, and the id is interpolated into sed below.
[[ "$HF_REPO" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || { echo "HF_REPO '$HF_REPO' is not <user>/<repo>" >&2; exit 2; }

Q4="ministral-3-14b-instruct-2512.Q4_K_M.gguf"
Q8="ministral-3-14b-instruct-2512.Q8_0.gguf"

MODELFILE_SRC="$REPO_ROOT/benchmarks/Modelfile.cfop-triage-$VERSION"
[ -f "$MODELFILE_SRC" ] || { echo "no gated Modelfile for $VERSION at $MODELFILE_SRC" >&2; exit 2; }
CARD_SRC="$HERE/README.md"

log() { printf '%s\n' "$*" >&2; }

verify() {  # verify <dir> <file>: the file's sha256 must appear in the manifest under that name
  local dir="$1" f="$2" want have
  want=$(awk -v f="$f" '$2 == f || $2 == "*" f {print $1}' "$MANIFEST")
  [ -n "$want" ] || { log "$f is not in $MANIFEST: not a gated artifact"; exit 1; }
  [ -f "$dir/$f" ] || { log "missing $dir/$f"; exit 1; }
  have=$(sha256sum "$dir/$f" | awk '{print $1}')
  [ "$have" = "$want" ] || { log "$f sha256 $have does not match manifest $want: not the gated $VERSION export"; exit 1; }
  log "   ok  $f  $have"
}
[ -f "$MANIFEST" ] || { log "manifest $MANIFEST missing (the dataset lines are committed with the repo; see the header)"; exit 1; }

# ---- 1. gate: the training data must be the pinned set, and clean ----------
# A clean scan of the wrong files proves nothing, so the files are pinned
# first: these must be the train/val set the weights were trained on.
log "== verifying dataset identity against $MANIFEST"
verify "$DATASET_DIR" triage_train.jsonl
verify "$DATASET_DIR" triage_val.jsonl
log "== scanning training data in $DATASET_DIR"
python3 "$HERE/scan_dataset.py" "$DATASET_DIR/triage_train.jsonl" "$DATASET_DIR/triage_val.jsonl"

# ---- 2. stage the small files ---------------------------------------------
# Directories this run created with mktemp are removed when it ends, however
# it ends. A caller-supplied directory is left in place (only the copies the
# script put there are removed), so pointing GGUF_STAGE_DIR at a mount point
# cannot remove the mount point.
CLEANUP=()
trap 'for d in ${CLEANUP[@]+"${CLEANUP[@]}"}; do rm -rf "$d"; done' EXIT  # empty-array-safe under set -u on bash < 4.4
if [ -z "${STAGE_DIR:-}" ]; then
  STAGE_DIR="$(mktemp -d -t cfop-hf-stage.XXXXXX)"; CLEANUP+=("$STAGE_DIR")
elif [ -e "$STAGE_DIR" ] && [ -n "$(ls -A "$STAGE_DIR" 2>/dev/null)" ]; then
  log "STAGE_DIR $STAGE_DIR is not empty: refusing, everything in the stage gets uploaded"
  exit 1
fi
log "== staging into $STAGE_DIR"
mkdir -p "$STAGE_DIR"

# The card, with REPO_ID filled in so the ollama and hf commands paste.
sed "s#REPO_ID#${HF_REPO}#g" "$CARD_SRC" > "$STAGE_DIR/README.md"

# The Modelfile is the gated one from benchmarks/, byte for byte, except that
# FROM points at the sibling GGUF instead of the NAS path. Anything else
# (TEMPLATE, PARSER, PARAMETER) must not drift from what was gated.
sed "s#^FROM .*#FROM ./${Q4}#" "$MODELFILE_SRC" > "$STAGE_DIR/Modelfile"

if [ -n "$ADAPTER_DIR" ]; then
  for f in adapter_model.safetensors adapter_config.json; do
    [ -f "$ADAPTER_DIR/$f" ] || { log "ADAPTER_DIR set but $f missing"; exit 1; }
  done
  mkdir -p "$STAGE_DIR/adapter"
  cp "$ADAPTER_DIR/adapter_model.safetensors" "$ADAPTER_DIR/adapter_config.json" "$STAGE_DIR/adapter/"
else
  log "   no ADAPTER_DIR: the adapter will not be published (the card says 'when present')"
fi

log "   staged:"; (cd "$STAGE_DIR" && find . -type f | sort | sed 's/^/     /') >&2

# The card and the Modelfile are text that ships verbatim, so they go through
# the same patterns as the dataset. The Modelfile is clean today; this is for
# the day someone pastes a log line into its header comment.
log "== scanning staged text files"
python3 "$HERE/scan_dataset.py" "$STAGE_DIR/README.md" "$STAGE_DIR/Modelfile"

if [ "$MODE" = stage ]; then
  log "== --stage-only: stopping before the artifact check"
  exit 0
fi

# ---- 3. gate: every artifact must match the manifest ----------------------
# The manifest is computed once from the gated files on the NAS host and
# committed. A file that is not in it, or does not match it, does not ship.
# (Size alone is not a check: every generation's export is byte-for-byte the
# same size, same base and same quant, so only the hash tells them apart.)
grep -q 'gguf$' "$MANIFEST" || {
  log "$MANIFEST has no GGUF lines yet. Append them on the NAS host from the gated files and commit:"
  log "  (cd \"$SRC_DIR\" && sha256sum *.gguf) >> $MANIFEST"
  exit 1
}
# The GGUFs are copied to a private local directory first and the COPY is
# what gets hashed and uploaded. Hashing the NAS file and then letting
# `hf upload` reopen the NAS path would leave a window in which the file
# could change after it was verified.
for f in "$Q4" "$Q8"; do [ -f "$SRC_DIR/$f" ] || { log "missing $SRC_DIR/$f"; exit 1; }; done
need=$(( $(stat -c %s "$SRC_DIR/$Q4") + $(stat -c %s "$SRC_DIR/$Q8") ))
if [ -z "$GGUF_STAGE_DIR" ]; then
  GGUF_STAGE_DIR="$(mktemp -d -p "${TMPDIR:-/var/tmp}" cfop-hf-gguf.XXXXXX)"; CLEANUP+=("$GGUF_STAGE_DIR")
elif [ -e "$GGUF_STAGE_DIR" ] && [ -n "$(ls -A "$GGUF_STAGE_DIR" 2>/dev/null)" ]; then
  log "GGUF_STAGE_DIR $GGUF_STAGE_DIR is not empty: refusing"
  exit 1
fi
mkdir -p "$GGUF_STAGE_DIR" && chmod 700 "$GGUF_STAGE_DIR"
# The copies never outlive the run, whichever directory they were put in.
CLEANUP+=("$GGUF_STAGE_DIR/$Q4" "$GGUF_STAGE_DIR/$Q8")
avail=$(df --output=avail -B1 "$GGUF_STAGE_DIR" | tail -1)
[ "$avail" -gt "$need" ] || { log "$GGUF_STAGE_DIR has $avail bytes free, need $need for the GGUF copies (set GGUF_STAGE_DIR)"; exit 1; }
log "== copying GGUFs to $GGUF_STAGE_DIR"
cp "$SRC_DIR/$Q4" "$SRC_DIR/$Q8" "$GGUF_STAGE_DIR/"

log "== verifying artifacts against $MANIFEST"
verify "$GGUF_STAGE_DIR" "$Q4"
verify "$GGUF_STAGE_DIR" "$Q8"
if [ -n "$ADAPTER_DIR" ]; then
  verify "$STAGE_DIR/adapter" adapter_model.safetensors
  verify "$STAGE_DIR/adapter" adapter_config.json
fi

if [ "$MODE" = dry ]; then
  log "== --dry-run: everything checked, nothing uploaded"
  exit 0
fi

# ---- 4. upload -------------------------------------------------------------
command -v hf >/dev/null || { log "hf CLI not found (pip install -U huggingface_hub)"; exit 1; }
hf auth whoami >/dev/null 2>&1 || { log "not logged in to Hugging Face: run 'hf auth login' or export HF_TOKEN"; exit 1; }

log "== creating $HF_REPO (no-op if it exists)"
hf repo create "$HF_REPO" --repo-type model --exist-ok >/dev/null

log "== uploading small files"
hf upload "$HF_REPO" "$STAGE_DIR" . --repo-type model --commit-message "cfop-triage-ministral3 $VERSION: card, Modelfile, adapter (CFOP-274)"

log "== uploading GGUFs from $GGUF_STAGE_DIR (large; resumable)"
hf upload "$HF_REPO" "$GGUF_STAGE_DIR/$Q4" "$Q4" --repo-type model --commit-message "$VERSION Q4_K_M, the deployed quant (CFOP-274)"
hf upload "$HF_REPO" "$GGUF_STAGE_DIR/$Q8" "$Q8" --repo-type model --commit-message "$VERSION Q8_0, reference quant (CFOP-274)"

log "== done: https://huggingface.co/$HF_REPO"
