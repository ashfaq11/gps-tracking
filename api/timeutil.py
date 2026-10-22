"""
Times are stored and sent as UTC; people see them in IST.

Every timestamp the API accepts goes through `as_utc` (schemas.UtcDatetime,
and the routers' since/until), so a client sending a naive time or another
offset can neither be misread nor make the API answer in that offset: a
naive time is taken as UTC -- the API's own convention, documented on every
endpoint -- and anything else is converted. Postgres stores TIMESTAMPTZ, so
what it holds is the instant either way; this keeps the JSON uniform
("...Z") and the in-memory backend's comparisons from mixing naive and
aware values.
"""

from datetime import datetime, timedelta, timezone

# India has no daylight saving, so a fixed offset is exact.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def ist_clock(value: datetime | str | None) -> str | None:
    """`10:42 AM IST` -- how a notification says when, to a person."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value is None:
        return None
    return as_utc(value).astimezone(IST).strftime("%I:%M %p IST").lstrip("0")
