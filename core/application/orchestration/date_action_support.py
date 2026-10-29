"""Strict application-owned date lexicons from the compiled UI catalogs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import lru_cache
from types import MappingProxyType

from core.application.settings import ui_i18n
from core.infrastructure.date_types import DateLexicon, DateValueError


def _locale_name(locale: object) -> str:
    name = str(locale).strip().lower()
    if name not in {row["code"] for row in ui_i18n.supported_ui_locales()}:
        raise DateValueError("unknown_locale", f"Unknown date locale: {name!r}.")
    return name


def _names(locale: str, category: str, count: int, form: str) -> tuple[str, ...]:
    # Check the actual locale catalog before translate_ui_text can merge English.
    catalog = ui_i18n._catalog_messages(locale)
    result = []
    for index in range(1, count + 1):
        key = f"date.{category}.{index}.{form}"
        if not catalog.get(key):
            raise DateValueError("unknown_locale", f"Date locale {locale!r} is missing {key}.")
        result.append(ui_i18n.translate_ui_text(key, locale=locale))
    return tuple(result)


@lru_cache(maxsize=64)
def _date_lexicon(locale: str) -> DateLexicon:
    months = tuple(
        _names(locale, "month", 12, form)
        for form in ("full", "abbr", "full_standalone", "abbr_standalone")
    )
    weekdays = tuple(_names(locale, "weekday", 7, form) for form in ("full", "abbr", "short"))
    return DateLexicon(
        locale=locale,
        months_full=months[0],
        months_abbr=months[1],
        months_full_standalone=months[2],
        months_abbr_standalone=months[3],
        weekdays_full=weekdays[0],
        weekdays_abbr=weekdays[1],
        weekdays_short=weekdays[2],
    )


def get_date_lexicon(locale: str = "en") -> DateLexicon:
    """Return an immutable complete locale, rejecting unsupported names before fallback."""
    return _date_lexicon(_locale_name(locale))


def get_date_lexicons(locales: Iterable[str] = ("en", "lt")) -> Mapping[str, DateLexicon]:
    """Supply trusted lexicons to pure dispatch; callers may request only needed locales."""
    return MappingProxyType({name: get_date_lexicon(name) for name in map(_locale_name, locales)})
