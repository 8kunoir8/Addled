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

from backend import app_paths

DATA_DIR = app_paths.subdir("integrations")
CALENDAR_FILE = DATA_DIR / "calendar_events.json"


def _normalize_dt(text: str) -> str:
    """Normalize 'tomorrow 3pm' / 'friday 4pm' / ISO timestamps to an ISO
    datetime string.

    dateutil in this bundle doesn't extract bare '3 pm' times, so the time
    component is parsed explicitly and dateutil only resolves explicit
    calendar dates. Relative day words / weekdays shift the base date.

    Falls back to the raw string when parsing fails.
    """
    if not text:
        return ""
    try:
        import re as _re
        from datetime import time as _time
        from dateutil import parser as date_parser

        now = datetime.now()
        base_date = now.date()
        lower = text.lower()

        # relative day words / weekdays shift the base date
        if "day after tomorrow" in lower:
            base_date = now.date() + timedelta(days=2)
        elif "tomorrow" in lower:
            base_date = now.date() + timedelta(days=1)
        elif "yesterday" in lower:
            base_date = now.date() - timedelta(days=1)
        for i, name in enumerate(["monday", "tuesday", "wednesday",
                                  "thursday", "friday", "saturday",
                                  "sunday"]):
            if name in lower:
                delta = (i - now.weekday()) % 7 or 7
                base_date = now.date() + timedelta(days=delta)
                break

        # explicit calendar date in the text overrides the shifted base
        try:
            probe = date_parser.parse(
                text, fuzzy=True,
                default=datetime.combine(base_date, _time(9, 0)))
            base_date = probe.date()
        except Exception:
            pass

        # time component: "3pm", "3:30 pm", "15:00", noon, midnight
        h, m = 9, 0
        match = _re.search(r"(?i)\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text)
        if match:
            h = int(match.group(1)) % 24
            m = int(match.group(2) or 0)
            mer = (match.group(3) or "").lower()
            if mer == "pm" and h < 12:
                h += 12
            elif mer == "am" and h == 12:
                h = 0
        else:
            match = _re.search(r"\b(\d{1,2}):(\d{2})\b", text)
            if match:
                h, m = int(match.group(1)) % 24, int(match.group(2))
            elif "noon" in lower:
                h, m = 12, 0
            elif "midnight" in lower:
                h, m = 0, 0

        return datetime.combine(base_date, _time(h, m)).strftime(
            "%Y-%m-%d %H:%M")
    except Exception:
        return text


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
                  description: str = "", location: str = "",
                  source: str = "local", reminder_minutes: int | None = None) -> dict:
        """Add a calendar event. start/end can be ISO timestamps or relative
        like 'tomorrow 3pm' (normalized on add so range queries work)."""
        import uuid
        if reminder_minutes is None:
            try:
                from backend.config import config
                reminder_minutes = int(config.get(
                    "scheduling", "reminder_lead_min", default=10))
            except Exception:
                reminder_minutes = 10
        event = {
            "id": f"evt_{int(time.time())}_{uuid.uuid4().hex[:6]}",
            "title": title,
            "start": _normalize_dt(start),
            "raw_start": start,
            "end": _normalize_dt(end) if end else "",
            "description": description,
            "location": location,
            "source": source,
            "reminder_minutes": reminder_minutes,
            "reminded": False,
            "created_at": time.time(),
        }
        self._events.append(event)
        self._save()
        return event

    def update_event(self, event_id: str, fields: dict) -> dict | None:
        """Partial update (UI edits). start/end re-normalized when present."""
        for i, ev in enumerate(self._events):
            if ev.get("id") == event_id:
                for key, value in fields.items():
                    if key in ("start", "end"):
                        ev[key] = _normalize_dt(value) if value else ""
                    else:
                        ev[key] = value
                self._save()
                return ev
        return None

    def due_reminders(self) -> list[dict]:
        """Events starting within their reminder lead window, not yet fired."""
        now = datetime.now()
        due = []
        for ev in self._events:
            if ev.get("reminded") or not ev.get("start"):
                continue
            try:
                start = datetime.strptime(ev["start"][:16], "%Y-%m-%d %H:%M")
            except (ValueError, KeyError):
                continue
            lead = int(ev.get("reminder_minutes", 10))
            if now <= start < now + timedelta(minutes=lead):
                due.append(ev)
        return due

    def mark_reminded(self, event_id: str) -> None:
        for ev in self._events:
            if ev.get("id") == event_id:
                ev["reminded"] = True
                self._save()
                return

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

    # Google's endpoints. Named here rather than inline so the flow, the
    # exchange and the refresh all agree on them.
    GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
    GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
    GOOGLE_EVENTS_ENDPOINT = ("https://www.googleapis.com/calendar/v3/"
                              "calendars/primary/events")
    GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar.events"

    def google_auth_url(self) -> str | None:
        """Start a Google authorization and return the URL to open.

        The flow is kept on the instance (`self._flow`) because the callback
        arrives on a later request and needs the same PKCE verifier and state.

        Returns None when no client id is configured — the caller shows a
        "not configured" state rather than opening a browser at a URL Google
        will reject.
        """
        try:
            from backend.config import config
            from backend.integrations import oauth_loopback
            client_id = config.get("integrations", "google_client_id",
                                   default="")
            if not client_id:
                return None
            # Supersede any previous attempt rather than leaving its port
            # bound: connecting twice should not fail on a socket.
            previous = getattr(self, "_flow", None)
            if previous is not None:
                previous.cancel("Superseded by a newer attempt.")
            flow = oauth_loopback.LoopbackFlow()
            self._flow = flow
            return flow.authorize_url(
                self.GOOGLE_AUTH_ENDPOINT,
                client_id=client_id,
                scope=self.GOOGLE_SCOPE,
                # `offline` is what makes Google issue a refresh token, and
                # `consent` is what makes it issue one AGAIN on a repeat
                # grant — without it the second connect returns no refresh
                # token and the connection silently stops renewing.
                extra={"access_type": "offline", "prompt": "consent"},
            )
        except Exception as e:  # noqa: BLE001
            log.debug("could not build the Google auth URL: %s", e)
            return None

    def google_wait_and_sync(self, timeout: float = 300.0) -> dict:
        """Wait for the browser callback, then exchange and pull events.

        The half that was missing: nothing consumed the callback, so no code
        ever reached `sync_google`. Call this on a worker thread after opening
        the URL from `google_auth_url()`.
        """
        flow = getattr(self, "_flow", None)
        if flow is None:
            return {"events": 0,
                    "error": "No connection is in progress — start again."}
        try:
            result = flow.wait(timeout=timeout)
        finally:
            self._flow = None
        if result.get("error"):
            return {"events": 0, "error": result["error"]}
        return self.sync_google(result.get("code", ""),
                                verifier=result.get("verifier", ""),
                                redirect_uri=result.get("redirect_uri", ""))

    def sync_google(self, code: str, verifier: str = "",
                    redirect_uri: str = "") -> dict:
        """Exchange an auth code for tokens and pull Google Calendar events.

        `verifier` is the PKCE code verifier the authorization was started
        with, and `redirect_uri` must be byte-identical to the one in the
        authorize request — Google rejects the exchange otherwise. Both come
        from the flow, and both are required: the previous version sent neither
        and hand-rolled the POST, so it could not have completed even at a
        working redirect.
        """
        try:
            from backend.config import config
            from backend.integrations import oauth_loopback

            client_id = config.get("integrations", "google_client_id",
                                   default="")
            client_secret = config.get("integrations", "google_client_secret",
                                       default="")

            if not client_id:
                return {"events": 0,
                        "error": "Google credentials not configured"}
            if not verifier or not redirect_uri:
                return {"events": 0,
                        "error": ("This connection was started before the "
                                  "current flow; start it again from the "
                                  "Connect button.")}

            tokens = oauth_loopback.exchange_code(
                token_endpoint=self.GOOGLE_TOKEN_ENDPOINT,
                code=code,
                verifier=verifier,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
            )
            if tokens.get("error"):
                return {"events": 0, "error": tokens["error"]}
            if not tokens.get("access_token"):
                return {"events": 0,
                        "error": "Google returned no access token."}

            # Save tokens
            config.set("integrations", "google_tokens", value=tokens)

            # Fetch events. Uses `httpx` like the token exchange above, rather
            # than the `urllib` this used to reach for: `urllib` has no timeout
            # here, so an unresponsive Google held the calling thread for the
            # OS default — and the import was only present because the exchange
            # was hand-rolled in the same function.
            import httpx
            access_token = tokens.get("access_token", "")
            time_min = datetime.utcnow().isoformat() + "Z"
            url = (self.GOOGLE_EVENTS_ENDPOINT +
                   f"?timeMin={time_min}&maxResults=50&singleEvents=true"
                   "&orderBy=startTime")
            try:
                with httpx.Client(timeout=30.0) as client:
                    resp2 = client.get(url, headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json"})
                resp2.raise_for_status()
                gcal = resp2.json()
            except Exception as e:  # noqa: BLE001
                return {"events": 0,
                        "error": (f"Connected, but reading the calendar "
                                  f"failed: {e}")}

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
