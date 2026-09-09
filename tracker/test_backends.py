"""Backend selection and the shapes shared by every adapter."""

from __future__ import annotations

import pytest

import backends
from shapes import decode_ref, encode_ref, item_from_body, md_to_html_lite


def test_make_backend_requires_a_name_and_knows_the_three():
    with pytest.raises(backends.TrackerError) as e:
        backends.make_backend({})
    assert "CFOP_TRACKER_BACKEND" in str(e.value)
    assert "github" in str(e.value) and "jira" in str(e.value) and "plane" in str(e.value)
    with pytest.raises(backends.TrackerError) as e:
        backends.make_backend({"CFOP_TRACKER_BACKEND": "linear"})
    assert "linear" in str(e.value)
    assert set(backends.BACKENDS) == {"plane", "github", "jira"}


@pytest.mark.parametrize("backend, missing", [
    ("github", "GITHUB_TOKEN"),
    ("jira", "JIRA_BASE_URL"),
    ("plane", "PLANE_BASE_URL"),
])
def test_each_backend_names_its_first_missing_env(backend, missing):
    with pytest.raises(backends.TrackerError) as e:
        backends.make_backend({"CFOP_TRACKER_BACKEND": backend})
    assert missing in str(e.value)


def test_github_repo_must_be_owner_slash_repo():
    with pytest.raises(backends.TrackerError) as e:
        backends.make_backend({"CFOP_TRACKER_BACKEND": "github", "GITHUB_TOKEN": "t",
                               "CFOP_TRACKER_GITHUB_REPO": "justname"})
    assert "owner/repo" in str(e.value)


def test_ref_round_trip_and_rejects_garbage():
    meta = {"backend": "plane", "id": "abc"}
    assert decode_ref(encode_ref(meta)) == meta
    with pytest.raises(ValueError):
        decode_ref("!!not base64!!")
    with pytest.raises(ValueError):
        decode_ref(encode_ref(["a", "list"]))  # decodes but is not an object


def test_item_from_body_defaults_and_normalises():
    item = item_from_body({"remediation_id": 7, "title": "  t  ", "labels": [" a ", "", "b"]})
    assert item.title == "t" and item.priority == "low" and item.labels == ["a", "b"]
    assert item.links == {} and item.body_markdown == ""
    with pytest.raises(ValueError):
        item_from_body({"remediation_id": 7, "title": "t", "links": ["not", "a", "dict"]})


def test_md_to_html_lite_escapes_and_renders():
    md = ("## Why\n- **Host:** `pi2`\n- see https://example.com/x\n\n"
          "para <script>alert(1)</script>\n\n```\nraw <b>\n```\n---\nfooter")
    out = md_to_html_lite(md)
    assert "<h3>Why</h3>" in out
    assert "<ul><li><strong>Host:</strong> <code>pi2</code></li>" in out
    assert '<a href="https://example.com/x">https://example.com/x</a>' in out
    assert "&lt;script&gt;" in out and "<script>" not in out
    assert "<pre>raw &lt;b&gt;</pre>" in out
    assert "<hr>" in out and "<p>footer</p>" in out
