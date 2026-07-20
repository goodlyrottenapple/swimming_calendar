import json
import os
from datetime import datetime, time, timedelta, timezone
import requests
from icalendar import Calendar, Event
from playwright.sync_api import sync_playwright

BASE_URL = "https://activeleicester.gladstonego.cloud/api"
ACTIVITIES_ENDPOINT = f"{BASE_URL}/search/activities/"
SESSIONS_ENDPOINT = f"{BASE_URL}/availability/V2/sessions"

SITE_ID = "ALC"
SWIM_KEYWORDS = {"swim", "swimming", "pool", "lane", "aqua", "public", "general"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:152.0) Gecko/20100101 Firefox/152.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://activeleicester.gladstonego.cloud/book",
    "X-Use-Sso": "1",
}


def get_gladstone_jwt_with_browser() -> str:
    """Spins up a headless browser, loads Gladstone Go, and extracts the generated Jwt cookie."""
    print("1. Launching browser to retrieve session token...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=HEADERS["User-Agent"])
        page = context.new_page()

        page.goto("https://activeleicester.gladstonego.cloud/book", wait_until="networkidle")
        cookies = context.cookies()
        browser.close()

        for cookie in cookies:
            if cookie["name"] == "Jwt":
                print("   ✔ Successfully acquired Jwt token.")
                return cookie["value"]

    raise RuntimeError("Failed to extract Jwt cookie via headless browser.")


def create_authenticated_session() -> requests.Session:
    jwt_token = get_gladstone_jwt_with_browser()
    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.set("Jwt", jwt_token, domain="activeleicester.gladstonego.cloud")
    session.headers["Authorization"] = f"Bearer {jwt_token}"
    return session


def get_swimming_activity_ids(session: requests.Session) -> list[str]:
    """Fetch all web-bookable activities and filter for swimming-related IDs."""
    print("2. Fetching activity list from Gladstone...")
    params = {"siteIds": SITE_ID, "webBookableOnly": "true"}

    response = session.get(ACTIVITIES_ENDPOINT, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()

    activities = data if isinstance(data, list) else data.get("data", data.get("items", []))
    print(f"   Total activities found at site '{SITE_ID}': {len(activities)}")

    swim_activity_ids = []
    for activity in activities:
        text_to_check = json.dumps(activity).lower()
        if any(keyword in text_to_check for keyword in SWIM_KEYWORDS):
            act_id = activity.get("id") or activity.get("activityId") or activity.get("code")
            if act_id:
                swim_activity_ids.append(str(act_id))

    print(f"   ✔ Filtered down to {len(swim_activity_ids)} swimming-related activity IDs.")
    return swim_activity_ids


def fetch_sessions(session: requests.Session, activity_ids: list[str], days_ahead=7) -> list[dict]:
    """Fetch session availability, querying in chunks to prevent URL length overflow."""
    if not activity_ids:
        print("   ❌ No swimming activity IDs found to query!")
        return []

    print(f"3. Fetching session availability for the next {days_ahead} days...")
    now_utc = datetime.now(timezone.utc)
    start_date = datetime.combine(now_utc.date(), time.min).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_date = datetime.combine(now_utc.date() + timedelta(days=days_ahead), time.max).strftime("%Y-%m-%dT%H:%M:%S.999Z")

    all_sessions = []
    chunk_size = 15
    for i in range(0, len(activity_ids), chunk_size):
        chunk = activity_ids[i:i + chunk_size]
        params = {
            "webBookableOnly": "true",
            "siteIds": SITE_ID,
            "activityIds": ",".join(chunk),
            "dateFrom": start_date,
            "dateTo": end_date,
        }

        response = session.get(SESSIONS_ENDPOINT, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        sessions = data if isinstance(data, list) else data.get("sessions", data.get("data", []))
        all_sessions.extend(sessions)

    print(f"   ✔ Total raw session records returned: {len(all_sessions)}")
    return all_sessions


def build_ics_calendar(sessions: list[dict]):
    """Flatten nested locations/slots and generate an iCalendar file."""
    print("4. Building iCalendar file...")
    cal = Calendar()
    cal.add("prodid", "-//Active Leicester Swimming Sync//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Active Leicester Swimming")

    events_added = 0

    for activity in sessions:
        title = activity.get("name") or activity.get("description") or "Swimming Session"
        activity_id = activity.get("id", "swim")
        web_comments = activity.get("webComments", "")

        # Unnest locations -> slots
        locations = activity.get("locations", [])
        for loc in locations:
            location_name = loc.get("locationNameToDisplay") or "Aylestone Leisure Centre"

            for slot in loc.get("slots", []):
                start_raw = slot.get("startTime")
                end_raw = slot.get("endTime")

                if not start_raw or not end_raw:
                    continue

                try:
                    start_time = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                    end_time = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                except ValueError:
                    continue

                event = Event()
                event.add("summary", title)
                event.add("dtstart", start_time)
                event.add("dtend", end_time)
                event.add("location", f"Aylestone Leisure Centre - {location_name}")

                # Build description with availability & booking link
                avail = slot.get("availability", {})
                places_in_centre = avail.get("inCentre")
                
                desc_lines = []
                if places_in_centre is not None:
                    desc_lines.append(f"Spaces Remaining: {places_in_centre}")
                if web_comments:
                    desc_lines.append(f"\n{web_comments}")
                desc_lines.append("\nBook session: https://activeleicester.gladstonego.cloud/book")
                
                event.add("description", "\n".join(desc_lines))

                # Create deterministic UID using start time and activity ID
                event.add("uid", f"gladstone-{activity_id}-{start_raw}@activeleicester")

                cal.add_component(event)
                events_added += 1

    os.makedirs("dist", exist_ok=True)
    output_path = os.path.join("dist", "swimming.ics")

    with open(output_path, "wb") as f:
        f.write(cal.to_ical())

    print(f"   ✔ Done! Successfully added {events_added} events to {output_path}.")


if __name__ == "__main__":
    http_session = create_authenticated_session()
    swimming_activity_ids = get_swimming_activity_ids(http_session)
    sessions = fetch_sessions(http_session, swimming_activity_ids, days_ahead=7)
    build_ics_calendar(sessions)