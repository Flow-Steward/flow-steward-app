"""Canonical durable wait kinds and statuses, independent of state projection."""

WAIT_KIND_TIMER = "timer"
WAIT_KIND_APPROVAL = "approval"
WAIT_KIND_CHILD = "child"
WAIT_KIND_EVENT = "event"

STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_AWAITING_TIMER = "awaiting_timer"
STATUS_AWAITING_CHILD = "awaiting_child"
STATUS_AWAITING_EVENT = "awaiting_event"

WAIT_STATUS_BY_KIND = {
    WAIT_KIND_TIMER: STATUS_AWAITING_TIMER,
    WAIT_KIND_APPROVAL: STATUS_AWAITING_APPROVAL,
    WAIT_KIND_CHILD: STATUS_AWAITING_CHILD,
    WAIT_KIND_EVENT: STATUS_AWAITING_EVENT,
}

#: Every durable waiting status: the run holds no worker and is not stuck.
DURABLE_WAIT_STATUSES = frozenset(WAIT_STATUS_BY_KIND.values())

# --- Event waits (ADR 0071) -------------------------------------------------

#: A correlation key is compared as exact text; longer keys are refused.
WAIT_EVENT_CORRELATION_KEY_MAX = 256
#: How many event types one wait may accept.
WAIT_EVENT_TYPES_MAX = 20
#: The largest event payload a wait accepts, as compact JSON bytes.
WAIT_EVENT_PAYLOAD_MAX_BYTES = 64 * 1024
#: Delivered events are kept this long, consumed or not; within it a key and an
#: event type are delivered once.
WAIT_EVENT_RETENTION_DAYS = 7
#: The most ``resume_waits`` items one extension response may carry.
WAIT_EVENT_ITEMS_MAX = 100

_EVENT_TYPE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_.:-")


def wait_event_type_is_valid(value: object) -> bool:
    """``^[a-z0-9][a-z0-9_.:-]{0,127}$``: an event type a wait can name."""
    if not isinstance(value, str) or not 0 < len(value) <= 128:
        return False
    return value[0].isalnum() and value[0].isascii() and set(value) <= _EVENT_TYPE_CHARS


def wait_event_source_is_valid(value: object) -> bool:
    """A canonical extension id (``acme.crawler``): the extension that delivers."""
    from core.application.extensions.identity import is_canonical_extension_id

    return isinstance(value, str) and is_canonical_extension_id(value) and value == value.strip()


def wait_event_correlation_key(value: object) -> str | None:
    """The exact text a correlation key compares by, or ``None`` when it is not one.

    Text and integers are keys; booleans, floats, objects, empty text, text with
    control characters and text over the limit are not.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    if not value.strip() or len(value) > WAIT_EVENT_CORRELATION_KEY_MAX:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value
