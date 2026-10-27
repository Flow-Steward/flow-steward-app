# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .db_store_common import *  # NOSONAR


class RegistryDBStoreConnectionMixin:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn: Any | None = None
        self._pool: Any | None = None
        self._tx_local = threading.local()
        self._public_projection_sync_in_progress = False
        self._column_exists_cache: dict[tuple[str, str, str], bool] = {}
        self._backend_initialized = False
        self._builder_registry_schema_ready = False

    def _ensure_backend(self) -> None:
        if self._conn is not None:
            if self._pool is None and isinstance(self._conn, PooledConnectionProxy):
                self._pool = getattr(self._conn, "_pool", None)
            if self._pool is not None:
                self._backend_initialized = True
                return
            if not bool(getattr(self._conn, "closed", 0)):
                self._backend_initialized = True
                return

        if self._backend_initialized:
            if self._pool is not None:
                return
            if self._conn is not None and not self._conn.closed:
                return

        self._pool = None
        self._conn = None
        if pooling_enabled() and self.dsn:
            min_conn, max_conn = pool_bounds()
            if max_conn > 1:
                self._pool = get_threaded_pool(self.dsn, minconn=min_conn, maxconn=max_conn)
                self._conn = PooledConnectionProxy(self._pool, dsn=self.dsn)
                self._backend_initialized = True
                return
        self._conn = psycopg_runtime.connect(self.dsn)
        self._conn.autocommit = True
        self._backend_initialized = True

    def _get_tx_conn(self) -> Any | None:
        return getattr(self._tx_local, "conn", None)

    def _set_tx_conn(self, conn: Any | None) -> None:
        self._tx_local.conn = conn

    def participates_in_transaction(self, transaction_connection: Any) -> bool:
        """Prove registry writes are bound to the lifecycle transaction."""
        return self._get_tx_conn() is transaction_connection

    @contextmanager
    def participate_in_external_transaction(self, transaction_connection: Any):
        """Bind registry reads/writes to an already-open application transaction."""
        current = self._get_tx_conn()
        if current is not None and current is not transaction_connection:
            raise RuntimeError("registry store is already bound to another transaction")
        owns_binding = current is None
        if owns_binding:
            # Never carry deferred projection work across transaction scopes.
            self._consume_projection_sync_pending()
        self._set_tx_conn(transaction_connection)
        try:
            yield
        finally:
            if owns_binding:
                projection_sync_pending = self._consume_projection_sync_pending()
                self._set_tx_conn(None)
                if projection_sync_pending:
                    register_transaction_after_commit(
                        transaction_connection,
                        self._trigger_public_contract_projection_sync,
                    )

    def _has_active_transaction(self) -> bool:
        return self._get_tx_conn() is not None

    def _set_projection_sync_pending(self, pending: bool) -> None:
        self._tx_local.projection_sync_pending = bool(pending)

    def _consume_projection_sync_pending(self) -> bool:
        pending = bool(getattr(self._tx_local, "projection_sync_pending", False))
        self._tx_local.projection_sync_pending = False
        return pending

    def _projection_sync_suppression_depth(self) -> int:
        return int(getattr(self._tx_local, "projection_sync_suppression_depth", 0) or 0)

    def _set_suppressed_projection_sync_pending(self, pending: bool) -> None:
        self._tx_local.suppressed_projection_sync_pending = bool(pending)

    def _consume_suppressed_projection_sync_pending(self) -> bool:
        pending = bool(getattr(self._tx_local, "suppressed_projection_sync_pending", False))
        self._tx_local.suppressed_projection_sync_pending = False
        return pending

    @contextmanager
    def suppress_projection_sync(self):
        depth = self._projection_sync_suppression_depth()
        self._tx_local.projection_sync_suppression_depth = depth + 1
        try:
            yield
        finally:
            self._tx_local.projection_sync_suppression_depth = depth
            should_sync_projection = (
                depth == 0 and self._consume_suppressed_projection_sync_pending()
            )
            if should_sync_projection:
                if self._get_tx_conn() is not None:
                    self._set_projection_sync_pending(True)
                else:
                    self._trigger_public_contract_projection_sync()

    def _clear_failed_transaction(self) -> None:
        tx_conn = self._get_tx_conn()
        if tx_conn is None:
            return
        try:
            if tx_conn.get_transaction_status() != _PG_TX_STATUS_IDLE:
                tx_conn.rollback()
        except Exception:
            # Best-effort cleanup for optional/fallback code paths that swallowed
            # a DB exception but need the active transaction to remain usable.
            pass

    @property
    def conn(self):
        self._ensure_backend()
        if self._pool is not None:
            return self._conn
        if self._conn is None or self._conn.closed:
            self._conn = psycopg_runtime.connect(self.dsn)
            self._conn.autocommit = True
        return self._conn

    def _acquire_conn(self):
        # Initialize before choosing the pooled/direct branch. Otherwise the
        # first call returns an autocommit proxy from `self.conn`, and a caller
        # cannot open a real transaction or retain its row locks.
        if self._pool is None:
            self._ensure_backend()
        if self._pool is not None:
            try:
                conn = self._pool.getconn()
                conn.autocommit = True
                return conn, True
            except Exception as exc:
                # Under short traffic bursts (e.g. workflow builder loading many
                # parallel registry endpoints), a small pool can be temporarily
                # exhausted. Fallback to an ad-hoc direct connection so request
                # handlers return validation payloads instead of HTTP 500.
                logger.warning(
                    "RegistryDBStore pool borrow failed (%s); falling back to direct connection",
                    exc,
                )
                conn = psycopg_runtime.connect(self.dsn)
                conn.autocommit = True
                return conn, False
        return self.conn, False

    def _release_conn(self, conn: Any, from_pool: bool) -> None:
        if from_pool and self._pool is not None:
            close_conn = bool(getattr(conn, "closed", 0))
            self._pool.putconn(conn, close=close_conn)
            return
        if conn is not None and conn is not self._conn:
            with suppress(Exception):
                conn.close()

    @contextmanager
    def _cursor(self):
        tx_conn = self._get_tx_conn()
        if tx_conn is not None:
            with tx_conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
                yield cur
            return

        conn, from_pool = self._acquire_conn()
        try:
            tx_status = conn.get_transaction_status()
            if tx_status != _PG_TX_STATUS_IDLE:
                conn.rollback()
            if not conn.autocommit:
                conn.autocommit = True
            with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
                yield cur
        finally:
            self._release_conn(conn, from_pool)

    @contextmanager
    def _transaction(self):
        """Multi-statement transaction (turns off autocommit temporarily)."""
        tx_conn = self._get_tx_conn()
        if tx_conn is not None:
            with tx_conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
                yield cur
            return

        conn, from_pool = self._acquire_conn()
        old_autocommit = conn.autocommit
        committed = False
        should_sync_projection = False
        try:
            tx_status = conn.get_transaction_status()
            if tx_status != _PG_TX_STATUS_IDLE:
                conn.rollback()
            conn.autocommit = False
            self._set_tx_conn(conn)
            with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
                yield cur
            conn.commit()
            committed = True
        except Exception:
            conn.rollback()
            raise
        finally:
            should_sync_projection = committed and self._consume_projection_sync_pending()
            self._set_tx_conn(None)
            conn.autocommit = old_autocommit
            self._release_conn(conn, from_pool)
        if should_sync_projection:
            self._trigger_public_contract_projection_sync()

    def _ensure_public_contract_projection(self) -> None:
        if self._has_active_transaction():
            # Projection reconciliation is optional for request correctness and may
            # touch projection tables that are unavailable in some environments.
            # Defer it so workflow/registry mutations can validate inside the same
            # transaction without poisoning that transaction.
            self._set_projection_sync_pending(True)
            return
        try:
            from core.application.registry.public_contract_projection import (
                ensure_public_contract_projection,
            )

            ensure_public_contract_projection(self)
        except Exception as exc:
            self._clear_failed_transaction()
            logger.warning("Public contract projection ensure failed: %s", exc)

    def _trigger_public_contract_projection_sync(self) -> None:
        if self._get_tx_conn() is not None:
            self._set_projection_sync_pending(True)
            return
        if self._projection_sync_suppression_depth() > 0:
            self._set_suppressed_projection_sync_pending(True)
            return
        if self._public_projection_sync_in_progress:
            return
        conn = None
        from_pool = False
        old_autocommit = True
        lock_acquired = False
        self._public_projection_sync_in_progress = True
        try:
            conn, from_pool = self._acquire_conn()
            old_autocommit = conn.autocommit
            tx_status = conn.get_transaction_status()
            if tx_status != _PG_TX_STATUS_IDLE:
                conn.rollback()
            if not conn.autocommit:
                conn.autocommit = True
            lock_acquired = self._try_acquire_public_contract_projection_lock(conn)
            if not lock_acquired:
                logger.info(
                    "Public contract projection sync skipped because another worker holds the advisory lock"
                )
                return
            from core.application.registry.public_contract_projection import (
                reconcile_public_contract_projection,
            )

            self._set_tx_conn(conn)
            reconcile_public_contract_projection(self, apply=True)
        except Exception as exc:
            self._clear_failed_transaction()
            logger.warning("Public contract projection sync failed: %s", exc)
        finally:
            self._set_tx_conn(None)
            if conn is not None and lock_acquired:
                self._release_public_contract_projection_lock(conn)
            if conn is not None:
                conn.autocommit = old_autocommit
                self._release_conn(conn, from_pool)
            self._public_projection_sync_in_progress = False

    def _try_acquire_public_contract_projection_lock(self, conn: Any) -> bool:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s))",
                (
                    _PUBLIC_CONTRACT_PROJECTION_LOCK_DOMAIN,
                    _PUBLIC_CONTRACT_PROJECTION_LOCK_KEY,
                ),
            )
            row = cur.fetchone()
        if isinstance(row, dict):
            return bool(next(iter(row.values()), False))
        if isinstance(row, tuple):
            return bool(row[0]) if row else False
        return bool(row)

    def _release_public_contract_projection_lock(self, conn: Any) -> None:
        with suppress(Exception), conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))",
                (
                    _PUBLIC_CONTRACT_PROJECTION_LOCK_DOMAIN,
                    _PUBLIC_CONTRACT_PROJECTION_LOCK_KEY,
                ),
            )

    def _column_exists(self, table: str, column: str, *, schema: str = "public") -> bool:
        cache_key = (str(schema or "public"), str(table or ""), str(column or ""))
        cached = self._column_exists_cache.get(cache_key)
        if cached is not None:
            return cached
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = %s
                  AND column_name = %s
                """,
                (schema, table, column),
            )
            exists = cur.fetchone() is not None
            self._column_exists_cache[cache_key] = exists
            return exists

    def _column_is_generated(self, table: str, column: str, *, schema: str = "public") -> bool:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT is_generated
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = %s
                  AND column_name = %s
                """,
                (schema, table, column),
            )
            row = cur.fetchone() or {}
        if isinstance(row, dict):
            value = row.get("is_generated")
        elif row:
            value = row[0]
        else:
            value = None
        return str(value or "").strip().upper() == "ALWAYS"

    def _table_exists(self, table: str, *, schema: str = "public") -> bool:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_name = %s
                """,
                (schema, str(table or "").strip()),
            )
            return cur.fetchone() is not None

    def ensure_builder_registry_schema(self) -> None:
        if self._builder_registry_schema_ready:
            return
        self._ensure_backend()
        statements = [
            """
            ALTER TABLE public.registry_agents
            ADD COLUMN IF NOT EXISTS builder_metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL
            """,
            """
            ALTER TABLE public.registry_skills
            ADD COLUMN IF NOT EXISTS builder_metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL
            """,
            """
            ALTER TABLE public.registry_tools
            ADD COLUMN IF NOT EXISTS builder_metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL
            """,
            """
            ALTER TABLE public.registry_workflows
            ADD COLUMN IF NOT EXISTS builder_metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL
            """,
            # Provenance for the materialized agent contract: 'own' for fields
            # authored directly on the agent, 'skill:<id>' / 'tool:<id>' for fields
            # inherited from a bound skill or tool (see materialize_agent_contract).
            """
            ALTER TABLE public.registry_agent_inputs
            ADD COLUMN IF NOT EXISTS source text DEFAULT 'own' NOT NULL
            """,
            """
            ALTER TABLE public.registry_agent_outputs
            ADD COLUMN IF NOT EXISTS source text DEFAULT 'own' NOT NULL
            """,
            """
            CREATE TABLE IF NOT EXISTS public.registry_step_kind_profiles (
                step_kind_key text PRIMARY KEY,
                display_name text NOT NULL,
                description text DEFAULT ''::text NOT NULL,
                enabled boolean DEFAULT true NOT NULL,
                builder_metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
                config_schema_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
                created_at timestamp with time zone DEFAULT now() NOT NULL,
                updated_at timestamp with time zone DEFAULT now() NOT NULL
            )
            """,
        ]
        with self._cursor() as cur:
            for statement in statements:
                cur.execute(statement)
        self._column_exists_cache.clear()
        self._builder_registry_schema_ready = True

    @staticmethod
    def _extract_connection_dependency_refs(raw_binding: Any) -> list[str]:
        refs: set[str] = set()

        def _add_ref(value: Any) -> None:
            text = str(value or "").strip()
            if text:
                refs.add(text)

        def _walk(value: Any) -> None:
            if isinstance(value, dict):
                for key in (
                    "connection_id",
                    "project_connection_id",
                    "connection_key",
                    "binding_key",
                    "connection_ref",
                ):
                    _add_ref(value.get(key))
                nested = value.get("connections")
                if isinstance(nested, list):
                    for item in nested:
                        _walk(item)
                return
            if isinstance(value, list):
                for item in value:
                    _walk(item)
                return
            _add_ref(value)

        _walk(raw_binding)
        return sorted(refs)

    def _sync_workflow_runtime_dependencies(
        self,
        workflow_id: str,
        *,
        workflow_payload: dict[str, Any] | None = None,
    ) -> None:
        normalized_workflow_id = str(workflow_id or "").strip()
        if not normalized_workflow_id:
            return
        if not self._table_exists("workflow_runtime_dependencies"):
            return

        workflow_row = workflow_payload if isinstance(workflow_payload, dict) else None
        if workflow_row is None:
            workflow_row = self.get_workflow(normalized_workflow_id) or {}
        if not workflow_row:
            return

        account_id = _system_registry_account_id(workflow_row)
        scope_kind = str(workflow_row.get("scope_kind") or "system").strip() or "system"
        source_install_id = str(workflow_row.get("source_install_id") or "").strip() or None

        steps = workflow_row.get("steps", [])
        if not isinstance(steps, list):
            steps = []

        dependency_rows: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str, str]] = set()

        def _push_dependency(
            *,
            group_id: str | None,
            step_id: str,
            dependency_kind: str,
            dependency_ref: str,
            install_id: str | None = None,
            block_key: str | None = None,
        ) -> None:
            dep_kind = str(dependency_kind or "").strip()
            dep_ref = str(dependency_ref or "").strip()
            if not dep_kind or not dep_ref:
                return
            key = (str(step_id or "").strip(), dep_kind, dep_ref)
            if key in seen_keys:
                return
            seen_keys.add(key)
            dependency_rows.append(
                {
                    "account_id": account_id,
                    "scope_kind": scope_kind,
                    "workflow_id": normalized_workflow_id,
                    "group_id": str(group_id or "").strip() or None,
                    "step_id": str(step_id or "").strip(),
                    "dependency_kind": dep_kind,
                    "dependency_ref": dep_ref,
                    "install_id": str(install_id or "").strip() or None,
                    "block_key": str(block_key or "").strip() or None,
                    "required_for_builder": True,
                    "required_for_execution": True,
                }
            )

        if source_install_id:
            _push_dependency(
                group_id=None,
                step_id="",
                dependency_kind="source_install",
                dependency_ref=source_install_id,
                install_id=source_install_id,
            )

        for step in steps:
            if not isinstance(step, dict):
                continue
            step_id = str(step.get("step_id") or "").strip()
            if not step_id:
                continue
            group_id = str(step.get("group_id") or "").strip() or None

            block_key = str(step.get("block_key") or "").strip()
            if block_key:
                persisted_block_key = (
                    block_key
                    if self._exists("registry_published_block_contracts", "block_key", block_key)
                    else None
                )
                _push_dependency(
                    group_id=group_id,
                    step_id=step_id,
                    dependency_kind="published_block",
                    dependency_ref=block_key,
                    block_key=persisted_block_key,
                )

            child_workflow_id = _workflow_step_child_workflow_id(step)
            if child_workflow_id:
                _push_dependency(
                    group_id=group_id,
                    step_id=step_id,
                    dependency_kind="child_workflow",
                    dependency_ref=child_workflow_id,
                )

            override = step.get("override")
            override = dict(override) if isinstance(override, dict) else {}
            connector_config = override.get("connector")
            connector_config = dict(connector_config) if isinstance(connector_config, dict) else {}
            connector_install_id = str(
                step.get("connector_install_id")
                or connector_config.get("connector_install_id")
                or override.get("connector_install_id")
                or ""
            ).strip()
            if connector_install_id:
                _push_dependency(
                    group_id=group_id,
                    step_id=step_id,
                    dependency_kind="extension_install",
                    dependency_ref=connector_install_id,
                    install_id=connector_install_id,
                )

            operation_id = str(
                connector_config.get("operation_id")
                or connector_config.get("action_id")
                or connector_config.get("function_name")
                or ""
            ).strip()
            if connector_install_id and operation_id:
                _push_dependency(
                    group_id=group_id,
                    step_id=step_id,
                    dependency_kind="extension_action",
                    dependency_ref=operation_id,
                    install_id=connector_install_id,
                )

            connection_binding = (
                step.get("connection_binding_jsonb") or step.get("connection_binding") or {}
            )
            if not connection_binding:
                connection_binding = (
                    connector_config.get("connection_binding_jsonb")
                    or connector_config.get("connection_binding")
                    or override.get("connection_binding")
                    or {}
                )
            for connection_ref in self._extract_connection_dependency_refs(connection_binding):
                _push_dependency(
                    group_id=group_id,
                    step_id=step_id,
                    dependency_kind="project_connection",
                    dependency_ref=connection_ref,
                )

        with self._transaction() as cur:
            cur.execute(
                "DELETE FROM workflow_runtime_dependencies WHERE workflow_id = %s",
                (normalized_workflow_id,),
            )
            for row in dependency_rows:
                cur.execute(
                    """
                    INSERT INTO workflow_runtime_dependencies (
                        account_id,
                        scope_kind,
                        workflow_id,
                        group_id,
                        step_id,
                        dependency_kind,
                        dependency_ref,
                        install_id,
                        block_key,
                        required_for_builder,
                        required_for_execution
                    ) VALUES (%(account_id)s, %(scope_kind)s, %(workflow_id)s, %(group_id)s, %(step_id)s,
                              %(dependency_kind)s, %(dependency_ref)s, %(install_id)s, %(block_key)s,
                              %(required_for_builder)s, %(required_for_execution)s)
                    """,
                    row,
                )


__all__ = [
    "RegistryDBStoreConnectionMixin",
]
