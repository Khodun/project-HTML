#!/usr/bin/env python3
"""
Monitors adhs-spezialambulanz.de for early ADHS Diagnostik Neupatienten
appointments before 15.06.2026. Sends an email via Brevo (free SMTP service)
when a slot appears.

Exit codes:
  0 = appointment (or booking page) found  → GitHub Actions also creates an issue
  1 = nothing found yet                    → no notification
  2 = site completely unreachable

Setup (local or GitHub Actions secrets):
  SMTP_USER  = your Brevo login email  (free account at brevo.com)
  SMTP_PASS  = Brevo SMTP key          (Brevo dashboard → SMTP & API → Generate SMTP key)
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

TARGET_DATE  = date(2026, 6, 15)
NOTIFY_EMAIL = "jenniferbloom1993@gmail.com"
BASE_URL     = "https://adhs-spezialambulanz.de"

# Brevo free SMTP — no 2FA, no app-password dance
SMTP_HOST = "smtp-relay.brevo.com"
SMTP_PORT = 587

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
    found = []
    today = date.today()
    text_lower = text.lower()

    for m in re.finditer(r'\b(\d{1,2})[./\-](\d{1,2})[./\-](20\d{2})\b', text):
        try:
            d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            if today <= d < TARGET_DATE:
                found.append(d)
        except ValueError:
            pass

    for m in re.finditer(r'\b(20\d{2})-(\d{2})-(\d{2})\b', text):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if today <= d < TARGET_DATE:
                found.append(d)
        except ValueError:
            pass

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
        pass
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
    signals = ["buchen", "reservieren", "kalender", "datum wählen", "slot", "verfügbar"]
    lower = text.lower()
    return sum(1 for s in signals if s in lower) >= 2

# ── Email notification ────────────────────────────────────────────────────────

def send_email(slots: list[date], page_url: str, smtp_user: str, smtp_pass: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "ADHS Termin verfügbar vor dem 15.06.2026!"
    msg["From"]    = smtp_user
    msg["To"]      = NOTIFY_EMAIL

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

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, NOTIFY_EMAIL, msg.as_string())

    print(f"  ✓ Email sent to {NOTIFY_EMAIL}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    today = date.today()

    if os.environ.get("TEST_MODE", "").lower() in ("1", "true"):
        print("TEST MODE — sending a test alert without checking the website.")
        if smtp_user and smtp_pass:
            send_email([], BASE_URL + " [TEST — ignore this]", smtp_user, smtp_pass)
        else:
            print("  ⚠  Set SMTP_USER + SMTP_PASS to test email alerts.")
        sys.exit(0)   # triggers the GitHub issue step too

    if today >= TARGET_DATE:
        print("Target date passed — no longer monitoring.")
        sys.exit(1)

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

        time.sleep(1)

    if checked == 0:
        print("\nAll pages blocked — site likely uses Cloudflare.")
        print("Install Playwright: pip install playwright && playwright install chromium")
        sys.exit(2)

    all_slots = sorted(set(all_slots))

    if all_slots:
        print(f"\nEarly slots found: {[d.strftime('%d.%m.%Y') for d in all_slots]}")
        if smtp_user and smtp_pass:
            send_email(all_slots, alert_url, smtp_user, smtp_pass)
        else:
            print("  ⚠  Set SMTP_USER + SMTP_PASS to enable email alerts.")
        sys.exit(0)

    if found_relevant:
        print(f"\nBooking page found but no explicit dates. Check manually: {alert_url}")
        if smtp_user and smtp_pass:
            send_email([], alert_url, smtp_user, smtp_pass)
        sys.exit(0)

    print("\nNo early appointments found this run.")
    sys.exit(1)


if __name__ == "__main__":
    main()
