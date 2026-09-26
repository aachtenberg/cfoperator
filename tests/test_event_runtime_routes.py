"""The two event-runtime servers expose the same routes (CFOP-215).

Production runs the stdlib server (``event_runtime/server.py``); the FastAPI
adapter (``fastapi_app.py``) is the documented ASGI alternative. Each carries
its own route table, and FastAPI is not installed in CI, so the adapter's
routes are never exercised there. A route added to one and forgotten in the
other would ship unnoticed. This reads both route tables from source and
requires them to match.
"""
import ast
import re

from repo_paths import REPO_ROOT

SERVER = REPO_ROOT / "event_runtime" / "server.py"
FASTAPI = REPO_ROOT / "event_runtime" / "fastapi_app.py"
_PARAM = re.compile(r"\{[^}]+\}")


def _shape(path):
    return _PARAM.sub("{}", path)


def _fastapi_routes():
    routes = set()
    for node in ast.walk(ast.parse(FASTAPI.read_text())):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in {"get", "post", "put", "patch", "delete"}
                    and dec.args and isinstance(dec.args[0], ast.Constant)):
                routes.add((dec.func.attr.upper(), _shape(dec.args[0].value)))
    return routes


def _is_parsed_path(node):
    return (isinstance(node, ast.Attribute) and node.attr == "path"
            and isinstance(node.value, ast.Name) and node.value.id == "parsed")


def _server_routes():
    tree = ast.parse(SERVER.read_text())
    constants = {target.id: node.value.value
                 for node in tree.body if isinstance(node, ast.Assign)
                 for target in node.targets
                 if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant)}

    def literal(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return constants[node.id]
        raise AssertionError(f"unreadable route expression at line {node.lineno}")

    routes = set()
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name in {"do_GET", "do_POST"}):
            continue
        method = fn.name[3:]
        for node in ast.walk(fn):
            if (isinstance(node, ast.Compare) and _is_parsed_path(node.left)
                    and isinstance(node.ops[0], ast.Eq)):
                routes.add((method, literal(node.comparators[0])))
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "startswith" and _is_parsed_path(node.func.value)):
                routes.add((method, literal(node.args[0]) + "{}"))
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id == "_match_completion_path"):
                routes.add((method, constants["_COMPLETION_PATH_PREFIX"] + "{}"
                            + constants["_COMPLETION_PATH_SUFFIX"]))
    return routes


def test_both_route_tables_are_read():
    # A parser that found nothing would make the comparison below vacuous.
    assert len(_server_routes()) >= 10
    assert len(_fastapi_routes()) >= 10
    assert ("GET", "/v1/alerts/{}") in _server_routes()


def test_the_servers_expose_the_same_routes():
    server, fastapi = _server_routes(), _fastapi_routes()
    assert server - fastapi == set(), f"only in server.py: {sorted(server - fastapi)}"
    assert fastapi - server == set(), f"only in fastapi_app.py: {sorted(fastapi - server)}"
