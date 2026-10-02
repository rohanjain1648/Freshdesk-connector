import pytest

from freshdesk_connector.errors import ValidationError
from freshdesk_connector.search import MAX_QUERY_LENGTH, build_ticket_query, custom_field_name


def test_combines_filters_with_and_and_values_with_or():
    q = build_ticket_query(status=["open", "pending"], priority=["urgent"], tags=["refund"])
    assert q == "(status:2 OR status:3) AND priority:4 AND tag:'refund'"


def test_dates_custom_fields_and_numeric_codes():
    q = build_ticket_query(
        status=[6],
        created_after="2026-09-01",
        updated_before="2026-09-30T10:00:00Z",
        custom_fields={"order_id": "ORD-1", "cf_is_vip": True, "attempts": 3},
    )
    assert q == (
        "status:6 AND created_at:>'2026-09-01' AND updated_at:<'2026-09-30' "
        "AND cf_order_id:'ORD-1' AND cf_is_vip:true AND cf_attempts:3"
    )


@pytest.mark.parametrize("bad", ["refund' OR status:5", 'x"y', "a\\b", ""])
def test_rejects_values_that_could_alter_the_query(bad):
    with pytest.raises(ValidationError):
        build_ticket_query(tags=[bad])


def test_rejects_unknown_status_and_bad_dates_and_field_names():
    with pytest.raises(ValidationError):
        build_ticket_query(status=["waiting"])
    with pytest.raises(ValidationError):
        build_ticket_query(created_after="last week")
    with pytest.raises(ValidationError):
        custom_field_name("order id; drop")


def test_requires_a_filter():
    with pytest.raises(ValidationError, match="at least one filter"):
        build_ticket_query()


def test_enforces_freshdesk_query_length_limit():
    tags = [f"tag_{i:03d}_" + "x" * 20 for i in range(30)]
    with pytest.raises(ValidationError, match=str(MAX_QUERY_LENGTH)):
        build_ticket_query(tags=tags)
