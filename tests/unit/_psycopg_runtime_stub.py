from __future__ import annotations

import sys
import types
from types import SimpleNamespace


def install_psycopg_runtime_stub() -> None:
    """Provide a minimal psycopg_runtime surface for import-time-only unit tests.

    IMPORTANT: Only installs the stub when psycopg_runtime is genuinely absent from
    the environment.  If the real package is importable, this function is a
    no-op — shadowing a real psycopg_runtime would break other tests in the same
    session that need real DB connectivity (e.g. test_registry.py).
    """
    try:
        from core.infrastructure.db import psycopg_runtime as _real  # noqa: F401

        # Real psycopg_runtime is available — nothing to do.
        return
    except ImportError:
        pass

    psycopg_runtime_mod = sys.modules.get("psycopg_runtime") or types.ModuleType("psycopg_runtime")
    extras_mod = sys.modules.get("psycopg_runtime.extras") or types.ModuleType("psycopg_runtime.extras")
    sql_mod = sys.modules.get("psycopg_runtime.sql") or types.ModuleType("psycopg_runtime.sql")
    pool_mod = sys.modules.get("psycopg_runtime.pool") or types.ModuleType("psycopg_runtime.pool")
    extensions_mod = sys.modules.get("psycopg_runtime.extensions") or types.ModuleType(
        "psycopg_runtime.extensions"
    )

    class OperationalError(Exception):
        pass

    class PoolError(Exception):
        pass

    class ThreadedConnectionPool:
        def __init__(self, *_args, **_kwargs) -> None:
            self.minconn = None
            self.maxconn = None

        def getconn(self):
            raise RuntimeError("psycopg_runtime ThreadedConnectionPool stub cannot open real connections")

        def putconn(self, _conn, *_args, **_kwargs) -> None:
            return None

        def closeall(self) -> None:
            return None

    def _connect_stub(*_args, **_kwargs):
        raise RuntimeError("psycopg_runtime.connect stub invoked in a unit test without DB patching")

    extras_mod.RealDictCursor = getattr(extras_mod, "RealDictCursor", object)

    # ---- psycopg_runtime.sql composables ----
    # These need to support .join(), .format(), and string coercion so that
    # recording-cursor unit tests can pass SQL composable objects to execute()
    # without crashing.

    class _Composable:
        """Minimal psycopg_runtime.sql composable stub."""

        def __init__(self, value=""):
            self._value = value

        def join(self, iterable):
            parts = list(iterable)
            sep = str(self._value)
            # Use str(p) so subclass __str__ overrides (e.g. _Identifier) are respected.
            return _Composable(sep.join(str(p) for p in parts))

        def format(self, *args, **kwargs):
            try:
                s = str(self._value)
                fargs = [str(a._value) if isinstance(a, _Composable) else str(a) for a in args]
                fkwargs = {k: (str(v._value) if isinstance(v, _Composable) else str(v)) for k, v in kwargs.items()}
                return _Composable(s.format(*fargs, **fkwargs))
            except Exception:
                return _Composable(str(self._value))

        def as_string(self, _context=None):
            return str(self._value)

        def __add__(self, other):
            """Support SQL("x") + SQL("y") → SQL("xy") composable concatenation."""
            return _Composable(str(self) + str(other))

        def __radd__(self, other):
            return _Composable(str(other) + str(self))

        def __str__(self):
            return str(self._value)

        def __repr__(self):
            return f"_Composable({self._value!r})"

    class _Identifier(_Composable):
        """Identifier composable — str() returns Identifier('name') for test regex parsing."""

        def __str__(self):
            return f"Identifier('{self._value}')"

        def __repr__(self):
            return f"Identifier('{self._value}')"

    class _Literal(_Composable):
        def __str__(self):
            return f"Literal({self._value!r})"

    class _Placeholder(_Composable):
        def __init__(self, name=None):
            super().__init__("%s" if name is None else f"%({name})s")

    if not hasattr(sql_mod, "SQL"):
        sql_mod.SQL = _Composable
    if not hasattr(sql_mod, "Identifier"):
        sql_mod.Identifier = _Identifier
    if not hasattr(sql_mod, "Literal"):
        sql_mod.Literal = _Literal
    if not hasattr(sql_mod, "Placeholder"):
        sql_mod.Placeholder = _Placeholder
    if not hasattr(sql_mod, "Composed"):
        sql_mod.Composed = _Composable

    # ---- psycopg_runtime.Binary ----
    def _binary_stub(data):
        """Stub: just return the data as-is (recording cursors don't validate types)."""
        return data
    extensions_mod.TRANSACTION_STATUS_IDLE = getattr(
        extensions_mod, "TRANSACTION_STATUS_IDLE", 0
    )
    extensions_mod.TRANSACTION_STATUS_INTRANS = getattr(
        extensions_mod, "TRANSACTION_STATUS_INTRANS", 2
    )
    extensions_mod.TRANSACTION_STATUS_INERROR = getattr(
        extensions_mod, "TRANSACTION_STATUS_INERROR", 3
    )
    extensions_mod.TRANSACTION_STATUS_UNKNOWN = getattr(
        extensions_mod, "TRANSACTION_STATUS_UNKNOWN", 4
    )

    psycopg_runtime_mod.Binary = getattr(psycopg_runtime_mod, "Binary", _binary_stub)
    psycopg_runtime_mod.extras = extras_mod
    psycopg_runtime_mod.sql = sql_mod
    psycopg_runtime_mod.pool = pool_mod
    psycopg_runtime_mod.extensions = extensions_mod
    psycopg_runtime_mod.OperationalError = getattr(psycopg_runtime_mod, "OperationalError", OperationalError)
    psycopg_runtime_mod.errorcodes = getattr(
        psycopg_runtime_mod,
        "errorcodes",
        SimpleNamespace(
            UNDEFINED_TABLE="42P01",
            UNDEFINED_COLUMN="42703",
            INVALID_SCHEMA_NAME="3F000",
            FOREIGN_KEY_VIOLATION="23503",
            UNIQUE_VIOLATION="23505",
            NOT_NULL_VIOLATION="23502",
            CHECK_VIOLATION="23514",
            INTEGRITY_CONSTRAINT_VIOLATION="23000",
        ),
    )
    psycopg_runtime_mod.connect = getattr(psycopg_runtime_mod, "connect", _connect_stub)

    pool_mod.PoolError = getattr(pool_mod, "PoolError", PoolError)
    pool_mod.ThreadedConnectionPool = getattr(pool_mod, "ThreadedConnectionPool", ThreadedConnectionPool)

    sys.modules["psycopg_runtime"] = psycopg_runtime_mod
    sys.modules["psycopg_runtime.extras"] = extras_mod
    sys.modules["psycopg_runtime.sql"] = sql_mod
    sys.modules["psycopg_runtime.pool"] = pool_mod
    sys.modules["psycopg_runtime.extensions"] = extensions_mod
    sys.modules["psycopg_runtime.errorcodes"] = psycopg_runtime_mod.errorcodes
