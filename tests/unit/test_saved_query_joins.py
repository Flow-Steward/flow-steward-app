"""A saved query may JOIN any tables its dataset covers, and only those.

The scope guard (`test_saved_query_resource_scope.py`) proves a JOIN cannot reach a
table outside the dataset. These cases prove the other direction: a dataset that
covers several tables can join them in every ordinary form.
"""

from __future__ import annotations

import pytest

from core.application.registry.saved_query_contract import (
    SavedQueryContractError,
    validate_saved_query_contract,
)

DATASET = {
    "resource_scope_jsonb": {
        "resources": [
            {"resource_id": "public.orders"},
            {"resource_id": "public.customers"},
            {"resource_id": "public.order_items"},
        ]
    }
}


def _validate(sql: str, *, params: dict | None = None) -> None:
    validate_saved_query_contract(
        {
            "query_language": "sql",
            "query_definition_jsonb": {"sql": sql},
            "params_schema_jsonb": params or {"type": "object", "properties": {}},
        },
        dataset=DATASET,
    )


@pytest.mark.parametrize(
    "sql",
    [
        "select o.id, c.email from orders o join customers c on c.id = o.customer_id limit 50",
        "select o.id, c.email from orders o inner join customers c on c.id = o.customer_id limit 50",
        "select o.id, c.email from orders o left join customers c on c.id = o.customer_id limit 50",
        "select o.id, c.email from orders o right outer join customers c on c.id = o.customer_id limit 50",
        "select o.id from orders o full join customers c on c.id = o.customer_id limit 50",
        "select o.id from public.orders o join public.customers c on c.id = o.customer_id limit 50",
        'select o.id from "public"."orders" o join "customers" c on c.id = o.customer_id limit 50',
        "select o.id from orders o join customers c using (customer_id) limit 50",
        "select o.id, i.sku, c.email from orders o "
        "join order_items i on i.order_id = o.id "
        "join customers c on c.id = o.customer_id limit 50",
        "select o.id from orders o, customers c where c.id = o.customer_id",
        "select c.email, t.total from customers c join "
        "(select customer_id, sum(amount) as total from orders group by customer_id) t "
        "on t.customer_id = c.id limit 50",
        "select c.email from customers c where exists "
        "(select 1 from orders o where o.customer_id = c.id) limit 50",
        "select o.id from orders as o join customers as c on c.id = o.customer_id limit 50",
        "select o.id from orders o cross join customers c limit 50",
        "select o.id from orders natural join customers limit 50",
        "SELECT O.ID FROM PUBLIC.ORDERS O LEFT OUTER JOIN PUBLIC.CUSTOMERS C "
        "ON C.ID = O.CUSTOMER_ID LIMIT 50",
        "select o.id from orders o left join customers c using (customer_id, region) "
        "join order_items i on i.order_id = o.id limit 50",
        "select o.id from (orders o join customers c on c.id = o.customer_id) limit 50",
        "select o.id from (orders o join customers c on c.id = o.customer_id), "
        "order_items i where i.order_id = o.id",
        "select (select max(c.id) from customers c join orders o on o.customer_id = c.id), "
        "o2.id from orders o2 limit 50",
    ],
)
def test_joins_between_tables_of_the_dataset_are_accepted(sql: str) -> None:
    _validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        # LATERAL and ONLY qualify the table after them; both were read as table names.
        "select o.id, x.total from orders o join lateral "
        "(select sum(i.amount) as total from order_items i where i.order_id = o.id) x "
        "on true limit 50",
        "select o.id from orders o, lateral "
        "(select c.email from customers c where c.id = o.customer_id) x limit 50",
        "select o.id from orders o join only customers c on c.id = o.customer_id limit 50",
        # MySQL's STRAIGHT_JOIN is a join, so the table after it is a joined table.
        "select o.id from orders o straight_join customers c on c.id = o.customer_id limit 50",
    ],
)
def test_join_spellings_that_were_refused_are_accepted(sql: str) -> None:
    _validate(sql)


def test_a_join_with_a_parameter_is_accepted_when_the_parameter_is_declared() -> None:
    _validate(
        "select o.id, c.email from orders o join customers c on c.id = o.customer_id "
        "where c.email = :email",
        params={
            "type": "object",
            "properties": {"email": {"type": "string"}},
            "required": ["email"],
        },
    )


@pytest.mark.parametrize(
    "sql",
    [
        "select o.id from orders o join payments p on p.order_id = o.id limit 50",
        "select o.id from orders o left join customers c on c.id = o.customer_id "
        "join payments p on p.order_id = o.id limit 50",
        "select o.id from orders o join public.payments p on p.order_id = o.id limit 50",
        "select o.id from orders o, customers c, payments p limit 50",
    ],
)
def test_a_join_that_reaches_a_table_outside_the_dataset_is_refused(sql: str) -> None:
    with pytest.raises(SavedQueryContractError) as excinfo:
        _validate(sql)
    assert "'payments'" in excinfo.value.message or "'public.payments'" in excinfo.value.message
    assert "outside the dataset scope" in excinfo.value.message


@pytest.mark.parametrize(
    ("sql", "resource"),
    [
        ("select o.id from orders o join secrets s on s.id = o.id limit 50", "secrets"),
        ('select o.id from orders o join "Secrets" s on s.id = o.id limit 50', "secrets"),
        ("select o.id from orders o join/**/secrets s on s.id = o.id limit 50", "secrets"),
        (
            "select o.id from orders o join customers c on c.id = "
            "(select id from secrets limit 1) limit 50",
            "secrets",
        ),
        (
            "select o.id from orders o join platform_secrets p on p.id = o.id limit 50",
            "platform_secrets",
        ),
        (
            "select o.id from orders o join pg_catalog.pg_authid a on true limit 50",
            "pg_catalog.pg_authid",
        ),
        (
            "select o.id from orders o join information_schema.tables t on true limit 50",
            "information_schema.tables",
        ),
        ("select o.id from orders o join lateral secrets s on true limit 50", "secrets"),
    ],
)
def test_a_join_to_a_platform_or_secret_table_is_refused(sql: str, resource: str) -> None:
    with pytest.raises(SavedQueryContractError) as excinfo:
        _validate(sql)
    assert excinfo.value.message == f"Query resource '{resource}' is outside the dataset scope"


@pytest.mark.parametrize(
    ("sql", "resource"),
    [
        # A parenthesized join hid the FROM list entries after it (PostgreSQL).
        (
            "select o.id from (orders o join customers c on c.id = o.customer_id), "
            "secrets s limit 50",
            "secrets",
        ),
        # A parenthesized table (MySQL, SQLite) was skipped as if it were a subquery.
        ("select o.id from orders o join (secrets) s on s.id = o.id limit 50", "secrets"),
        ("select o.id from orders o join ((secrets)) s on true limit 50", "secrets"),
        ("select o.id from orders o, (customers) c, secrets s limit 50", "secrets"),
        ("select o.id from (orders o, customers c), secrets limit 50", "secrets"),
        # PostgreSQL's `TABLE name` reads the whole table without a FROM.
        ("select * from (table secrets) t limit 50", "secrets"),
        ("select o.id from orders o where o.id in (table secrets) limit 50", "secrets"),
        # MySQL's STRAIGHT_JOIN joins the table after it.
        ("select o.id from orders o straight_join secrets s limit 50", "secrets"),
        # Another schema's table with a declared table's name is not the declared one.
        ("select a.id from orders o join audit.orders a on a.id = o.id limit 50", "audit.orders"),
        (
            "select u.id from orders o join auth.customers u on u.id = o.customer_id limit 50",
            "auth.customers",
        ),
    ],
)
def test_a_join_that_used_to_slip_past_the_scope_check_is_refused(sql: str, resource: str) -> None:
    with pytest.raises(SavedQueryContractError) as excinfo:
        _validate(sql)
    assert excinfo.value.message == f"Query resource '{resource}' is outside the dataset scope"


def test_a_join_still_needs_where_or_limit() -> None:
    with pytest.raises(SavedQueryContractError) as excinfo:
        _validate("select o.id from orders o join customers c on c.id = o.customer_id")
    assert "WHERE clause or LIMIT" in excinfo.value.message
