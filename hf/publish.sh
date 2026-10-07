#!/usr/bin/env bash
# Publish the v5 triage fine-tune to Hugging Face (CFOP-274).
#
# Run on the host with the NAS mounted (ubuntu-llm-01). Nothing here needs
# the GPU. The upload is gated twice: the training data is scanned for
# anything secret-shaped, and every artifact that leaves the machine must
# match the sha256 manifest in hf/v5.sha256. The script refuses to continue
# on either.
#
#   HF_REPO=<user>/cfop-triage-ministral3-14b-v5 hf/publish.sh              # scan, stage, verify, upload
#   HF_REPO=<user>/cfop-triage-ministral3-14b-v5 hf/publish.sh --dry-run    # everything except the upload
#   HF_REPO=<user>/cfop-triage-ministral3-14b-v5 hf/publish.sh --stage-only # scan and stage only; nothing verified, nothing uploaded
#
# Environment:
#   HF_REPO       required. The model repo id to create or update.
#   SRC_DIR       directory holding the two v5 GGUFs.
#                 default /mnt/nas-backup/unsloth/cfoperator-v6/cfop-triage-v5-gguf
#   DATASET_DIR   directory holding triage_train.jsonl and triage_val.jsonl (the
#                 v5 set the weights were trained on). default /mnt/nas-backup/unsloth/cfoperator-v6
#   ADAPTER_DIR   optional. A directory with adapter_model.safetensors and
#                 adapter_config.json; published under adapter/ when set.
#   MANIFEST      sha256sum-format file naming every artifact that may be
#                 uploaded (GGUFs, and the adapter files when ADAPTER_DIR is
#                 set). default hf/v5.sha256. Generate it ONCE on the NAS host
#                 from the gated files and commit it:
#                   (cd "$SRC_DIR" && sha256sum *.gguf) > hf/v5.sha256
#                   (cd "$ADAPTER_DIR" && sha256sum adapter_model.safetensors adapter_config.json) >> hf/v5.sha256
#   STAGE_DIR     where the small files are assembled. default: a fresh mktemp
#                 dir. A pre-existing non-empty directory is refused: whatever
#                 is in the stage gets uploaded, so leftovers from an earlier
#                 run would ship too.
#
# Authentication is whatever `hf auth login` left behind, or HF_TOKEN in the
# environment. The token is never written anywhere by this script.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

SRC_DIR="${SRC_DIR:-/mnt/nas-backup/unsloth/cfoperator-v6/cfop-triage-v5-gguf}"
DATASET_DIR="${DATASET_DIR:-/mnt/nas-backup/unsloth/cfoperator-v6}"
ADAPTER_DIR="${ADAPTER_DIR:-}"
MANIFEST="${MANIFEST:-$HERE/v5.sha256}"
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

MODELFILE_SRC="$REPO_ROOT/benchmarks/Modelfile.cfop-triage-v5"
CARD_SRC="$HERE/README.md"

log() { printf '%s\n' "$*" >&2; }

# ---- 1. gate: the training data must be clean -----------------------------
log "== scanning training data in $DATASET_DIR"
python3 "$HERE/scan_dataset.py" "$DATASET_DIR/triage_train.jsonl" "$DATASET_DIR/triage_val.jsonl"

# ---- 2. stage the small files ---------------------------------------------
if [ -z "${STAGE_DIR:-}" ]; then
  STAGE_DIR="$(mktemp -d -t cfop-hf-stage.XXXXXX)"
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

if [ "$MODE" = stage ]; then
  log "== --stage-only: stopping before the artifact check"
  exit 0
fi

# ---- 3. gate: every artifact must match the manifest ----------------------
# The manifest is computed once from the gated files on the NAS host and
# committed. A file that is not in it, or does not match it, does not ship.
# (Size alone is not a check: the v5 export is byte-for-byte the same size as
# v1's, same base and same quant, so only the hash tells them apart.)
[ -f "$MANIFEST" ] || {
  log "manifest $MANIFEST missing. Generate it on the NAS host from the gated files and commit it:"
  log "  (cd \"$SRC_DIR\" && sha256sum *.gguf) > $MANIFEST"
  exit 1
}
verify() {  # verify <dir> <file>: the file's sha256 must appear in the manifest under that name
  local dir="$1" f="$2" want have
  want=$(awk -v f="$f" '$2 == f || $2 == "*" f {print $1}' "$MANIFEST")
  [ -n "$want" ] || { log "$f is not in $MANIFEST: not a gated artifact"; exit 1; }
  [ -f "$dir/$f" ] || { log "missing $dir/$f"; exit 1; }
  have=$(sha256sum "$dir/$f" | awk '{print $1}')
  [ "$have" = "$want" ] || { log "$f sha256 $have does not match manifest $want: not the gated v5 export"; exit 1; }
  log "   ok  $f  $have"
}
log "== verifying artifacts against $MANIFEST"
verify "$SRC_DIR" "$Q4"
verify "$SRC_DIR" "$Q8"
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
hf upload "$HF_REPO" "$STAGE_DIR" . --repo-type model --commit-message "cfop-triage-ministral3 v5: card, Modelfile, adapter (CFOP-274)"

log "== uploading GGUFs (large; resumable)"
hf upload "$HF_REPO" "$SRC_DIR/$Q4" "$Q4" --repo-type model --commit-message "v5 Q4_K_M, the deployed quant (CFOP-274)"
hf upload "$HF_REPO" "$SRC_DIR/$Q8" "$Q8" --repo-type model --commit-message "v5 Q8_0, reference quant (CFOP-274)"

log "== done: https://huggingface.co/$HF_REPO"
