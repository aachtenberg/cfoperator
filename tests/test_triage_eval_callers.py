#!/usr/bin/env python3
"""Every script that imports benchmarks/triage_eval.py uses names it still has (CFOP-550).

CFOP-314 replaced ``load_production_system_prompt()`` with
``production_system_prompt()`` so the harness builds its prompt from the same
function production does. Three callers kept the old name:
``scripts/build_triage_dataset.py``, ``benchmarks/reason_compare.py`` and
``hf/check_model_text.py``. All three died with AttributeError on the line that
does their actual work, and CI stayed green because no test runs that far: the
retrain runbook's first command was a trap for six weeks. reason_compare.py
was broken twice over: the same CFOP-314 change made ``call_ollama`` take a
prebuilt payload, and it still passed five arguments.

So the guard checks both halves of a call: the name exists, and a call to a
harness function passes a number of positional arguments the signature
accepts and only keyword names it declares.

The check is static on purpose. Importing triage_eval imports the agent, which
needs a full dependency set and a path dance; reading both sides with ``ast``
needs neither, and it covers a caller added tomorrow without registration.
"""

from __future__ import annotations

import ast

from repo_paths import REPO_ROOT

HARNESS = REPO_ROOT / "benchmarks" / "triage_eval.py"
_SKIP_PARTS = {".git", ".claude", "__pycache__", "node_modules", ".venv"}


def _defined_names(path) -> set[str]:
    """Module-level names: defs, classes, assignments and imports."""
    names = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                names |= {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
    return names


def _uses(path) -> set[tuple[str, int]]:
    """(attribute, line) for every ``<alias>.attr`` where alias is triage_eval."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname or a.name for a in node.names if a.name == "triage_eval"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
            found.add((node.attr, node.lineno))
    return found


def _signatures(path) -> dict:
    """name -> (min positional, max positional or None, keyword names)."""
    sigs = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            positional = a.posonlyargs + a.args
            required = len(positional) - len(a.defaults)
            most = None if a.vararg else len(positional)
            kw = None if a.kwarg else {x.arg for x in a.args + a.kwonlyargs}
            sigs[node.name] = (required, most, kw)
    return sigs


def _calls(path) -> list:
    """(func, n_positional, keyword names, line) for each ``<alias>.func(...)``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
               for a in n.names if a.name == "triage_eval"}
    out = []
    for node in ast.walk(tree):
        f = getattr(node, "func", None)
        if (isinstance(node, ast.Call) and isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name) and f.value.id in aliases):
            if any(isinstance(x, ast.Starred) for x in node.args) or any(k.arg is None for k in node.keywords):
                continue  # *args / **kw: not statically checkable
            out.append((f.attr, len(node.args), {k.arg for k in node.keywords}, node.lineno))
    return out


def _callers():
    for path in sorted(REPO_ROOT.rglob("*.py")):
        if _SKIP_PARTS & set(path.relative_to(REPO_ROOT).parts) or path == HARNESS:
            continue
        if "triage_eval" in path.read_text(encoding="utf-8", errors="ignore"):
            uses = _uses(path)
            if uses:
                yield path, uses


def test_callers_only_use_names_the_harness_defines():
    defined = _defined_names(HARNESS)
    callers = list(_callers())
    # Not vacuous: the three scripts that motivated this guard must be found.
    found = {p.relative_to(REPO_ROOT).as_posix() for p, _ in callers}
    assert {"scripts/build_triage_dataset.py", "benchmarks/reason_compare.py",
            "hf/check_model_text.py"} <= found, found
    missing = [f"{p.relative_to(REPO_ROOT)}:{line}  triage_eval.{attr}"
               for p, uses in callers for attr, line in sorted(uses, key=lambda u: u[1])
               if attr not in defined]
    assert not missing, (
        "these call names benchmarks/triage_eval.py no longer defines:\n  "
        + "\n  ".join(missing))


def test_callers_pass_arguments_the_harness_signatures_accept():
    sigs = _signatures(HARNESS)
    bad = []
    checked = 0
    for path, _ in _callers():
        for func, npos, kws, line in _calls(path):
            if func not in sigs:
                continue  # a missing name is the other test's failure
            checked += 1
            required, most, accepted = sigs[func]
            where = f"{path.relative_to(REPO_ROOT)}:{line}  triage_eval.{func}"
            if npos + len(kws) < required or (most is not None and npos > most):
                bad.append(f"{where}: {npos} positional + {sorted(kws)} for a signature "
                           f"taking {required}..{most if most is not None else 'n'}")
            elif accepted is not None and kws - accepted:
                bad.append(f"{where}: unknown keyword(s) {sorted(kws - accepted)}")
    assert checked, "no calls into triage_eval were found; the guard is checking nothing"
    assert not bad, "calls that no longer fit benchmarks/triage_eval.py:\n  " + "\n  ".join(bad)
