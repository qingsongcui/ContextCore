from datetime import date, timedelta

from fastapi import HTTPException


def upper_date_bound(value: str) -> tuple[str, str]:
    """Date-only filters include that full UTC storage day; timestamps stay exact."""
    if len(value) != 10:
        return '<=', value
    try:
        following_day = date.fromisoformat(value) + timedelta(days=1)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(status_code=422, detail='Invalid end date') from exc
    return '<', following_day.isoformat()
