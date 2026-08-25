import datetime


def now_iso() -> str:
    """Current UTC time as ISO-8601 with the 'Z' (Zulu) designator. Python's
    isoformat() renders a zero UTC offset as '+00:00'; 'Z' is the conventional
    UTC marker most non-Python consumers expect. Safe because the input is
    always UTC (timezone.utc), so isoformat always ends in '+00:00'."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
