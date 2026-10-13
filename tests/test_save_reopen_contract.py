"""A bookmark revision may advance, but content, privacy and inventory may not."""

from copy import deepcopy

import pytest

from scripts.acceptance_scope import reopened_content_preserved


def state():
    return {
        "node_id": "notice",
        "items": [{"id": "paper", "holder_id": "npc", "version": 3}],
        "reading": {
            "revision": 7,
            "epoch": 0,
            "progress": {"selected": "speech:0", "fingerprint": "abc", "acknowledged": False},
        },
    }


def test_only_bookmark_metadata_can_advance_without_mutating_input():
    before, after = state(), state()
    assert reopened_content_preserved(before, after)
    after["reading"]["revision"] = 8
    assert reopened_content_preserved(before, after)
    assert before["reading"]["revision"] == 7


@pytest.mark.parametrize("revision", [6, -1, True, 8.0, "8", None])
def test_revision_is_monotonic_integer_not_coerced(revision):
    before, after = state(), state()
    after["reading"]["revision"] = revision
    assert not reopened_content_preserved(before, after)


@pytest.mark.parametrize("change", ["epoch", "selected", "acknowledged", "holder", "item-version", "node", "extra", "missing", "missing-revision"])
def test_metadata_exception_cannot_hide_real_state_changes(change):
    before, after = state(), deepcopy(state())
    after["reading"]["revision"] = 8
    if change == "epoch":
        after["reading"]["epoch"] = 1
    elif change in {"selected", "acknowledged"}:
        after["reading"]["progress"][change] = "changed"
    elif change == "holder":
        after["items"][0]["holder_id"] = "player"
    elif change == "item-version":
        after["items"][0]["version"] = 4
    elif change == "node":
        after["node_id"] = "other"
    elif change == "extra":
        after["reading"]["progress"]["new"] = True
    elif change == "missing":
        del after["reading"]["progress"]["selected"]
    else:
        del after["reading"]["revision"]
    assert not reopened_content_preserved(before, after)
