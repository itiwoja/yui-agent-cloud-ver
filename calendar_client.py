"""Google Calendarから今日の予定を読み取る。予定の作成・更新は行わない。"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from tasks_client import get_credentials

JST = ZoneInfo("Asia/Tokyo")


def _service():
    return build("calendar", "v3", credentials=get_credentials(), cache_discovery=False)


def get_today_events() -> list[dict[str, str]]:
    """primaryカレンダーのJST当日0時から24時までの予定を返す。"""
    today = datetime.now(JST).date()
    start_of_day = datetime.combine(today, time.min, tzinfo=JST)
    end_of_day = start_of_day + timedelta(days=1)

    result = (
        _service()
        .events()
        .list(
            calendarId="primary",
            timeMin=start_of_day.isoformat(),
            timeMax=end_of_day.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    return [
        {
            "summary": event.get("summary", "（タイトルなし）"),
            "start": event.get("start", {}).get("dateTime")
            or event.get("start", {}).get("date", ""),
            "end": event.get("end", {}).get("dateTime")
            or event.get("end", {}).get("date", ""),
        }
        for event in result.get("items", [])
        if event.get("status") != "cancelled"
    ]
