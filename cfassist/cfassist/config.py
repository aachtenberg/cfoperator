"""Configuration loading with YAML and ${VAR} environment variable expansion."""

import os
from pathlib import Path

import yaml


DEFAULT_CONFIG_DIR = Path.home() / ".cfassist"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.yaml"

DEFAULTS = {
    "llm": {
        "provider": "ollama",
        "url": "http://localhost:11434",
        "model": "llama3.2",
        "temperature": 0.7,
    },
    "context": {
        "directory": str(DEFAULT_CONFIG_DIR / "context"),
        "max_tokens": 8000,
    },
    "memory": {
        "directory": str(DEFAULT_CONFIG_DIR / "memory"),
        "max_conversations": 50,
    },
    "tools": {
        "bash": {"enabled": True, "timeout": 30},
        "read_file": {"enabled": True, "max_lines": 500},
    },
    "system_prompt": (
        "You are cfassist, a helpful SRE and systems administration assistant "
        "running in the user's terminal. You have access to tools for running "
        "shell commands and reading files. Be concise and practical. Focus on "
        "diagnosing issues, explaining errors, and suggesting fixes. When you "
        "need to check something, use your tools rather than guessing."
    ),
}


def _expand_env_vars(config):
    """Recursively expand ${VAR} references in config values."""
    if isinstance(config, dict):
        return {k: _expand_env_vars(v) for k, v in config.items()}
    elif isinstance(config, list):
        return [_expand_env_vars(item) for item in config]
    elif isinstance(config, str) and "${" in config:
        # Handle ${VAR} anywhere in string, not just full-value replacement
        import re
        def replace_var(match):
            var = match.group(1)
            return os.getenv(var, "")
        return re.sub(r"\$\{([^}]+)\}", replace_var, config)
    return config


def _deep_merge(base, override):
    """Deep merge override into base dict. Override wins on conflicts."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _expand_path(path_str):
    """Expand ~ and env vars in path strings."""
    return str(Path(os.path.expandvars(os.path.expanduser(path_str))))


def load_config(config_path=None):
    """Load config from YAML, merge with defaults, expand env vars.

    Priority: file values > defaults. Environment variables in ${VAR}
    syntax are expanded in all string values.
    """
    config = DEFAULTS.copy()

    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH

    if path.exists():
        with open(path) as f:
            file_config = yaml.safe_load(f) or {}
        file_config = _expand_env_vars(file_config)
        config = _deep_merge(DEFAULTS, file_config)

    # Expand ~ in directory paths
    for section in ("context", "memory"):
        if "directory" in config.get(section, {}):
            config[section]["directory"] = _expand_path(config[section]["directory"])

    return config


def ensure_directories(config):
    """Create config, context, and memory directories if they don't exist."""
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    for section in ("context", "memory"):
        dir_path = Path(config[section]["directory"])
        dir_path.mkdir(parents=True, exist_ok=True)

    # Write default config if none exists
    if not DEFAULT_CONFIG_PATH.exists():
        _write_default_config()


def _write_default_config():
    """Write a commented default config file."""
    DEFAULT_CONFIG_PATH.write_text("""\
# cfassist configuration
# See: https://github.com/aachtenberg/cfoperator

llm:
  provider: ollama
  url: http://localhost:11434
  model: llama3.2
  temperature: 0.7

  # OpenAI-compatible provider example:
  # provider: openai
  # url: https://api.openai.com/v1
  # model: gpt-4o
  # api_key: ${OPENAI_API_KEY}

context:
  directory: ~/.cfassist/context
  max_tokens: 8000

memory:
  directory: ~/.cfassist/memory
  max_conversations: 50

tools:
  bash:
    enabled: true
    timeout: 30
  read_file:
    enabled: true
    max_lines: 500

# Override the default system prompt:
# system_prompt: |
#   You are a custom assistant...
""")
