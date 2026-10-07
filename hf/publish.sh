#!/usr/bin/env bash
# Publish the v5 triage fine-tune to Hugging Face (CFOP-274).
#
# Run on the host with the NAS mounted (ubuntu-llm-01). Nothing here needs
# the GPU. The upload is gated: the training data is scanned for anything
# secret-shaped first, and the script refuses to continue on a finding.
#
#   HF_REPO=<user>/cfop-triage-ministral3-14b-v5 hf/publish.sh            # stage, scan, upload
#   HF_REPO=<user>/cfop-triage-ministral3-14b-v5 hf/publish.sh --stage-only  # stage and scan, no network
#
# Environment:
#   HF_REPO       required. The model repo id to create or update.
#   SRC_DIR       directory holding the two v5 GGUFs.
#                 default /mnt/nas-backup/unsloth/cfoperator-v6/cfop-triage-v5-gguf
#   DATASET_DIR   directory holding triage_train.jsonl and triage_val.jsonl (the
#                 v5 set the weights were trained on). default /mnt/nas-backup/unsloth/cfoperator-v6
#   ADAPTER_DIR   optional. A directory with adapter_model.safetensors and
#                 adapter_config.json; published under adapter/ when set.
#   STAGE_DIR     where the small files are assembled. default: a fresh mktemp dir.
#
# Authentication is whatever `hf auth login` left behind, or HF_TOKEN in the
# environment. The token is never written anywhere by this script.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

SRC_DIR="${SRC_DIR:-/mnt/nas-backup/unsloth/cfoperator-v6/cfop-triage-v5-gguf}"
DATASET_DIR="${DATASET_DIR:-/mnt/nas-backup/unsloth/cfoperator-v6}"
ADAPTER_DIR="${ADAPTER_DIR:-}"
STAGE_DIR="${STAGE_DIR:-$(mktemp -d -t cfop-hf-stage.XXXXXX)}"
STAGE_ONLY=0
[ "${1:-}" = "--stage-only" ] && STAGE_ONLY=1

: "${HF_REPO:?set HF_REPO to <user>/<repo>}"

Q4="ministral-3-14b-instruct-2512.Q4_K_M.gguf"
Q8="ministral-3-14b-instruct-2512.Q8_0.gguf"
# Byte sizes recorded in docs/triage-fine-tune.md for the v5 export. A file of
# another size is a different export, and the gate results do not apply to it.
Q4_BYTES=8239067360
Q8_BYTES=14359310560

MODELFILE_SRC="$REPO_ROOT/benchmarks/Modelfile.cfop-triage-v5"
CARD_SRC="$HERE/README.md"

log() { printf '%s\n' "$*" >&2; }

# ---- 1. gate: the training data must be clean -----------------------------
log "== scanning training data in $DATASET_DIR"
python3 "$HERE/scan_dataset.py" "$DATASET_DIR/triage_train.jsonl" "$DATASET_DIR/triage_val.jsonl"

# ---- 2. stage the small files ---------------------------------------------
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

if [ "$STAGE_ONLY" = 1 ]; then
  log "== --stage-only: stopping before any network access"
  exit 0
fi

# ---- 3. the GGUFs must be the gated export --------------------------------
for pair in "$Q4:$Q4_BYTES" "$Q8:$Q8_BYTES"; do
  f="${pair%%:*}"; want="${pair##*:}"
  [ -f "$SRC_DIR/$f" ] || { log "missing $SRC_DIR/$f"; exit 1; }
  have=$(stat -c %s "$SRC_DIR/$f")
  [ "$have" = "$want" ] || { log "$f is $have bytes, expected $want: not the gated v5 export"; exit 1; }
done

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
