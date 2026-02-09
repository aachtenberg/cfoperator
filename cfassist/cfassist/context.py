"""Load context files from a directory into the system prompt."""

from pathlib import Path

SUPPORTED_EXTENSIONS = {".md", ".txt", ".yaml", ".yml", ".csv", ".json", ".conf", ".cfg", ".ini", ".toml"}


def load_context_directory(directory, max_chars=32000):
    """Load all supported files from directory into a single text block.

    Files are sorted alphabetically (use 01-, 02- prefixes for ordering).
    Each file is separated with a header showing the filename.

    Returns:
        (text, file_count) tuple
    """
    dir_path = Path(directory)
    if not dir_path.exists():
        return None, 0

    files = sorted(
        f for f in dir_path.rglob("*")
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    if not files:
        return None, 0

    parts = []
    total_chars = 0

    for file_path in files:
        try:
            content = file_path.read_text(errors="replace")
        except Exception:
            continue

        # Relative path from context directory for the header
        relative = file_path.relative_to(dir_path)
        header = f"--- File: {relative} ---"
        entry = f"{header}\n{content}\n"

        if total_chars + len(entry) > max_chars:
            # Add what fits, then stop
            remaining = max_chars - total_chars
            if remaining > len(header) + 100:  # only add if meaningful content fits
                parts.append(f"{header}\n{content[:remaining - len(header) - 10]}\n[truncated]\n")
            break

        parts.append(entry)
        total_chars += len(entry)

    text = "\n".join(parts)
    return text, len(parts)
