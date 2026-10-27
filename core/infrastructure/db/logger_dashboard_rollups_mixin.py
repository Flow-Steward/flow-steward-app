from __future__ import annotations

from datetime import date, datetime
from typing import Any

from core.infrastructure.db import dashboard_rollups_store


def _helpers():
    from core.infrastructure.db import platform_logger as _platform_logger

    return _platform_logger


def _default_account_id() -> str:
    return _helpers()._default_account_id()


class LoggerDashboardRollupsMixin:
    conn: Any
    _table_exists: Any
    _column_exists: Any

    def _list_dashboard_rollup_accounts(self) -> list[str]:
        return dashboard_rollups_store.list_dashboard_rollup_accounts(
            self.conn,
            table_exists=self._table_exists,
            column_exists=self._column_exists,
            default_account_id=_default_account_id(),
        )

    @staticmethod
    def _dashboard_parse_dt(value: datetime | str | None) -> datetime | None:
        return dashboard_rollups_store.parse_dt(value)

    def get_dashboard_rollup_state(self, account_id: str | None = None) -> dict:
        return dashboard_rollups_store.get_dashboard_rollup_state(
            self.conn,
            table_exists=self._table_exists,
            account_id=account_id,
            default_account_id=_default_account_id(),
        )

    def list_dashboard_job_facts(
        self,
        *,
        account_id: str | None = None,
        window_start: datetime | str | None = None,
        window_end: datetime | str | None = None,
        limit: int = 0,
        offset: int = 0,
    ) -> list[dict]:
        return dashboard_rollups_store.list_dashboard_job_facts(
            self.conn,
            table_exists=self._table_exists,
            account_id=account_id,
            default_account_id=_default_account_id(),
            window_start=window_start,
            window_end=window_end,
            limit=limit,
            offset=offset,
        )

    def list_dashboard_daily_rollups(
        self,
        *,
        account_id: str | None = None,
        start_date: date | datetime | str | None = None,
        end_date: date | datetime | str | None = None,
    ) -> list[dict]:
        return dashboard_rollups_store.list_dashboard_daily_rollups(
            self.conn,
            table_exists=self._table_exists,
            account_id=account_id,
            default_account_id=_default_account_id(),
            start_date=start_date,
            end_date=end_date,
        )

    def get_core_slo_metrics(
        self,
        *,
        account_id: str | None = None,
        window_hours: int = 24,
    ) -> dict:
        return dashboard_rollups_store.get_core_slo_metrics(
            self.conn,
            table_exists=self._table_exists,
            column_exists=self._column_exists,
            account_id=account_id,
            default_account_id=_default_account_id(),
            window_hours=window_hours,
        )

    def _refresh_dashboard_job_facts_window(
        self,
        *,
        account_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, int]:
        return dashboard_rollups_store.refresh_dashboard_job_facts_window(
            self.conn,
            table_exists=self._table_exists,
            column_exists=self._column_exists,
            account_id=account_id,
            default_account_id=_default_account_id(),
            window_start=window_start,
            window_end=window_end,
        )

    def _refresh_dashboard_daily_rollups_window(
        self,
        *,
        account_id: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, int]:
        return dashboard_rollups_store.refresh_dashboard_daily_rollups_window(
            self.conn,
            table_exists=self._table_exists,
            account_id=account_id,
            default_account_id=_default_account_id(),
            start_date=start_date,
            end_date=end_date,
        )

    def refresh_dashboard_rollups(
        self,
        *,
        account_id: str | None = None,
        now: datetime | str | None = None,
        backfill_days: int = 180,
        lookback_days: int = 3,
    ) -> dict:
        return dashboard_rollups_store.refresh_dashboard_rollups(
            self.conn,
            table_exists=self._table_exists,
            column_exists=self._column_exists,
            default_account_id=_default_account_id(),
            account_id=account_id,
            now=now,
            backfill_days=backfill_days,
            lookback_days=lookback_days,
        )
