"""Minimal transaction status constants mirroring prior driver values."""

from __future__ import annotations

from psycopg import pq

TRANSACTION_STATUS_IDLE = int(pq.TransactionStatus.IDLE)
TRANSACTION_STATUS_INTRANS = int(pq.TransactionStatus.INTRANS)
TRANSACTION_STATUS_INERROR = int(pq.TransactionStatus.INERROR)
TRANSACTION_STATUS_UNKNOWN = int(pq.TransactionStatus.UNKNOWN)
