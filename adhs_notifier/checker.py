#!/usr/bin/env python3
"""
Monitors adhs-spezialambulanz.de for early ADHS Diagnostik Neupatienten
appointments before 15.06.2026. Sends an email when slots appear.

Setup:
  pip install -r requirements.txt
  export SMTP_USER="your_gmail@gmail.com"
  export SMTP_PASS="your_gmail_app_password"   # https://myaccount.google.com/apppasswords
  python checker.py

For automated use, run via cron or GitHub Actions (see ../.github/workflows/adhs_check.yml).
"""

import os
import re
import sys
import time
import smtplib
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────

TARGET_DATE = date(2026, 6, 15)          # only alert for slots before this date
NOTIFY_EMAIL = "jenniferbloom1993@gmail.com"
BASE_URL = "https://adhs-spezialambulanz.de"

# Pages to probe (main site + common German booking sub-pages)
CHECK_PATHS = [
    "/",
    "/termin",
    "/termine",
    "/terminvereinbarung",
    "/terminbuchung",
    "/neupatienten",
    "/kontakt",
    "/online-termin",
]

APPOINTMENT_KEYWORDS = [
    "ADHS Diagnostik",
    "Neupatienten",
    "Neupatient",
    "Termin",
    "verfügbar",
    "frei",
    "buchen",
    "Diagnostik",
    "Erstgespräch",
    "Ersttermin",
    "online",
]

GERMAN_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "april": 4,
    "mai": 5, "juni": 6, "juli": 7, "august": 8,
    "september": 9, "oktober": 10, "november": 11, "dezember": 12,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Cache-Control": "max-age=0",
}

# ── Date parsing ──────────────────────────────────────────────────────────────

def parse_dates_from_text(text: str) -> list[date]:
    """Extract all dates from page text that fall between today and TARGET_DATE."""
    found = []
    today = date.today()
    text_lower = text.lower()

    # DD.MM.YYYY  /  DD/MM/YYYY  /  DD-MM-YYYY
    for m in re.finditer(r'\b(\d{1,2})[./\-](\d{1,2})[./\-](20\d{2})\b', text):
        try:
            d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            if today <= d < TARGET_DATE:
                found.append(d)
        except ValueError:
            pass

    # YYYY-MM-DD  (ISO)
    for m in re.finditer(r'\b(20\d{2})-(\d{2})-(\d{2})\b', text):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if today <= d < TARGET_DATE:
                found.append(d)
        except ValueError:
            pass

    # "5. Juni 2026" / "5 Juni 2026"
    month_re = '|'.join(GERMAN_MONTHS.keys())
    for m in re.finditer(rf'(\d{{1,2}})\.?\s+({month_re})\s+(20\d{{2}})', text_lower):
        try:
            d = date(int(m.group(3)), GERMAN_MONTHS[m.group(2)], int(m.group(1)))
            if today <= d < TARGET_DATE:
                found.append(d)
        except ValueError:
            pass

    return sorted(set(found))


# ── HTTP fetching ─────────────────────────────────────────────────────────────

def fetch_with_requests(url: str, session: requests.Session) -> tuple[str | None, str]:
    """Return (page_text, final_url) or (None, url) on failure."""
    try:
        r = session.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            return soup.get_text(separator=" "), r.url
        print(f"    HTTP {r.status_code} for {url}")
    except Exception as e:
        print(f"    requests error for {url}: {e}")
    return None, url


def fetch_with_playwright(url: str) -> tuple[str | None, str]:
    """Fallback using Playwright for JS-heavy or Cloudflare-protected pages."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_extra_http_headers({"Accept-Language": "de-DE,de;q=0.9"})
            page.goto(url, wait_until="networkidle", timeout=30000)
            time.sleep(2)
            content = page.inner_text("body")
            final_url = page.url
            browser.close()
            return content, final_url
    except ImportError:
        pass  # playwright not installed
    except Exception as e:
        print(f"    playwright error for {url}: {e}")
    return None, url


def fetch_page(url: str, session: requests.Session) -> tuple[str | None, str]:
    text, final_url = fetch_with_requests(url, session)
    if text is None:
        text, final_url = fetch_with_playwright(url)
    return text, final_url


# ── Content analysis ──────────────────────────────────────────────────────────

def has_relevant_content(text: str) -> bool:
    lower = text.lower()
    return any(kw.lower() in lower for kw in APPOINTMENT_KEYWORDS)


def looks_like_booking_page(text: str) -> bool:
    """True if the page appears to offer actual appointment booking."""
    booking_signals = ["buchen", "reservieren", "kalender", "datum wählen", "slot", "verfügbar"]
    lower = text.lower()
    return sum(1 for s in booking_signals if s in lower) >= 2


# ── Notification ──────────────────────────────────────────────────────────────

def send_email(slots: list[date], page_url: str, smtp_user: str, smtp_pass: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "ADHS Termin verfügbar vor dem 15.06.2026!"
    msg["From"] = smtp_user
    msg["To"] = NOTIFY_EMAIL

    if slots:
        slot_str = "\n".join(f"  • {d.strftime('%d.%m.%Y')}" for d in slots)
        body = (
            f"Frühe Termine für ADHS Diagnostik Neupatienten gefunden!\n\n"
            f"Verfügbare Daten (vor dem 15.06.2026):\n{slot_str}\n\n"
            f"Jetzt prüfen und buchen:\n{page_url}\n\n"
            f"---\nAutomatische Benachrichtigung — checker.py"
        )
    else:
        body = (
            f"Die Seite für ADHS Diagnostik Neupatienten hat relevante Inhalte.\n"
            f"Bitte manuell prüfen, ob ein Termin vor dem 15.06.2026 verfügbar ist:\n\n"
            f"{page_url}\n\n"
            f"---\nAutomatische Benachrichtigung — checker.py"
        )

    msg.attach(MIMEText(body, "plain", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, NOTIFY_EMAIL, msg.as_string())
    print(f"  ✓ Email sent to {NOTIFY_EMAIL}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    today = date.today()

    if today >= TARGET_DATE:
        print("Target date passed — no longer monitoring.")
        sys.exit(0)

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
          f"Checking for appointments before {TARGET_DATE.strftime('%d.%m.%Y')} …")

    session = requests.Session()
    all_slots: list[date] = []
    alert_url = BASE_URL
    found_relevant = False
    checked = 0

    for path in CHECK_PATHS:
        url = BASE_URL + path
        text, final_url = fetch_page(url, session)
        if text is None:
            print(f"  [SKIP] {url}")
            continue

        checked += 1
        relevant = has_relevant_content(text)
        print(f"  [{'MATCH' if relevant else 'OK   '}] {url}")

        if relevant:
            found_relevant = True
            alert_url = final_url

            slots = parse_dates_from_text(text)
            if slots:
                all_slots.extend(slots)
                print(f"         → {len(slots)} early date(s): "
                      f"{[d.strftime('%d.%m.%Y') for d in slots]}")
            elif looks_like_booking_page(text):
                print("         → Booking page detected (no explicit dates parsed)")

        time.sleep(1)  # polite crawl delay

    if checked == 0:
        print("\nAll pages returned errors — site may be using Cloudflare.")
        print("Install Playwright: pip install playwright && playwright install chromium")
        sys.exit(2)

    all_slots = sorted(set(all_slots))

    if all_slots:
        print(f"\n🎯 Early slots found: {[d.strftime('%d.%m.%Y') for d in all_slots]}")
        if smtp_user and smtp_pass:
            send_email(all_slots, alert_url, smtp_user, smtp_pass)
        else:
            print("  ⚠  Set SMTP_USER + SMTP_PASS env vars to enable email alerts.")
        sys.exit(0)

    if found_relevant:
        print(f"\n⚠  Relevant content found but no dates parsed. Manual check: {alert_url}")
        if smtp_user and smtp_pass:
            send_email([], alert_url, smtp_user, smtp_pass)
        sys.exit(0)

    print("\nNo early appointments found this run.")
    sys.exit(0)


if __name__ == "__main__":
    main()
