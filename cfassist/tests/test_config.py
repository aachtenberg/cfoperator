"""Config loading for the CFOperator block, and the defaults-mutation bug."""

import textwrap

from cfassist import config as config_mod
from cfassist.cfoperator import resolve_endpoint


def _write(tmp_path, body):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body))
    return str(path)


def test_cfoperator_block_is_read_from_the_file(tmp_path):
    path = _write(tmp_path, """
        cfoperator:
          url: http://cfop.local:8083
          token: file-token
          timeout: 12
    """)
    cfg = config_mod.load_config(path)
    assert resolve_endpoint(cfg["cfoperator"], env={}) == (
        "http://cfop.local:8083", "file-token", 12.0)


def test_env_expansion_still_applies_to_the_token(tmp_path, monkeypatch):
    """The shipped default config writes `token: ${CFOP_API_TOKEN}`; that has to
    resolve through the existing expansion rather than land as a literal."""
    monkeypatch.setenv("CFOP_API_TOKEN", "from-env")
    path = _write(tmp_path, """
        cfoperator:
          token: ${CFOP_API_TOKEN}
    """)
    cfg = config_mod.load_config(path)
    assert cfg["cfoperator"]["token"] == "from-env"


def test_a_file_without_a_cfoperator_block_still_gets_the_defaults(tmp_path):
    path = _write(tmp_path, """
        llm:
          model: gemma4:26b
    """)
    cfg = config_mod.load_config(path)
    assert cfg["cfoperator"] == {"url": "", "token": "", "timeout": 30}
    assert cfg["llm"]["model"] == "gemma4:26b"
    assert cfg["llm"]["provider"] == "ollama"      # default survived the merge


def test_loading_a_config_does_not_mutate_the_module_defaults(tmp_path):
    """DEFAULTS was shared by reference through a shallow .copy(), so
    `config["llm"]["model"] = ...` in the CLI rewrote the default for the rest
    of the process — and, with a new nested section added, would have leaked a
    token between loads. Mutation check: restore `.copy()` and this goes red.
    """
    path = _write(tmp_path, "llm:\n  model: from-file\n")
    cfg = config_mod.load_config(path)
    cfg["llm"]["model"] = "mutated"
    cfg["cfoperator"]["token"] = "leaked-secret"

    assert config_mod.DEFAULTS["llm"]["model"] == "llama3.2"
    assert config_mod.DEFAULTS["cfoperator"]["token"] == ""
    assert config_mod.load_config(path)["cfoperator"]["token"] == ""


def test_default_config_template_documents_the_cfoperator_block():
    """The written-on-first-run config is the only docs most users read."""
    import inspect
    source = inspect.getsource(config_mod._write_default_config)
    assert "cfoperator:" in source
    assert "CFOP_API_TOKEN" in source
