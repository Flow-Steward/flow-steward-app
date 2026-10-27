"""HTTP error mapping for dataset and saved-query mutations."""

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException


@contextmanager
def dataset_mutation_errors() -> Iterator[None]:
    """Preserve missing-resource and rejected-mutation HTTP responses."""
    try:
        yield
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
