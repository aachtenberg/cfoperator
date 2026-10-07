"""Secret-shaped values in tool results are replaced before a model or the
transcript sees them (CFOP-272).

Fixtures are shaped like what the read-only tools really return — ``docker
inspect``, ``env``, ``kubectl get secret -o yaml``, a key file, a connection
string — because that is what an investigation legitimately runs. The
false-positive pins matter as much: ``tokens_total`` and ``token_count`` are
metric names an investigation reads all day, and a redactor that eats them
makes the model blind in exchange for nothing.

``cfshared`` is not one of CI's package runs, so this lives in ``tests/``,
which runs with the repo root on the path.
"""

from repo_paths import REPO_ROOT  # noqa: F401  (tests/ convention; root on sys.path)
import json

import pytest

from cfshared.tool_args import PLACEHOLDER, redact_tool_args, redact_tool_result


def _redacted(value):
    out, count = redact_tool_result(value)
    return out, count


# --- text shapes an ssh/kubectl result carries ---------------------------------


def test_env_and_dotenv_lines_keep_the_key_and_lose_the_value():
    text = ("POSTGRES_PASSWORD=hunter2\nOLLAMA_URL=http://ollama:11434\n"
            "GROQ_API_KEY=gsk_abcdefghijklmnopqrstuvwxyz\nexport GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789\n")
    out, count = _redacted(text)
    assert "hunter2" not in out and "gsk_abcdef" not in out and "ghp_abcdef" not in out
    assert f"POSTGRES_PASSWORD={PLACEHOLDER}" in out, "the key stays: the model needs to know it is set"
    assert "OLLAMA_URL=http://ollama:11434" in out, "a URL is not a secret"
    assert count >= 3


def test_docker_inspect_env_array_is_scrubbed_structurally_and_in_text():
    inspect = [{"Config": {"Env": ["POSTGRES_PASSWORD=hunter2", "PATH=/usr/bin", "ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnopqrstuvwxyz"]},
                "Name": "/cfoperator-agent-1"}]
    out, _ = _redacted(inspect)
    env = out[0]["Config"]["Env"]
    assert env[0] == f"POSTGRES_PASSWORD={PLACEHOLDER}" and env[1] == "PATH=/usr/bin"
    assert "sk-ant-abc" not in env[2] and env[2].startswith("ANTHROPIC_API_KEY=")
    # The same text as the CLI prints it, rather than parsed.
    out_text, _ = _redacted(json.dumps(inspect))
    assert "hunter2" not in out_text and "sk-ant-abc" not in out_text
    assert '"PATH=/usr/bin"' in out_text, "quoting around a non-secret element is untouched"


def test_kubectl_secret_yaml_loses_every_data_value_whatever_the_key():
    yaml_text = ("apiVersion: v1\nkind: Secret\nmetadata:\n  name: cfoperator-db\n  namespace: apps\n"
                 "type: Opaque\ndata:\n  postgres-password: aHVudGVyMg==\n  some-random-name: c2VjcmV0\n"
                 "stringData:\n  url: postgresql://cfop:hunter2@db:5432/kb\n")
    out, count = _redacted(yaml_text)
    assert "aHVudGVyMg==" not in out and "c2VjcmV0" not in out and "hunter2" not in out
    assert f"postgres-password: {PLACEHOLDER}" in out and f"some-random-name: {PLACEHOLDER}" in out
    assert "name: cfoperator-db" in out and "namespace: apps" in out, "metadata is not secret"
    assert count >= 3


def test_kubectl_secret_json_is_scrubbed_structurally():
    secret = {"kind": "Secret", "metadata": {"name": "x"}, "data": {"anything": "aHVudGVyMg=="},
              "stringData": {"url": "postgresql://u:p@h/db"}}
    out, _ = _redacted(secret)
    assert out["data"]["anything"] == PLACEHOLDER and out["stringData"]["url"] == PLACEHOLDER
    assert out["metadata"]["name"] == "x"


def test_a_configmap_with_a_data_block_is_not_a_secret():
    yaml_text = "kind: ConfigMap\ndata:\n  prometheus_url: http://prom:9090\n  loki_url: http://loki:3100\n"
    out, count = _redacted(yaml_text)
    assert out == yaml_text and count == 0


def test_pem_private_key_body_is_replaced():
    pem = ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
           "QyNTUxOQAAACB\n-----END OPENSSH PRIVATE KEY-----\n")
    out, count = _redacted(f"$ cat ~/.ssh/id_ed25519\n{pem}")
    assert "b3BlbnNzaC1rZXktdjEA" not in out
    assert "-----BEGIN OPENSSH PRIVATE KEY-----" in out and "-----END OPENSSH PRIVATE KEY-----" in out
    assert count == 1


def test_connection_string_password_and_webhook_paths_are_hidden():
    text = ("DATABASE_URL=postgresql://cfop:hunter2@db:5432/kb\n"
            "slack: https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXXXXXXXXXX\n"
            "discord: https://discord.com/api/webhooks/1234567890/abcdefGHIJKL\n")
    out, _ = _redacted(text)
    assert "hunter2" not in out and "XXXXXXXX" not in out and "abcdefGHIJKL" not in out
    assert "https://hooks.slack.com/services/" in out and "db:5432/kb" in out


def test_bearer_headers_and_known_token_prefixes_keep_only_the_prefix():
    text = ("curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc'\n"
            "x-api-key: xai-abcdefghijklmnopqrstuvwxyz0123456789\n"
            "token cfop_bZqLYUXNabcdefghij minted\n"
            "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n")
    out, _ = _redacted(text)
    assert "eyJhbGci" not in out and "xai-abcdef" not in out and "bZqLYUXN" not in out and "IOSFODNN7" not in out
    assert f"cfop_{PLACEHOLDER}" in out, "the prefix says what kind of credential it was"
    assert f"Bearer {PLACEHOLDER}" in out


def test_yaml_and_json_quoted_values_keep_their_quotes():
    out, _ = _redacted('{"api_key": "sk-abcdefghijklmnopqrstuvwxyz", "model": "gemma4:26b"}')
    assert out == f'{{"api_key": "{PLACEHOLDER}", "model": "gemma4:26b"}}'
    out, _ = _redacted("llm:\n  api_key: 'sk-abcdefghijklmnopqrstuvwxyz'\n  model: gemma4:26b\n")
    assert f"api_key: '{PLACEHOLDER}'" in out and "model: gemma4:26b" in out


# --- what must survive ------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "cfoperator_llm_tokens_total{provider=\"ollama\",type=\"input\"} 123456",
    "token_count: 512\nprompt_tokens: 1200",
    "Oct 07 12:00:01 web sshd[1]: password_reset requested for user alice",
    "TOKEN_FILE=/run/secrets/token\nSECRET_NAME=cfoperator-db",
    "the secret sauce is caching; authorization header absent",
    "NAME        READY   STATUS    RESTARTS\nsecret-rotator-0   1/1   Running   0",
    "POSTGRES_PASSWORD=***",
])
def test_metric_names_log_lines_and_prose_are_untouched(text):
    out, count = _redacted(text)
    assert out == text, out
    assert count == 0


def test_structures_keep_shape_and_ids_the_loop_reads():
    """``find_learnings`` ids and ``github_create_pr`` URLs travel in the result;
    the redacted copy must still carry them, since that is what the agent
    reads after redaction."""
    result = [{"id": 42, "title": "Loki OOM", "token": "abc"}, {"id": 43, "html_url": "https://github.com/x/y/pull/9"}]
    out, count = _redacted(result)
    assert [r["id"] for r in out] == [42, 43] and out[1]["html_url"].endswith("/pull/9")
    assert out[0]["token"] == PLACEHOLDER and count == 1


def test_non_string_scalars_and_empty_values_pass_through():
    out, count = _redacted({"password": "", "token": None, "count": 3, "ok": True, "ratio": 0.5})
    assert out == {"password": "", "token": None, "count": 3, "ok": True, "ratio": 0.5}
    assert count == 0


def test_args_and_results_agree_on_which_keys_are_secret():
    """Widening the key list for results widened it for arguments too, on
    purpose: a key that is *** in the transcript's tool_call line must be ***
    in its tool_result line."""
    for key in ("password", "passwd", "client_secret", "aws_secret_access_key", "x_api_key"):
        assert redact_tool_args({key: "v"}) == {key: PLACEHOLDER}, key
        assert redact_tool_result({key: "v"})[0] == {key: PLACEHOLDER}, key
    assert redact_tool_args({"token_count": 5}) == {"token_count": 5}
