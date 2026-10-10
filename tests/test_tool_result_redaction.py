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
    """The redacted copy and the count, as a pair."""
    out, count = redact_tool_result(value)
    return out, count


# --- text shapes an ssh/kubectl result carries ---------------------------------


def test_env_and_dotenv_lines_keep_the_key_and_lose_the_value():
    """`env`, `.env`, `export`: the key says it is set, the value is gone."""
    text = ("POSTGRES_PASSWORD=hunter2\nOLLAMA_URL=http://ollama:11434\n"
            "GROQ_API_KEY=gsk_abcdefghijklmnopqrstuvwxyz\nexport GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789\n")
    out, count = _redacted(text)
    assert "hunter2" not in out and "gsk_abcdef" not in out and "ghp_abcdef" not in out
    assert f"POSTGRES_PASSWORD={PLACEHOLDER}" in out, "the key stays: the model needs to know it is set"
    assert "OLLAMA_URL=http://ollama:11434" in out, "a URL is not a secret"
    assert count >= 3


def test_docker_inspect_env_array_is_scrubbed_structurally_and_in_text():
    """`docker inspect` Env, both parsed and as the CLI prints it."""
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
    """`kubectl get secret -o yaml`: arbitrary key names under data:/stringData:."""
    yaml_text = ("apiVersion: v1\nkind: Secret\nmetadata:\n  name: cfoperator-db\n  namespace: apps\n"
                 "type: Opaque\ndata:\n  postgres-password: aHVudGVyMg==\n  some-random-name: c2VjcmV0\n"
                 "stringData:\n  url: postgresql://cfop:hunter2@db:5432/kb\n")
    out, count = _redacted(yaml_text)
    assert "aHVudGVyMg==" not in out and "c2VjcmV0" not in out and "hunter2" not in out
    assert f"postgres-password: {PLACEHOLDER}" in out and f"some-random-name: {PLACEHOLDER}" in out
    assert "name: cfoperator-db" in out and "namespace: apps" in out, "metadata is not secret"
    assert count >= 3


def test_kubectl_secret_json_is_scrubbed_structurally():
    """A parsed Secret: every data/stringData value goes, metadata stays."""
    secret = {"kind": "Secret", "metadata": {"name": "x"}, "data": {"anything": "aHVudGVyMg=="},
              "stringData": {"url": "postgresql://u:p@h/db"}}
    out, _ = _redacted(secret)
    assert out["data"]["anything"] == PLACEHOLDER and out["stringData"]["url"] == PLACEHOLDER
    assert out["metadata"]["name"] == "x"


def test_kubectl_secret_json_arriving_as_ssh_stdout_is_scrubbed_as_text():
    """`ssh host kubectl get secret x -o json` returns stdout as one string;
    the structural rule never sees a dict (review of #309)."""
    stdout = json.dumps({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "cfoperator-db"},
                         "data": {"postgres-password": "aHVudGVyMg==", "random-name": "c2VjcmV0"},
                         "stringData": {"url": "postgresql://u:hunter2@h/db"}}, indent=2)
    out, count = _redacted({"stdout": stdout, "exit_code": 0})
    assert "aHVudGVyMg==" not in out["stdout"] and "c2VjcmV0" not in out["stdout"] and "hunter2" not in out["stdout"]
    assert '"name": "cfoperator-db"' in out["stdout"]
    assert count >= 3


def test_the_last_applied_configuration_annotation_is_scrubbed_too():
    """`kubectl apply`'d Secrets carry themselves again as one-line JSON in an
    annotation inside the YAML output; the YAML block rule does not reach it."""
    yaml_text = ("apiVersion: v1\nkind: Secret\nmetadata:\n  annotations:\n"
                 "    kubectl.kubernetes.io/last-applied-configuration: |\n"
                 '      {"apiVersion":"v1","data":{"k":"aHVudGVyMg=="},"kind":"Secret","metadata":{"name":"x"}}\n'
                 "  name: x\ndata:\n  k: aHVudGVyMg==\n")
    out, _ = _redacted(yaml_text)
    assert "aHVudGVyMg==" not in out
    assert '"name":"x"' in out and "name: x" in out


def test_a_block_scalar_under_stringdata_loses_every_line():
    """`conf: |` followed by indented lines: the continuation lines are the
    value, not new items (review of #309)."""
    yaml_text = ("kind: Secret\nstringData:\n  conf: |\n    user = admin\n    password = hunter2\n\n"
                 "    token = cfop_abcdefghij\n  plain: simple\ntype: Opaque\n")
    out, _ = _redacted(yaml_text)
    assert "hunter2" not in out and "cfop_abcdefghij" not in out and "user = admin" not in out
    assert f"conf: {PLACEHOLDER}" in out and f"plain: {PLACEHOLDER}" in out
    assert "type: Opaque" in out, "the next top-level key is outside the block"


def test_an_unquoted_value_runs_to_the_end_of_the_line():
    """`password: abc,def` used to keep `def` (review of #309)."""
    out, _ = _redacted("password: abc,def;ghi}jkl\nnext: fine\n")
    assert out == f"password: {PLACEHOLDER}\nnext: fine\n"
    out, _ = _redacted("POSTGRES_PASSWORD=ab,cd;ef\n")
    assert out == f"POSTGRES_PASSWORD={PLACEHOLDER}\n"


def test_a_long_identifier_run_is_redacted_in_linear_time():
    """Redaction runs before the size cap, on the raw result. A hex dump or a
    base64 blob is one long identifier run; the key rule must not be retried
    from every character of it."""
    import time
    blob = "deadbeef" * 50_000  # 400 KB, no secret in it
    start = time.perf_counter()
    out, count = _redacted(blob + "\nPOSTGRES_PASSWORD=hunter2\n")
    elapsed = time.perf_counter() - start
    assert "hunter2" not in out and count == 1
    assert elapsed < 1.0, f"took {elapsed:.2f}s"


def test_tuples_are_walked_like_lists():
    """A tuple result is walked and comes back as a tuple."""
    out, count = _redacted(("POSTGRES_PASSWORD=hunter2", {"token": "x"}))
    assert isinstance(out, tuple) and "hunter2" not in out[0] and out[1]["token"] == PLACEHOLDER and count == 2


def test_a_configmap_with_a_data_block_is_not_a_secret():
    """A ConfigMap has a data: block too and must survive untouched."""
    yaml_text = "kind: ConfigMap\ndata:\n  prometheus_url: http://prom:9090\n  loki_url: http://loki:3100\n"
    out, count = _redacted(yaml_text)
    assert out == yaml_text and count == 0


def test_pem_private_key_body_is_replaced():
    """A key file read with cat: the armour stays, the body goes."""
    pem = ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
           "QyNTUxOQAAACB\n-----END OPENSSH PRIVATE KEY-----\n")
    out, count = _redacted(f"$ cat ~/.ssh/id_ed25519\n{pem}")
    assert "b3BlbnNzaC1rZXktdjEA" not in out
    assert "-----BEGIN OPENSSH PRIVATE KEY-----" in out and "-----END OPENSSH PRIVATE KEY-----" in out
    assert count == 1


def test_connection_string_password_and_webhook_paths_are_hidden():
    """URL userinfo passwords and webhook URL paths."""
    text = ("DATABASE_URL=postgresql://cfop:hunter2@db:5432/kb\n"
            "slack: https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXXXXXXXXXX\n"
            "discord: https://discord.com/api/webhooks/1234567890/abcdefGHIJKL\n")
    out, _ = _redacted(text)
    assert "hunter2" not in out and "XXXXXXXX" not in out and "abcdefGHIJKL" not in out
    assert "https://hooks.slack.com/services/" in out and "db:5432/kb" in out


def test_bearer_headers_and_known_token_prefixes_keep_only_the_prefix():
    """Bearer headers and token shapes keep a recognisable prefix."""
    text = ("curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc'\n"
            "x-api-key: xai-abcdefghijklmnopqrstuvwxyz0123456789\n"
            "token cfop_bZqLYUXNabcdefghij minted\n"
            "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n")
    out, _ = _redacted(text)
    assert "eyJhbGci" not in out and "xai-abcdef" not in out and "bZqLYUXN" not in out and "IOSFODNN7" not in out
    assert f"cfop_{PLACEHOLDER}" in out, "the prefix says what kind of credential it was"
    assert f"Bearer {PLACEHOLDER}" in out



def test_basic_and_bearer_need_a_credential_shaped_value_outside_a_header():
    """"basic" and "bearer" are words. Outside an Authorization header only a
    credential-shaped value goes; inside one, any value does, including a
    base64 pair with no digit in it."""
    for prose in ("Basic authentication is enabled", "basic configuration.\nbearer tokens rotate nightly",
                  "Usage: --auth basic|bearer  Basic installation instructions follow"):
        out, count = _redacted(prose)
        assert out == prose and count == 0, out
    out, count = _redacted("Authorization: Basic dXNlcjpwYXNz\n"
                           "curl -H \"authorization: bearer abcdefghij\" http://x\n"
                           "proxy said: basic Zm9vOmJhcg== rejected\n")
    assert "dXNlcjpwYXNz" not in out and "abcdefghij" not in out and "Zm9vOmJhcg" not in out
    assert f"Authorization: Basic {PLACEHOLDER}" in out and f"bearer {PLACEHOLDER}" in out
    assert count == 3

def test_yaml_and_json_quoted_values_keep_their_quotes():
    """A quoted value is replaced inside its quotes, so the document still parses."""
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
    """False-positive pins: what an investigation reads all day must survive."""
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
    """Numbers, booleans, None and empty strings are not secrets."""
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


# --- second review round on #309 ---------------------------------------------------


def test_an_escaped_quote_inside_a_quoted_value_does_not_end_it():
    """`"abc\\"secretSuffix"` is one value; the escaped quote used to close it
    and leave the suffix (CodeRabbit)."""
    out, _ = _redacted('{"password":"abc\\"secretSuffix","user":"bob"}')
    assert out == '{"password":"' + PLACEHOLDER + '","user":"bob"}'
    out, _ = _redacted("""password='it"s' user=bob""")
    assert out == f"password='{PLACEHOLDER}' user=bob"


def test_an_unterminated_quote_still_redacts_the_rest_of_the_line():
    """Neither the quoted nor the bare branch matched, so the value stayed:
    fail-open (claude-review). A quote that never closes takes the line."""
    out, _ = _redacted('password="abc def\nuser=bob\n')
    assert out == f'password="{PLACEHOLDER}\nuser=bob\n'


def test_one_line_env_output_keeps_the_other_assignments():
    """`cat /proc/1/environ | tr '\\0' ' '` style output: the value ends at the
    next KEY=, so the model keeps the other fields (claude-review)."""
    out, _ = _redacted("PASSWORD=abc user=bob host=x TOKEN=cfop_abcdefghij PATH=/bin")
    assert out == f"PASSWORD={PLACEHOLDER} user=bob host=x TOKEN={PLACEHOLDER} PATH=/bin"


def test_a_secret_data_value_containing_braces_is_still_scrubbed():
    """A stringData value holding serialized JSON has braces inside its quotes;
    a brace-excluding object match stopped short of it (CodeRabbit)."""
    text = '{"kind":"Secret","stringData":{"config":"{\\"nested\\":1,\\"pw\\":\\"hunter2\\"}","k":"v"},"metadata":{"name":"x"}}'
    out, count = _redacted(text)
    assert "hunter2" not in out and '"k":"' + PLACEHOLDER + '"' in out and '"config":"' + PLACEHOLDER + '"' in out
    assert '"name":"x"' in out and count == 2


def test_booleans_under_a_secret_key_are_not_secrets_but_numbers_are():
    """`secret: false` is a flag and stays, uncounted; a numeric token is a PIN."""
    out, count = _redacted({"secret": False, "token": 1234, "ok": True})
    assert out == {"secret": False, "token": PLACEHOLDER, "ok": True} and count == 1


def test_namedtuples_survive_the_walk():
    """`type(v)(iterable)` raises for a namedtuple; it takes its fields positionally."""
    from collections import namedtuple
    Row = namedtuple("Row", "name token")
    out, count = _redacted(Row("x", "cfop_abcdefghij"))
    # No key name travels with a tuple field, so the text rule does the work
    # and the token keeps its prefix, as it would in any string.
    assert isinstance(out, Row) and out.name == "x" and out.token == f"cfop_{PLACEHOLDER}" and count == 1


def test_command_line_flags_are_redacted_in_both_forms():
    """`--password=x` and `--password x`: the shape of ps, docker inspect Args
    and systemctl status output. The key rule's lookbehind rejects a key after
    `-`, so these have their own rule (claude-review on #309)."""
    text = ("mysqld --password=hunter2 --user=root\n"
            "psql --password hunter2 -h db\n"
            "tool --db-password x --api-key=sk-abcdefghijklmnopqrstuvwxyz --token-file /run/t\n"
            "docker login --password-stdin\n"
            'svc --password "two words" --password --help\n')
    out, _ = _redacted(text)
    for gone in ("hunter2", "sk-abcdef", "two words"):
        assert gone not in out, gone
    for kept in ("--user=root", "-h db", "--token-file /run/t", "--password-stdin", "--password --help"):
        assert kept in out, kept
    assert f"--password={PLACEHOLDER}" in out and f"--password {PLACEHOLDER}" in out
    assert f'--password "{PLACEHOLDER}"' in out and f"--db-password {PLACEHOLDER}" in out
    inspect_args = {"Args": ["--password=hunter2", "--user=root"]}
    assert _redacted(inspect_args)[0]["Args"] == [f"--password={PLACEHOLDER}", "--user=root"]


def test_an_unterminated_quoted_flag_value_takes_the_rest_of_the_line():
    """`--password "pa ssword` matched the bare-word branch and kept ` ssword`
    (CodeRabbit on #309); like the key rule, an open quote takes the line."""
    out, _ = _redacted('svc --password "pa ssword\nnext --user=bob\n')
    assert out == f'svc --password "{PLACEHOLDER}\nnext --user=bob\n'


def test_a_bare_value_stops_at_an_embedded_quote_by_design():
    """Pinned on purpose, not endorsed: `password: abc"def` keeps `"def`.
    The quote stop is what keeps `"POSTGRES_PASSWORD=x", "PATH=/usr/bin"` on
    a JSON line from eating the rest of the line, and an unquoted value that
    contains a quote is rare; quoted values have their own branches. If this
    changes, the docker-inspect text fixture above is the one to keep green."""
    out, _ = _redacted('password: abc"def\n')
    assert out == f'password: {PLACEHOLDER}"def\n'
    out, _ = _redacted('["POSTGRES_PASSWORD=secret", "PATH=/usr/bin"]')
    assert out == f'["POSTGRES_PASSWORD={PLACEHOLDER}", "PATH=/usr/bin"]'


# --- arguments: command text is redacted like a result (CFOP-286) ------------


@pytest.mark.parametrize("command, secret", [
    ("mysql -u root -pSECRET123 -e 'show databases'", "SECRET123"),
    ("mysqldump -h db -p'pa ss' app", "pa ss"),
    ("mariadb -uapp -ps3cr3t -e 'select 1'", "s3cr3t"),
    ("redis-cli -h redis -a hunter2 ping", "hunter2"),
    ("redis-cli --user app --pass hunter2 get k", "hunter2"),
    ("sshpass -p hunter2 ssh pi2 uptime", "hunter2"),
    ("sshpass -e -f /dev/null -p hunter2 ssh pi2", "hunter2"),  # options before -p
    ("mysql -u root \\\n  -pSECRET123 -e 'select 1'", "SECRET123"),  # line continuation
    ("mysql -u root \\\n-pSECRET123 db", "SECRET123"),  # continuation, no indent
    ('mysql -p"first\\"SECOND" db', "SECOND"),  # an escaped quote does not end the value
    ('mysql -p"never closed SECOND', "SECOND"),  # an unclosed quote fails closed
    ("mysql -pfirst -pSECOND db", "SECOND"),  # a repeated flag: every one goes
    ("mysql -p mydb -pSECOND", "SECOND"),  # a spaced -p before an attached one
    ("mysql " + "-uapp " * 40 + "-pSECOND db", "SECOND"),  # 240 chars out: in the window
    ("mysql -p'first part'TAILSECRET db", "TAILSECRET"),  # one shell word, many spans
    ('mysql -pabc"def ghi"SECOND db', "SECOND"),
    ('mysql -pAAA"unclosed SECOND', "SECOND"),  # spans, then an unclosed quote
    ("redis-cli -a 'pw'SECOND ping", "SECOND"),
    ("mysql -pFIRST\\\nSECOND db", "SECOND"),  # a continuation inside the password
    ('mysql -p"FIRST\\\nSECOND" db', "SECOND"),
    ("redis-cli -a first --pass SECOND ping", "SECOND"),
    ("curl -H 'Authorization: Bearer abc.def.ghi123' http://x/api", "abc.def.ghi123"),
    ("psql postgresql://app:pw123@db:5432/app -c 'select 1'", "pw123"),
    ("mysql --password=hunter2 db", "hunter2"),
    ("redis-cli -u redis://:hunter2@redis:6379 ping", "hunter2"),  # empty user
])
def test_a_secret_typed_into_a_command_is_not_stored(command, secret):
    """ssh_execute's `command` is not a secret key, so only the text rules can
    catch what the model typed into it. The transcript and the pod log both
    store this copy."""
    out = redact_tool_args({"host": "pi2", "command": command})
    assert secret not in out["command"], out
    assert PLACEHOLDER in out["command"]
    assert out["host"] == "pi2"


@pytest.mark.parametrize("command", [
    "grep -E 'restart|kill' /var/log/syslog",
    "systemctl status x",
    "ps -p 1234 -o pid,cmd",
    "journalctl -u k3s -p err --since '1 hour ago'",
    "mkdir -p /tmp/x",
    "ssh -p 22 pi2 uptime",
    "sshpass -e ssh -p 2222 pi2 uptime",  # the wrapped ssh's -p is a port
    "sshpass -f /run/pw ssh -p 22 pi2",
    "kubectl exec mysql-0 -- ps -p1234",  # mysql-0 is a pod name, not mysql
    "mysql -u root -p mydb",  # a spaced -p prompts; mydb is a database name
    "mysql -u root -p mydb; ps -p1234",  # the next command's -p is not mysql's
    "mysql --protocol=tcp -P 3306 -e 'select 1'",
    "kubectl get pods -n apps -o wide",
])
def test_ordinary_commands_are_stored_as_typed(command):
    """The point of storing the arguments is to show what ran."""
    assert redact_tool_args({"command": command}) == {"command": command}


def test_a_program_flag_rule_stays_inside_its_own_command():
    """`mysql …; ps -p 1234`: the pid is the next command's, not a password,
    and the separator survives."""
    out = redact_tool_args({"command": "mysql -uroot -psecret; ps -p 1234"})["command"]
    assert out == f"mysql -uroot -p{PLACEHOLDER}; ps -p 1234"


def test_process_listings_in_results_lose_a_mysql_password_too():
    """The program-flag rules are text rules, so a `ps aux` result showing a
    client's command line is scrubbed as well as the command that ran it."""
    out, count = _redacted("  99 mysql -pXYZ123 -h db\n 100 mysqld --user=mysql")
    assert "XYZ123" not in out and "mysqld --user=mysql" in out and count == 1


def test_arguments_are_redacted_before_they_are_clipped():
    """A clip landing inside a secret would leave a prefix no rule matches: a
    GitHub token needs 20 characters after ghp_ to be recognised, and the clip
    here leaves 10 of them."""
    command = "x" * 985 + " ghp_" + "A" * 30
    out = redact_tool_args({"command": command}, limit=1000)["command"]
    assert "AAAAAAAAAA" not in out and "ghp_" in out


def test_a_long_line_of_program_names_is_redacted_in_linear_time():
    """Each mysql occurrence scans a bounded window for its flag; unbounded, a
    line of `mysql mysql …` with no flag rescanned the rest of the line per
    occurrence (CodeRabbit on #317). Redaction runs before any size cap."""
    import time

    def seconds(k):
        start = time.monotonic()
        redact_tool_result("mysql " * k)
        return time.monotonic() - start

    # Bounded: ~0.05 s for 5,000, and 4x the input costs ~4x. Unbounded: ~2 s,
    # and 4x the input costs ~16x. The growth ratio is the part a loaded
    # runner cannot fake, so either check passing is enough (claude-review).
    small, large = seconds(5000), seconds(20000)
    assert large < 1.0 or large / max(small, 1e-6) < 8, (small, large)
