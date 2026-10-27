"""A saved query must not reach a table outside its dataset's resource scope.

The E2E ledger's data-minimization item asked for the over-broad `select *` test
fixture to be narrowed and for the existing resource/output-scope validation to be
checked. Checking it surfaced two ways to walk straight past the existing scope
guard, because `_RESOURCE_REFERENCE` only ever looked at the single identifier
directly after `FROM`/`JOIN`:

* a double-quoted (or backtick/bracket-quoted) table name did not match the
  identifier pattern at all, so `from "secrets"` was invisible to the check;
* a comma-separated FROM list only had its first entry examined, so
  `from account a, secrets s` read `secrets` unchecked.

Both are bypasses of a guard that already exists, not a new policy: the fix is to
the reference extractor.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.registry.saved_query_contract import (
    SavedQueryContractError,
    validate_saved_query_contract,
)

DATASET = {"resource_scope_jsonb": {"resources": [{"resource_id": "public.account"}]}}


def _validate(sql: str, *, params: dict | None = None) -> None:
    validate_saved_query_contract(
        {
            "query_language": "sql",
            "query_definition_jsonb": {"sql": sql},
            "params_schema_jsonb": params or {"type": "object", "properties": {}},
        },
        dataset=DATASET,
    )


# ---------------------------------------------------------------------------
# In-scope queries keep working
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "select account from account limit 10",
        "select account, accountLogin from account limit 10",
        "select a.account from account a limit 10",
        "select a.account from public.account a limit 10",
        'select a.account from "account" a limit 10',
        "select x from (select account as x from account limit 5) t limit 10",
        "select a.account from account a join account b on b.account = a.account limit 10",
        'select a.account from account "a" limit 10',
        "select a.account from account as a limit 10",
        "select 1 from account a join account b using (account) limit 10",
        "select 1 from account a, account b limit 10",
        "select 1 from account a join account b on coalesce(a.x, b.x) = 1 limit 10",
        "select 1 from account a join account b on b.account in (1, 2) limit 10",
        "select 1 from account a left join account b on 1 = 1 limit 10",
        "select 1 from account a, account b, account c limit 10",
    ],
)
def test_in_scope_queries_are_accepted(sql):
    _validate(sql)


# ---------------------------------------------------------------------------
# The bypasses
# ---------------------------------------------------------------------------


def test_a_quoted_out_of_scope_table_is_rejected():
    """`from "secrets"` must not slip past the identifier pattern."""
    with pytest.raises(SavedQueryContractError) as excinfo:
        _validate('select id from "secrets" limit 10')
    assert "outside the dataset scope" in excinfo.value.message


@pytest.mark.parametrize(
    "sql",
    [
        "select a.account from account a, secrets s limit 10",
        "select a.account from account a , secrets limit 10",
        "select a.account from account, public.secrets limit 10",
        'select a.account from account a, "secrets" s limit 10',
        "select a.account from account as a, secrets limit 10",
        "select a.account from account a,\n secrets s limit 10",
        "select 1 from account a, account b, secrets c limit 10",
        "select 1 from account a, public.secrets s limit 10",
    ],
)
def test_a_comma_joined_out_of_scope_table_is_rejected(sql):
    """Only the first entry of a comma-separated FROM list used to be checked."""
    with pytest.raises(SavedQueryContractError) as excinfo:
        _validate(sql)
    assert "outside the dataset scope" in excinfo.value.message


@pytest.mark.parametrize(
    "sql",
    [
        # A quoted alias did not match the bare-identifier alias pattern, so the
        # scan stopped there and never reached the rest of the FROM list.
        'select a.account from account "a", secrets s limit 10',
        "select a.account from account `a`, secrets limit 10",
        "select a.account from account [a], secrets limit 10",
        'select a.account from account as "a", secrets limit 10',
        'select 1 from "account" "a", "secrets" "s" limit 10',
    ],
)
def test_a_quoted_alias_does_not_hide_the_rest_of_the_from_list(sql):
    with pytest.raises(SavedQueryContractError) as excinfo:
        _validate(sql)
    assert "outside the dataset scope" in excinfo.value.message


@pytest.mark.parametrize(
    "sql",
    [
        # The FROM list continues after a join condition; the scan used to stop at ON.
        "select 1 from account a join account b on b.account = a.account, secrets limit 10",
        "select 1 from account a join account b using (account), secrets s limit 10",
        "select 1 from account a join account b on coalesce(a.x, b.x) = 1, secrets limit 10",
        "select 1 from account a left join account b on 1 = 1, secrets limit 10",
        "select 1 from account a join account b on b.account in (1, 2), secrets limit 10",
    ],
)
def test_a_join_condition_does_not_hide_the_rest_of_the_from_list(sql):
    with pytest.raises(SavedQueryContractError) as excinfo:
        _validate(sql)
    assert "outside the dataset scope" in excinfo.value.message


# ---------------------------------------------------------------------------
# Everything the guard already caught must keep being caught
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "select id from secrets limit 10",
        "select id from public.secrets limit 10",
        "select x from (select id as x from secrets limit 5) t limit 10",
        "select account from account union select id from secrets limit 10",
        "select id from account join secrets on secrets.id = account.account limit 10",
        "select 1 from account a cross join secrets limit 10",
        'select 1 from "public"."secrets" limit 10',
        "select 1 from account a, (select 1 from secrets limit 1) t limit 10",
    ],
)
def test_previously_caught_out_of_scope_references_stay_rejected(sql):
    with pytest.raises(SavedQueryContractError) as excinfo:
        _validate(sql)
    assert "outside the dataset scope" in excinfo.value.message


@pytest.mark.parametrize(
    "sql, fragment",
    [
        ("select account from account", "WHERE clause or LIMIT"),
        ("delete from account", "read-only SELECT"),
        ("select account from account limit 1; select 1", "one SQL statement"),
    ],
)
def test_other_existing_guards_are_unchanged(sql, fragment):
    with pytest.raises(SavedQueryContractError) as excinfo:
        _validate(sql)
    assert fragment in excinfo.value.message


def test_an_undeclared_parameter_is_still_rejected():
    with pytest.raises(SavedQueryContractError) as excinfo:
        _validate("select account from account where accountIsDisabled = :disabled limit 5")
    assert "is not declared" in excinfo.value.message


# ---------------------------------------------------------------------------
# A "read-only SELECT" that writes, locks or calls out is refused (CWE-89)
# ---------------------------------------------------------------------------
# The check looked at the leading SELECT and a denylist of DML/DDL words; SELECT
# INTO, row locks and side-effecting functions passed it and reached the
# connector with its credentials.


@pytest.mark.parametrize(
    "sql",
    [
        "select * into copied_account from account limit 1",
        "select account into #tmp from account limit 1",
        "select account from account limit 1 into outfile '/tmp/accounts.csv'",
        "select account from account limit 1 into dumpfile '/tmp/a.bin'",
        "select account from account where account = 1 for share",
        "select account from account where account = 1 for no key update",
        "select account from account where account = 1 for key share",
        "select account from account where account = 1 lock in share mode",
        "select nextval('account_seq') from account limit 1",
        "select setval('account_seq', 1) from account limit 1",
        "select set_config('role', 'admin', false) from account limit 1",
        "select pg_sleep(10) from account limit 1",
        "select pg_read_file('/etc/passwd') from account limit 1",
        "select lo_export(1, '/tmp/x') from account limit 1",
        "select dblink_exec('host=x', 'drop table y') from account limit 1",
        "select load_file('/etc/passwd') from account limit 1",
        "select sleep(5) from account limit 1",
        "select xp_cmdshell('whoami') from account limit 1",
    ],
)
def test_a_select_with_side_effects_is_refused(sql: str) -> None:
    with pytest.raises(SavedQueryContractError, match="read-only SELECT"):
        _validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        # The words alone inside string literals or comments are data, not SQL.
        "select account from account where account = 'into outfile' limit 10",
        "select account from account /* for share */ limit 10",
        # Column names that merely start like a blocked function are fine.
        "select account as pg_label from account limit 10",
        "select sleep_minutes from account limit 10",
    ],
)
def test_a_plain_select_mentioning_those_words_as_data_still_passes(sql: str) -> None:
    _validate(sql)
