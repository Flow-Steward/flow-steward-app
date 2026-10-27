"""Canonical durable wait kinds and statuses, independent of state projection."""

WAIT_KIND_TIMER = "timer"
WAIT_KIND_APPROVAL = "approval"
WAIT_KIND_CHILD = "child"

STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_AWAITING_TIMER = "awaiting_timer"
STATUS_AWAITING_CHILD = "awaiting_child"

WAIT_STATUS_BY_KIND = {
    WAIT_KIND_TIMER: STATUS_AWAITING_TIMER,
    WAIT_KIND_APPROVAL: STATUS_AWAITING_APPROVAL,
    WAIT_KIND_CHILD: STATUS_AWAITING_CHILD,
}

#: Every durable waiting status: the run holds no worker and is not stuck.
DURABLE_WAIT_STATUSES = frozenset(WAIT_STATUS_BY_KIND.values())
