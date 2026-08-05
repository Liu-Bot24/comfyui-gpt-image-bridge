from __future__ import annotations

import pytest

from gpt_image_bridge.images import encode_reference_slots, renumber_references

from conftest import image_tensor


def test_different_size_references_keep_slot_order():
    refs = encode_reference_slots(
        image_tensor(11, 7, 0.1),
        image_tensor(5, 13, 0.2),
        None,
        image_tensor(17, 9, 0.3),
    )
    assert [(ref.source_slot, ref.width, ref.height) for ref in refs] == [
        (2, 11, 7),
        (3, 5, 13),
        (5, 17, 9),
    ]
    assert all(ref.role == "reference" for ref in refs)


def test_batch_within_slot_is_deterministic():
    batch = image_tensor(4, 6, 0.1).repeat(2, 1, 1, 1)
    refs = encode_reference_slots(None, batch)
    assert [(ref.source_slot, ref.batch_index) for ref in refs] == [(3, 0), (3, 1)]


def test_reference_limit_is_enforced():
    batch = image_tensor(4, 6, 0.1).repeat(10, 1, 1, 1)
    with pytest.raises(ValueError, match="At most 9"):
        encode_reference_slots(batch)


def test_generate_reference_slots_start_at_one():
    refs = encode_reference_slots(
        image_tensor(11, 7, 0.1),
        image_tensor(5, 13, 0.2),
        start_slot=1,
    )
    assert [ref.source_slot for ref in refs] == [1, 2]


def test_operation_renumbering_preserves_order_and_image_bytes():
    refs = encode_reference_slots(
        image_tensor(11, 7, 0.1),
        None,
        image_tensor(5, 13, 0.2),
    )
    generate_refs = renumber_references(refs, start_slot=1)
    edit_refs = renumber_references(refs, start_slot=2)

    assert [ref.source_slot for ref in generate_refs] == [1, 2]
    assert [ref.source_slot for ref in edit_refs] == [2, 3]
    assert [ref.png_bytes for ref in generate_refs] == [
        ref.png_bytes for ref in refs
    ]
    assert [ref.batch_index for ref in edit_refs] == [0, 0]
