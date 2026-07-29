"""Map skills/*/SKILL.md files onto MCP prompts.

Each skill directory containing a SKILL.md becomes one prompt, named after
the directory (kebab-case preserved). The frontmatter description becomes
the prompt description; the markdown body is the prompt text, with an
optional `target` argument appended so hosts can point the playbook at a
specific pod/host/service. Host-neutral by construction: the files never
mention Cursor or Slack.
"""

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def _parse_skill(path):
    """Split SKILL.md into (frontmatter dict, body). Returns (None, None) on
    files without valid frontmatter — those are skipped, not fatal."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None, None
    try:
        _, fm, body = text.split("---", 2)
        meta = yaml.safe_load(fm) or {}
        return (meta, body.strip()) if isinstance(meta, dict) else (None, None)
    except (ValueError, yaml.YAMLError):
        return None, None


def _make_prompt_fn(body):
    async def playbook(target: str = "") -> str:
        if target:
            return f"{body}\n\n## Target\n\nApply this playbook to: {target}"
        return body
    return playbook


def register(mcp, client, settings):
    skills_dir = Path(settings.skills_dir)
    if not skills_dir.is_dir():
        logger.warning("skills dir %s not found; no MCP prompts registered",
                       skills_dir)
        return

    count = 0
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        meta, body = _parse_skill(skill_md)
        if not body:
            logger.warning("skipping %s: no parseable frontmatter", skill_md)
            continue
        name = str(meta.get("name") or skill_md.parent.name)
        description = str(meta.get("description") or f"CFOperator playbook: {name}")
        mcp.prompt(name=name, description=description)(_make_prompt_fn(body))
        count += 1
    logger.info("registered %d MCP prompts from %s", count, skills_dir)
