"""
YouTube Analytics v2 weekly pull written to reports/analytics_<date>.json.
"""
import datetime
import json
import os
from googleapiclient.discovery import build
from src.auth import get_credentials
from src.yt_client import get_youtube_service, get_channel_info

def get_analytics_service(creds):
    return build("youtubeAnalytics", "v2", credentials=creds)

def main():
    creds = get_credentials()
    yt_service = get_youtube_service(creds)
    ch_info = get_channel_info(yt_service)
    analytics = get_analytics_service(creds)

    today = datetime.date.today()
    start_date = (today - datetime.timedelta(days=7)).isoformat()
    end_date = today.isoformat()

    resp = analytics.reports().query(
        ids="channel==MINE",
        startDate=start_date,
        endDate=end_date,
        metrics="views,estimatedMinutesWatched,averageViewDuration,subscribersGained,subscribersLost"
    ).execute()

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    report_file = os.path.join(base_dir, "reports", f"analytics_{today.isoformat()}.json")

    report_data = {
        "channel_id": ch_info["id"],
        "channel_title": ch_info["title"],
        "start_date": start_date,
        "end_date": end_date,
        "metrics": resp
    }

    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)

    print(f"[analytics] Weekly report written to {report_file}")

if __name__ == "__main__":
    main()
