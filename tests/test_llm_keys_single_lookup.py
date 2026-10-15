"""One lookup for hosted-provider keys (CFOP-111).

Thirteen sites in agent/agent.py and web_server.py read a key straight from
the env, so a key set from the console would have reached some paths and not
others -- the judge eligible, the chain not, or the reverse. Every read now
goes through agent/llm_keys.py; this fails on the next ``getenv`` of a key
anywhere else. Parsed from source, like tests/test_rbac_covers_tools.py.
"""

import re

from repo_paths import REPO_ROOT

SOURCES = ('agent/agent.py', 'web_server.py', 'agent/reverify.py', 'agent/remediation.py')
KEY_READ = re.compile(r"getenv\(\s*(?:['\"][A-Z_]*API_KEY['\"]|[^)]*key_env)")


def _key_reads(text):
    return [i for i, line in enumerate(text.splitlines(), 1) if KEY_READ.search(line)]


def test_no_key_is_read_from_the_env_outside_the_lookup():
    offenders = {}
    for rel in SOURCES:
        path = REPO_ROOT / rel
        if not path.exists():
            continue
        lines = _key_reads(path.read_text(encoding='utf-8'))
        if lines:
            offenders[rel] = lines
    assert offenders == {}, (
        f"API keys read from the env outside agent/llm_keys.py: {offenders} -- "
        "go through provider_api_key() / llm_keys.api_key() so console-set keys apply")


def test_the_scan_is_not_vacuous():
    """The pattern matches the two shapes the thirteen sites had, and not an
    unrelated env read. The lookup itself reads the env through a variable
    (``os.getenv(env, '')``), which is why it is the one place allowed to."""
    assert _key_reads("api_key = os.getenv('ANTHROPIC_API_KEY', '')")
    assert _key_reads("return os.getenv(cfg['key_env'], '')")
    assert _key_reads("if fb_type in OPENAI_COMPAT_PROVIDERS and not os.getenv(OPENAI_COMPAT_PROVIDERS[fb_type]['key_env']):")
    assert not _key_reads("url = os.getenv('OLLAMA_URL', '')")
    lookup = (REPO_ROOT / 'agent' / 'llm_keys.py').read_text(encoding='utf-8')
    assert "os.getenv(env, '')" in lookup, 'the lookup no longer reads the env at all'
