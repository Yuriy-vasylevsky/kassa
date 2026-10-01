"""Report calendar uses Kyiv time, independently of the host timezone."""

from datetime import datetime
from zoneinfo import ZoneInfo


def report_day(now: float | None = None) -> str:
    zone = ZoneInfo("Europe/Kyiv")
    return (datetime.now(zone) if now is None else datetime.fromtimestamp(now, zone)).date().isoformat()
