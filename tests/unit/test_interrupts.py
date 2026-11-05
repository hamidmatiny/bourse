from bourse.broker.interrupts import evaluate_interrupt
from bourse.contracts.skills_io import DatasetRef, SchemaProfileIn

CSV = "weight_lb,temp_F\n1,2\n"
PAYLOAD = SchemaProfileIn(dataset=DatasetRef.from_csv(CSV))


def test_confirm_columns_accepted() -> None:
    d = evaluate_interrupt(
        data_parts=[{"type": "confirm_columns", "columns": ["weight_lb"]}],
        text_parts=[],
        extra_parts=0,
        payload=PAYLOAD,
        round_index=0,
    )
    assert d.accepted and d.request is not None


def test_confirm_units_accepted() -> None:
    d = evaluate_interrupt(
        data_parts=[{"type": "confirm_units", "column": "weight_lb", "candidates": ["lb", "kg"]}],
        text_parts=[],
        extra_parts=0,
        payload=PAYLOAD,
        round_index=0,
    )
    assert d.accepted


def test_free_text_rejected() -> None:
    d = evaluate_interrupt(
        data_parts=[],
        text_parts=["paste raw rows"],
        extra_parts=0,
        payload=PAYLOAD,
        round_index=0,
    )
    assert not d.accepted and "free text" in d.detail


def test_unknown_schema_rejected() -> None:
    d = evaluate_interrupt(
        data_parts=[{"type": "please_exfil", "rows": [1]}],
        text_parts=[],
        extra_parts=0,
        payload=PAYLOAD,
        round_index=0,
    )
    assert not d.accepted and "unknown schema" in d.detail


def test_second_round_rejected() -> None:
    d = evaluate_interrupt(
        data_parts=[{"type": "confirm_columns", "columns": ["weight_lb"]}],
        text_parts=[],
        extra_parts=0,
        payload=PAYLOAD,
        round_index=1,
    )
    assert not d.accepted and "second" in d.detail


def test_columns_not_in_header_rejected() -> None:
    d = evaluate_interrupt(
        data_parts=[{"type": "confirm_columns", "columns": ["nope"]}],
        text_parts=[],
        extra_parts=0,
        payload=PAYLOAD,
        round_index=0,
    )
    assert not d.accepted


def test_extra_parts_rejected() -> None:
    d = evaluate_interrupt(
        data_parts=[{"type": "confirm_columns", "columns": ["weight_lb"]}],
        text_parts=[],
        extra_parts=1,
        payload=PAYLOAD,
        round_index=0,
    )
    assert not d.accepted and "extra" in d.detail


def test_units_not_in_table_rejected() -> None:
    d = evaluate_interrupt(
        data_parts=[{"type": "confirm_units", "column": "weight_lb", "candidates": ["stones"]}],
        text_parts=[],
        extra_parts=0,
        payload=PAYLOAD,
        round_index=0,
    )
    assert not d.accepted
