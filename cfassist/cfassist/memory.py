"""Conversation memory — save/load as JSONL files."""

import json
import re
from datetime import datetime
from pathlib import Path


def _slugify(text, max_len=40):
    """Create a filename-safe slug from text."""
    slug = re.sub(r"[^\w\s-]", "", text.lower().strip())
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug[:max_len].rstrip("-")


def save_conversation(memory_dir, messages):
    """Save a conversation to a JSONL file.

    File is named: YYYY-MM-DDTHH-MM-SS_slug-of-first-message.jsonl
    """
    if not messages:
        return None

    dir_path = Path(memory_dir)
    dir_path.mkdir(parents=True, exist_ok=True)

    # Build filename from timestamp and first user message
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    first_user = next((m["content"] for m in messages if m["role"] == "user"), "conversation")
    slug = _slugify(first_user)
    filename = f"{timestamp}_{slug}.jsonl"
    filepath = dir_path / filename

    with open(filepath, "w") as f:
        for msg in messages:
            entry = {
                "role": msg["role"],
                "content": msg.get("content", ""),
            }
            if "tool_calls" in msg:
                entry["tool_calls"] = msg["tool_calls"]
            f.write(json.dumps(entry, default=str) + "\n")

    return filepath


def load_conversation(filepath):
    """Load a conversation from a JSONL file."""
    messages = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line:
                messages.append(json.loads(line))
    return messages


def list_conversations(memory_dir, limit=20):
    """List recent conversations, newest first."""
    dir_path = Path(memory_dir)
    if not dir_path.exists():
        return []

    files = sorted(dir_path.glob("*.jsonl"), reverse=True)
    results = []
    for f in files[:limit]:
        # Extract info from filename
        name = f.stem
        parts = name.split("_", 1)
        timestamp = parts[0] if parts else ""
        slug = parts[1] if len(parts) > 1 else ""
        results.append({
            "path": str(f),
            "timestamp": timestamp,
            "slug": slug,
        })
    return results


def cleanup_old_conversations(memory_dir, max_conversations=50):
    """Remove oldest conversations if over the limit."""
    dir_path = Path(memory_dir)
    if not dir_path.exists():
        return

    files = sorted(dir_path.glob("*.jsonl"))
    if len(files) <= max_conversations:
        return

    to_remove = files[: len(files) - max_conversations]
    for f in to_remove:
        f.unlink()
