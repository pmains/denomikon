"""helper module."""

from datetime import date
from typing import Optional

def _parse_date(val) -> Optional[date]:
    """Parse a value into a date, handling SQLite string returns."""
    if val is None:
        return None
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        try:
            return date.fromisoformat(val)
        except (ValueError, TypeError):
            return None
    return None

