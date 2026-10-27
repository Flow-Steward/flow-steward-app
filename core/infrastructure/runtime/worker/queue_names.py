"""Canonical RabbitMQ queue naming helpers."""

from __future__ import annotations


def queue_dead_letter_queue_name(queue_name: str) -> str:
    normalized_queue_name = str(queue_name or "").strip()
    return f"{normalized_queue_name}.dead_letter" if normalized_queue_name else ""


__all__ = ["queue_dead_letter_queue_name"]
