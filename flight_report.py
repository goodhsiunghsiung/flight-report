#!/usr/bin/env python3
"""Weekly Taiwan -> Tokyo flight price report, delivered by email.

Searches Google Flights (via the `fast-flights` scraper) for one-way legs:
  outbound  : TPE/TSA -> NRT/HND on a Thursday morning
  return    : NRT/HND -> TPE/TSA on the Saturday afternoon/evening after
then combines the cheapest legs per weekend, compares with last week's prices,
and emails an HTML report.

Usage:
  python flight_report.py            # search + send email (needs SMTP_* env vars)
  python flight_report.py --dry-run  # search + write report.html only, no email
"""

from __future__ import annotations

import csv
import io
import json
import os
import random
import smtplib
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, get_flights

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
HISTORY_FILE = ROOT / "data" / "history.json"
REPORT_FILE = ROOT / "report.html"
TZ = ZoneInfo("Asia/Taipei")
WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


@dataclass
class Leg:
    price: int
    airlines: str
    origin: str
    dest: str
    dep_date: str
    dep_time: str
    arr_time: str
    stops: int

    def label(self) -> str:
        stops = "nonstop" if self.stops == 0 else f"{self.stops} stop"
        return f"{self.origin} {self.dep_time} → {self.dest} {self.arr_time} · {self.airlines} · {stops}"


@dataclass
class Weekend:
    out_date: date
    ret_label: str
    ret_date: date
    out_legs: list[Leg]
    ret_legs: list[Leg]
    errors: list[str]

    @property
    def best_out(self) -> Leg | None:
        return self.out_legs[0] if self.out_legs else None

    @property
    def best_ret(self) -> Leg | None:
        return self.ret_legs[0] if self.ret_legs else None

    @property
    def total(self) -> int | None:
        if self.best_out and self.best_ret:
            return self.best_out.price + self.best_ret.price
        return None

    @property
    def key(self) -> str:
        return f"{self.out_date.isoformat()}_{self.ret_label}"


# ---------------------------------------------------------------- searching

def upcoming_outbound_dates(today: date) -> list[date]:
    weekday = WEEKDAYS.index(CONFIG["outbound_weekday"].upper())
    days = (weekday - today.weekday()) % 7 or 7  # never today
    first = today + timedelta(days=days)
    return [first + timedelta(weeks=i) for i in range(CONFIG["weeks_ahead"])]


def search_leg(day: date, origin: str, dest: str, window: list[int]) -> list[Leg]:
    lo, hi = window
    query = create_query(
        flights=[
            FlightQuery(
                date=day.isoformat(),
                from_airport=origin,
                to_airport=dest,
                earliest_departure_hour=lo if lo > 0 else None,
                latest_departure_hour=hi if hi < 24 else None,
            )
        ],
        trip="one-way",
        seat=CONFIG["seat"],
        passengers=Passengers(adults=CONFIG["adults"]),
        currency=CONFIG["currency"],
        language="en",
        max_stops=CONFIG["max_stops"],
    )

    results = None
    for attempt in range(3):
        try:
            results = get_flights(query)
            break
        except FlightsNotFound:
            return []
        except Exception:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))

    legs = []
    for f in results or []:
        if not f.price or not f.flights:
            continue
        first, last = f.flights[0], f.flights[-1]
        dep_h, dep_m = first.departure.time
        # Google's hour filter is loose; enforce the window strictly here.
        if not (lo * 60 <= dep_h * 60 + dep_m < hi * 60):
            continue
        if tuple(first.departure.date) != (day.year, day.month, day.day):
            continue
        arr_h, arr_m = last.arrival.time
        legs.append(
            Leg(
                price=int(f.price),
                airlines=", ".join(f.airlines) or "?",
                origin=first.from_airport.code,
                dest=last.to_airport.code,
                dep_date=day.isoformat(),
                dep_time=f"{dep_h:02d}:{dep_m:02d}",
                arr_time=f"{arr_h:02d}:{arr_m:02d}",
                stops=len(f.flights) - 1,
            )
        )
    return legs


def polite_pause() -> None:
    lo, hi = CONFIG["request_delay_seconds"]
    time.sleep(random.uniform(lo, hi))


def dedupe_sorted(legs: list[Leg]) -> list[Leg]:
    seen, out = set(), []
    for leg in sorted(legs, key=lambda l: l.price):
        k = (leg.origin, leg.dest, leg.dep_time, leg.airlines)
        if k not in seen:
            seen.add(k)
            out.append(leg)
    return out


def search_all(today: date) -> list[Weekend]:
    origins, dests = CONFIG["origins"], CONFIG["destinations"]
    returns = [r for r in CONFIG["return_options"] if r.get("enabled", True)]
    weekends = []

    for out_date in upcoming_outbound_dates(today):
        out_legs, out_errors = [], []
        for o in origins:
            for d in dests:
                try:
                    out_legs += search_leg(out_date, o, d, CONFIG["outbound_depart_window"])
                except Exception as e:
                    out_errors.append(f"{o}→{d} {out_date}: {type(e).__name__}")
                polite_pause()
        out_legs = dedupe_sorted(out_legs)

        for r in returns:
            ret_date = out_date + timedelta(days=r["days_after_outbound"])
            ret_legs, errors = [], list(out_errors)
            for d in dests:
                for o in origins:
                    try:
                        ret_legs += search_leg(ret_date, d, o, r["depart_window"])
                    except Exception as e:
                        errors.append(f"{d}→{o} {ret_date}: {type(e).__name__}")
                    polite_pause()
            weekends.append(
                Weekend(out_date, r["label"], ret_date, out_legs, dedupe_sorted(ret_legs), errors)
            )
            print(f"{out_date} / {r['label']} {ret_date}: "
                  f"{len(out_legs)} out, {len(ret_legs)} back, total={weekends[-1].total}", flush=True)
    return weekends


def google_flights_link(w: Weekend) -> str:
    out, ret = w.best_out, w.best_ret
    if not (out and ret):
        return ""
    same_airports = out.origin == ret.dest and out.dest == ret.origin
    query = create_query(
        flights=[
            FlightQuery(date=w.out_date.isoformat(), from_airport=out.origin, to_airport=out.dest),
            FlightQuery(date=w.ret_date.isoformat(), from_airport=ret.origin, to_airport=ret.dest),
        ],
        trip="round-trip" if same_airports else "multi-city",
        seat=CONFIG["seat"],
        passengers=Passengers(adults=CONFIG["adults"]),
        currency=CONFIG["currency"],
        language="en",
    )
    return query.url()


# ---------------------------------------------------------------- history

def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return {}


def update_history(history: dict, weekends: list[Weekend], today: date) -> dict:
    """Returns {key: (previous_price, lowest_seen)} computed before adding today's run."""
    stats = {}
    for w in weekends:
        runs = [r for r in history.get(w.key, []) if r["run"] != today.isoformat()]
        prev = runs[-1]["price"] if runs else None
        lowest = min((r["price"] for r in runs), default=None)
        stats[w.key] = (prev, lowest)
        if w.total is not None:
            runs.append({"run": today.isoformat(), "price": w.total})
        history[w.key] = runs

    # drop weekends that are already in the past
    cutoff = today.isoformat()
    for k in [k for k in history if k.split("_")[0] < cutoff]:
        del history[k]

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=1, sort_keys=True), encoding="utf-8")
    return stats


# ---------------------------------------------------------------- report

def money(v: int | None) -> str:
    if v is None:
        return "—"
    prefix = "NT$" if CONFIG["currency"] == "TWD" else CONFIG["currency"] + " "
    return f"{prefix}{v:,}"


def delta_html(now: int | None, prev: int | None) -> str:
    if now is None or prev is None:
        return '<span style="color:#888">new</span>'
    d = now - prev
    if d == 0:
        return '<span style="color:#888">±0</span>'
    color, arrow = ("#1a7f37", "▼") if d < 0 else ("#cf222e", "▲")
    return f'<span style="color:{color}">{arrow} {money(abs(d))}</span>'


def build_html(weekends: list[Weekend], stats: dict, today: date) -> str:
    alert = CONFIG["alert_price"]
    priced = [w for w in weekends if w.total is not None]
    cheapest = sorted(priced, key=lambda w: w.total)[:3]

    td = 'style="padding:6px 10px;border-bottom:1px solid #eee;vertical-align:top"'
    th = 'style="padding:6px 10px;text-align:left;background:#f3f4f6;font-weight:600"'

    def dates(w: Weekend) -> str:
        return f"{w.out_date:%a %b %d} → {w.ret_date:%a %b %d}"

    parts = [
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1f2328;max-width:900px">',
        f'<h2 style="margin:0 0 4px">✈️ Taiwan → Tokyo weekend flights</h2>',
        f'<div style="color:#666;margin-bottom:16px">Report of {today:%Y-%m-%d} · '
        f'{"/".join(CONFIG["origins"])} ⇄ {"/".join(CONFIG["destinations"])} · '
        f'{CONFIG["outbound_weekday"].title()} depart {CONFIG["outbound_depart_window"][0]:02d}:00–'
        f'{CONFIG["outbound_depart_window"][1]:02d}:00 · {CONFIG["adults"]} adult · {CONFIG["seat"]}'
        f'{" · nonstop only" if CONFIG["max_stops"] == 0 else ""}</div>',
    ]

    if cheapest:
        parts.append('<h3 style="margin:16px 0 8px">🏆 Best weekends</h3><ol style="margin:0;padding-left:20px">')
        for w in cheapest:
            badge = " 🔔" if w.total <= alert else ""
            parts.append(f"<li><b>{money(w.total)}</b>{badge} — {dates(w)}</li>")
        parts.append("</ol>")

    parts.append(
        f'<h3 style="margin:20px 0 8px">📅 All weekends</h3>'
        f'<table style="border-collapse:collapse;font-size:14px;width:100%"><tr>'
        f"<th {th}>Dates</th><th {th}>Best total</th><th {th}>vs last week</th>"
        f"<th {th}>Lowest seen</th><th {th}>Outbound</th><th {th}>Return</th><th {th}></th></tr>"
    )
    for w in weekends:
        prev, lowest = stats.get(w.key, (None, None))
        hl = ' style="background:#fff8c5"' if w.total is not None and w.total <= alert else ""
        link = google_flights_link(w)
        link_html = f'<a href="{escape(link)}">Google Flights</a>' if link else ""
        out =f"{escape(w.best_out.label())}<br><small>{money(w.best_out.price)}</small>" if w.best_out else "no flights"
        ret = f"{escape(w.best_ret.label())}<br><small>{money(w.best_ret.price)}</small>" if w.best_ret else "no flights"
        if w.errors:
            ret += f'<br><small style="color:#cf222e">⚠ {len(w.errors)} search(es) failed</small>'
        parts.append(
            f"<tr{hl}><td {td}><b>{dates(w)}</b></td><td {td}><b>{money(w.total)}</b></td>"
            f"<td {td}>{delta_html(w.total, prev)}</td><td {td}>{money(lowest)}</td>"
            f"<td {td}>{out}</td><td {td}>{ret}</td>"
            f"<td {td}>{link_html}</td></tr>"
        )
    parts.append("</table>")

    n = CONFIG["top_n_per_direction"]
    parts.append(f'<h3 style="margin:24px 0 8px">🔎 Top {n} options per weekend</h3>')
    for w in weekends:
        if not (w.out_legs or w.ret_legs):
            continue
        parts.append(f'<div style="margin:12px 0 4px;font-weight:600">{dates(w)}</div>'
                     f'<table style="border-collapse:collapse;font-size:13px">')
        for name, legs in (("Out", w.out_legs[:n]), ("Back", w.ret_legs[:n])):
            for leg in legs:
                parts.append(f"<tr><td {td}>{name}</td><td {td}>{money(leg.price)}</td>"
                             f"<td {td}>{escape(leg.label())}</td></tr>")
        parts.append("</table>")

    errors = sum(len(w.errors) for w in weekends)
    parts.append(
        '<p style="color:#666;font-size:12px;margin-top:24px">'
        "Prices are the sum of two one-way fares from Google Flights for the cheapest legs "
        "(airports may be mixed, e.g. TPE→NRT / HND→TSA). A full-service round-trip ticket can "
        "sometimes be cheaper — open the Google Flights link to compare. "
        f"🔔 / yellow rows = total at or below {money(alert)}."
        + (f" <b>{errors} search(es) failed this run.</b>" if errors else "")
        + "</p></div>"
    )
    return "\n".join(parts)


def build_csv(weekends: list[Weekend]) -> bytes:
    buf = io.StringIO()
    fields = list(Leg.__dataclass_fields__)
    writer = csv.DictWriter(buf, fieldnames=["weekend", "direction", *fields])
    writer.writeheader()
    for w in weekends:
        for direction, legs in (("out", w.out_legs), ("back", w.ret_legs)):
            for leg in legs:
                writer.writerow({"weekend": w.key, "direction": direction, **asdict(leg)})
    return buf.getvalue().encode("utf-8-sig")


def subject_line(weekends: list[Weekend]) -> str:
    priced = [w for w in weekends if w.total is not None]
    if not priced:
        return "✈️ TW→Tokyo weekly: no prices found (check the run)"
    best = min(priced, key=lambda w: w.total)
    bell = "🔔 " if best.total <= CONFIG["alert_price"] else ""
    return (f"{bell}✈️ TW→Tokyo weekly: best {money(best.total)} "
            f"({best.out_date:%b %d}–{best.ret_date:%b %d})")


def send_email(subject: str, html: str, csv_bytes: bytes) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    to = os.environ.get("MAIL_TO") or user

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText(html, "html", "utf-8"))
    attachment = MIMEApplication(csv_bytes, Name="flights.csv")
    attachment["Content-Disposition"] = 'attachment; filename="flights.csv"'
    msg.attach(attachment)

    with smtplib.SMTP_SSL(host, port) as smtp:
        smtp.login(user, password)
        smtp.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    print(f"Email sent to {to}")


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    today = datetime.now(TZ).date()

    weekends = search_all(today)
    stats = update_history(load_history(), weekends, today)
    html = build_html(weekends, stats, today)
    REPORT_FILE.write_text(html, encoding="utf-8")
    subject = subject_line(weekends)
    print(subject)

    if dry_run or not os.environ.get("SMTP_USER"):
        print(f"Dry run: report written to {REPORT_FILE}")
    else:
        send_email(subject, html, build_csv(weekends))
    return 0


if __name__ == "__main__":
    sys.exit(main())
