"""Transport-independent validation errors for persisted Dataset queries."""

from dataclasses import dataclass


@dataclass
class SavedQueryContractError(ValueError):
    """A safe, transport-independent saved-query validation failure."""

    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return self.message

    def as_detail(self) -> dict[str, str]:
        return {
            "code": self.code,
            "field": self.path.split(".", 1)[0],
            "path": self.path,
            "message": self.message,
        }


def invalid_saved_query(path: str, message: str) -> SavedQueryContractError:
    return SavedQueryContractError(
        code="saved_query_definition_invalid",
        path=path,
        message=message,
    )
