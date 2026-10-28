from datetime import datetime, timezone

import pytest
from icalendar import Calendar

from test_reader import calendar, event
from yandex_calendar_mcp.recurrence import check_complexity


@pytest.mark.parametrize("rule", ["FREQ=SECONDLY", "FREQ=MINUTELY", "FREQ=HOURLY;BYSECOND=" + ",".join(str(x) for x in range(60))])
def test_dense_rules_rejected_before_expansion(rule):
    cal = Calendar.from_ical(calendar(event("UID:limit\nDTSTART:20261028T100000Z\nDTEND:20261028T110000Z\nRRULE:" + rule)))
    with pytest.raises(ValueError):
        check_complexity(cal, datetime(2026, 10, 28, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc))


def test_normal_daily_rule_allowed():
    cal = Calendar.from_ical(calendar(event("UID:ok\nDTSTART:20261028T100000Z\nDTEND:20261028T110000Z\nRRULE:FREQ=DAILY;COUNT=5")))
    check_complexity(cal, datetime(2026, 10, 28, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc))
