"""
Calendar integration — Google Calendar OAuth + local calendar management.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("addled.integrations.calendar")

DATA_DIR = Path(__file__).parent.parent / "memory" / "integrations"
CALENDAR_FILE = DATA_DIR / "calendar_events.json"


class CalendarIntegration:
    """Local calendar with Google sync capability."""

    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._events: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not CALENDAR_FILE.exists():
            return []
        try:
            with open(CALENDAR_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self):
        with open(CALENDAR_FILE, "w", encoding="utf-8") as f:
            json.dump(self._events, f, indent=2, ensure_ascii=False)

    def add_event(self, title: str, start: str, end: str | None = None,
                  description: str = "", location: str = "", source: str = "local") -> dict:
        """Add a calendar event. start/end can be ISO timestamps or relative like 'tomorrow 3pm'."""
        import uuid
        event = {
            "id": f"evt_{int(time.time())}_{uuid.uuid4().hex[:6]}",
            "title": title,
            "start": start,
            "end": end or "",
            "description": description,
            "location": location,
            "source": source,
            "created_at": time.time(),
        }
        self._events.append(event)
        self._save()
        return event

    def get_events(self, start: str | None = None, end: str | None = None) -> list[dict]:
        """Get events, optionally filtered by date range (YYYY-MM-DD)."""
        if not start and not end:
            return sorted(self._events, key=lambda e: e.get("start", ""))

        result = []
        for ev in self._events:
            ev_start = ev.get("start", "")
            if start and ev_start < start:
                continue
            if end and ev_start > end:
                continue
            result.append(ev)
        return sorted(result, key=lambda e: e.get("start", ""))

    def get_today(self) -> list[dict]:
        today = datetime.now().strftime("%Y-%m-%d")
        return self.get_events(today, today)

    def get_week(self) -> list[dict]:
        now = datetime.now()
        week_start = now.strftime("%Y-%m-%d")
        week_end = (now + timedelta(days=7)).strftime("%Y-%m-%d")
        return self.get_events(week_start, week_end)

    def get_month(self, year: int, month: int) -> list[dict]:
        start = f"{year}-{month:02d}-01"
        if month == 12:
            end = f"{year+1}-01-01"
        else:
            end = f"{year}-{month+1:02d}-01"
        return self.get_events(start, end)

    def delete_event(self, event_id: str) -> bool:
        for i, ev in enumerate(self._events):
            if ev.get("id") == event_id:
                del self._events[i]
                self._save()
                return True
        return False

    def delete_old(self, days: int = 30):
        """Remove events older than N days."""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        self._events = [e for e in self._events if e.get("start", "") >= cutoff]
        self._save()

    def google_auth_url(self) -> str | None:
        """Get Google OAuth authorization URL (requires client ID in config)."""
        try:
            from backend.config import config
            client_id = config.get("integrations", "google_client_id", default="")
            if not client_id:
                return None
            scope = "https://www.googleapis.com/auth/calendar.events"
            redirect = "http://localhost:9876/oauth/callback"
            return (f"https://accounts.google.com/o/oauth2/v2/auth?"
                    f"client_id={client_id}&redirect_uri={redirect}"
                    f"&response_type=code&scope={scope}&access_type=offline")
        except Exception:
            return None

    def sync_google(self, code: str) -> dict:
        """Exchange auth code for tokens and pull Google Calendar events."""
        try:
            from backend.config import config
            import urllib.request
            import urllib.parse

            client_id = config.get("integrations", "google_client_id", default="")
            client_secret = config.get("integrations", "google_client_secret", default="")

            if not client_id or not client_secret:
                return {"events": 0, "error": "Google credentials not configured"}

            # Exchange code for tokens
            data = urllib.parse.urlencode({
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": "http://localhost:9876/oauth/callback",
                "grant_type": "authorization_code",
            }).encode()
            req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
            with urllib.request.urlopen(req) as resp:
                tokens = json.loads(resp.read())

            # Save tokens
            config.set("integrations", "google_tokens", value=tokens)

            # Fetch events
            access_token = tokens.get("access_token", "")
            time_min = datetime.utcnow().isoformat() + "Z"
            url = (f"https://www.googleapis.com/calendar/v3/calendars/primary/events?"
                   f"timeMin={time_min}&maxResults=50&singleEvents=true&orderBy=startTime")
            req2 = urllib.request.Request(url)
            req2.add_header("Authorization", f"Bearer {access_token}")
            with urllib.request.urlopen(req2) as resp2:
                gcal = json.loads(resp2.read())

            count = 0
            for item in gcal.get("items", []):
                title = item.get("summary", "Untitled")
                start = item.get("start", {}).get("dateTime", item.get("start", {}).get("date", ""))
                end = item.get("end", {}).get("dateTime", item.get("end", {}).get("date", ""))
                desc = item.get("description", "")
                loc = item.get("location", "")
                # Deduplicate
                exists = any(e.get("title") == title and e.get("start") == start for e in self._events)
                if not exists:
                    self.add_event(title, start, end, desc, loc, source="google")
                    count += 1

            return {"events": count, "total": len(self._events)}
        except Exception as e:
            return {"events": 0, "error": str(e)}


# Singleton
calendar = CalendarIntegration()
