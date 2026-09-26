"""Every call the agent makes to the event runtime goes through its client (CFOP-214).

The runtime has two auth gates (bearer on /alert and the reads, a shared secret
on the completion post-back), and each caller hand-rolling its own ``urlopen``
is how two of them went out without the bearer for 34 days once the gate was
switched on. ``event_runtime/client.py`` now carries both credentials. This
fails when agent-process code reads the runtime's URL for itself, which is the
first thing a new hand-rolled caller does.

A source check rather than a behavioural one because the failure it prevents
is a caller that does not exist yet.
"""
import ast

from repo_paths import REPO_ROOT

RUNTIME_URL_ENV = "CFOP_EVENT_RUNTIME_URL"

# The agent process: the agent package, its tools, and the console server
# modules at the root. mcp_server and bridge are separate processes that talk
# to the agent, not the runtime.
AGENT_PROCESS = ("agent", "tools")
ROOT_MODULES = ("web_server.py", "web_auth.py")


def _agent_process_sources():
    for d in AGENT_PROCESS:
        for path in sorted((REPO_ROOT / d).rglob("*.py")):
            if path.name.startswith("test_") or "__pycache__" in path.parts:
                continue
            yield path
    for name in ROOT_MODULES:
        yield REPO_ROOT / name


def _reads_of(path, literal):
    """Line numbers where ``literal`` appears as a whole string constant.

    Whole-constant equality skips docstrings and comments that merely mention
    the variable, and catches getenv, environ[...] and environ.get alike.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value == literal]


def test_the_sources_are_found():
    # A wrong root would make the loop below vacuous.
    names = {p.name for p in _agent_process_sources()}
    assert {"agent.py", "web_server.py"} <= names


def test_no_agent_code_reads_the_runtime_url_itself():
    offenders = [f"{path.relative_to(REPO_ROOT)}:{line}"
                 for path in _agent_process_sources()
                 for line in _reads_of(path, RUNTIME_URL_ENV)]
    assert not offenders, (
        f"{RUNTIME_URL_ENV} read outside event_runtime/client.py at {offenders}. "
        "Call the runtime through EventRuntimeClient.from_env(); it sends the "
        "bearer and completion secret the runtime's gates require.")


def test_the_client_is_where_the_url_is_read():
    client = REPO_ROOT / "event_runtime" / "client.py"
    tree = ast.parse(client.read_text())
    assert any(isinstance(n, ast.Constant) and n.value == RUNTIME_URL_ENV for n in ast.walk(tree))
