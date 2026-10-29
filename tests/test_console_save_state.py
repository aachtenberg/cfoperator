"""A console Save is lit only while there is something to save (CFOP-561).

Every explicit Save in Admin (the per-scope model rows, the provider keys,
the node-action allowlist) was a ``chip primary`` that was never disabled. In
the operator's words, Save stays highlighted even after you save. The fix is
one helper, ``armSave`` in ``ui/common.js``. A page calls it right after
rendering from the server, and it keeps the button disabled (grey) while every
input still matches what was rendered.

These guards are about the class of regression. A primary Save is found
by its markup on every page, template strings included, not by name, so a
fourth one added later must start disabled and be armed, or this fails. The
behavioural half runs ``armSave`` under node, the way ``test_console_list_kit``
runs the kit, because "edit lights it, putting it back greys it" is a round
trip a grep cannot see. Fails closed without node: a skip would be a green
run that tested nothing.
"""

from repo_paths import REPO_ROOT
import json
import re
import shutil
import subprocess

import pytest

UI = REPO_ROOT / "ui"
COMMON_JS = UI / "common.js"

#: A primary button whose label starts with "Save", in markup or in a JS
#: template string. The plain "+ Save repo" chips are not primary and are not
#: matched: they create a new entry rather than save a loaded form.
_PRIMARY_SAVE = re.compile(
    r"<button\b(?P<attrs>[^>]*\bclass=\"[^\"]*\bprimary\b[^\"]*\"[^>]*)>\s*Save\b[^<]*</button>")
# A bare ``disabled`` attribute, not one produced by a ${...} conditional:
# that shape (``${storage ? '' : ' disabled'}``) was the old provider-key
# Save, which was lit whenever storage was available.
_DISABLED = re.compile(r"(?<=\s)disabled(?=[\s>])")
_ID = re.compile(r"\bid=\"(?P<id>[^\"]*)\"")


def read(name):
    return (UI / name).read_text(encoding="utf-8")


def primary_saves():
    """(page, the button's attributes) for every primary Save in ui/."""
    return [(p.name, m.group("attrs"))
            for p in sorted(UI.glob("*.html"))
            for m in _PRIMARY_SAVE.finditer(p.read_text(encoding="utf-8"))]


SAVES = primary_saves()


def test_the_primary_saves_are_found():
    """The scope rows, the provider keys and the allowlist. If the pattern
    stops matching, every guard below passes on an empty list."""
    assert len([1 for page, _ in SAVES if page == "admin.html"]) >= 3, SAVES


@pytest.mark.parametrize("page,attrs", SAVES)
def test_a_primary_save_starts_disabled(page, attrs):
    assert _DISABLED.search(attrs), (
        f"{page}: a primary Save renders lit ({attrs.strip()}). Render it disabled "
        "and let armSave() light it once an input changes (CFOP-561).")


@pytest.mark.parametrize("page,attrs", SAVES)
def test_a_primary_save_is_armed(page, attrs):
    """By id: the literal part before any ${...} is what the page looks it up
    by when it arms the button after rendering."""
    m = _ID.search(attrs)
    assert m, f"{page}: a primary Save has no id, so nothing can arm it ({attrs.strip()})"
    prefix = m.group("id").split("${")[0]
    assert prefix, f"{page}: a primary Save's id has no literal part ({m.group('id')})"
    assert f"armSave(document.getElementById('{prefix}" in read(page), (
        f"{page}: #{m.group('id')} is never passed to armSave(), so it would stay "
        "grey forever, or lit forever if someone drops the disabled.")


@pytest.mark.parametrize("page", sorted({page for page, _ in SAVES}))
def test_a_disabled_primary_save_looks_grey(page):
    """.chip:disabled only dims, so a disabled primary would still read as
    accent. The page has to style the disabled primary itself."""
    assert re.search(r"\.chip\.primary:disabled\s*\{[^}]*border-color", read(page)), (
        f"{page}: no .chip.primary:disabled rule; a clean Save would still look highlighted")


# --------------------------------------------------------------------------
# armSave itself, under node
# --------------------------------------------------------------------------

_STUB = r"""
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync(process.argv[2],'utf8');
function el(type, value){
  const on={};
  return {type, value, checked:!!value,
    addEventListener(ev,fn){ (on[ev]=on[ev]||[]).push(fn); },
    fire(ev){ (on[ev]||[]).forEach(fn=>fn()); }};
}
const box={document:{}}; box.globalThis=box; vm.createContext(box); vm.runInContext(src,box);
const btn={disabled:false}, out={};

const model=el('text','gemma4:26b'), backend=el('select-one','ollama'), tick=el('checkbox',true);
box.armSave(btn,[backend,model,null,tick]);
out.clean=btn.disabled;
model.value='gemma4:31b'; model.fire('input'); out.typed=btn.disabled;
model.value='gemma4:26b '; model.fire('input'); out.typedBackWithSpace=btn.disabled;
backend.value='deepseek'; backend.fire('change'); out.selectChanged=btn.disabled;
backend.value='ollama'; backend.fire('change'); out.selectBack=btn.disabled;
tick.checked=false; tick.fire('change'); out.unticked=btn.disabled;

// A successful save re-renders and re-arms: the new values are the baseline.
backend.value='deepseek';
box.armSave(btn,[backend,model,tick]);
out.rearmed=btn.disabled;

const empty={disabled:false}; box.armSave(empty,[]); out.noInputs=empty.disabled;
box.armSave(null,[model]); out.nullButtonSurvives=true;
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def behaviour(tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required to run armSave; skipping would pass while not guarding")
    stub = tmp_path_factory.mktemp("save") / "stub.js"
    stub.write_text(_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(COMMON_JS)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_save_is_grey_until_something_changes(behaviour):
    assert behaviour["clean"] is True


def test_an_edit_lights_it_and_putting_it_back_greys_it(behaviour):
    """A trailing space is not a change: the saves send trimmed values."""
    assert behaviour["typed"] is False
    assert behaviour["typedBackWithSpace"] is True
    assert behaviour["selectChanged"] is False and behaviour["selectBack"] is True


def test_a_checkbox_counts(behaviour):
    """The allowlist is all checkboxes; their value attribute never changes."""
    assert behaviour["unticked"] is False


def test_rearming_after_a_save_greys_it(behaviour):
    """The defect itself: lit straight after a save."""
    assert behaviour["rearmed"] is True


def test_nothing_to_edit_means_nothing_to_save(behaviour):
    assert behaviour["noInputs"] is True and behaviour["nullButtonSurvives"] is True
