# Taiwan → Tokyo weekly flight report

Every Monday 08:00 (Taiwan time), this searches Google Flights for the next 12 weekends and emails you a report:

- **Outbound:** TPE or TSA → NRT or HND, **Thursday 05:00–12:00**
- **Return:** NRT or HND → TPE or TSA, **Saturday 12:00 or later**
- Nonstop only, 1 adult, economy, prices in TWD

The email has:
- 🏆 the 3 cheapest weekends
- a table of every weekend with the best total, **the change since last week (▲/▼)**, the **lowest price seen so far**, and a Google Flights link
- the top 3 flights per direction for each weekend
- 🔔 in the subject and yellow rows when a total is at or below `alert_price`
- a `flights.csv` attachment with every flight found

## Setup (about 10 minutes, one time)

### 1. Make a Gmail App Password
The script logs in to Gmail to send the email. Gmail requires an App Password for this (your normal password won't work):
1. Turn on 2-Step Verification for your Google account, if it isn't on yet.
2. Open https://myaccount.google.com/apppasswords, create one named `flight-report`, and copy the 16-character password.

### 2. Put this folder in a **private** GitHub repository
1. On github.com, create a new **private** repository (for example `flight-report`).
2. Upload all files in this folder, including the hidden `.github` folder. Use `git push`, or drag the files onto the repo page with "Add file → Upload files".

### 3. Add the secrets
In the repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Name | Value |
|---|---|
| `SMTP_USER` | your Gmail address |
| `SMTP_PASSWORD` | the 16-character App Password |
| `MAIL_TO` | where to send the report (optional; defaults to `SMTP_USER`; separate multiple addresses with commas) |

### 4. Run a test
Go to **Actions → Weekly flight report → Run workflow**. It takes about 6 minutes, and then the email should arrive. From then on it runs by itself every Monday.

## Changing the search — `config.json`

| Key | Meaning |
|---|---|
| `origins` / `destinations` | Airport codes. You could add `KHH` (Kaohsiung), for example. |
| `outbound_weekday` | `"THU"`. Can be any of `MON`…`SUN`. |
| `outbound_depart_window` | `[5, 12]` = depart from 05:00 up to (not including) 12:00 |
| `return_options` | Return days, counted from the outbound date. Saturday is on. **Set the Sunday option to `"enabled": true`** to also see Thursday→Sunday trips in the same email. |
| `weeks_ahead` | How many weekends to check (12 ≈ 3 months) |
| `max_stops` | `0` = nonstop only |
| `alert_price` | Total price in TWD that triggers 🔔 |
| `adults`, `seat`, `currency` | Passengers, cabin (`economy`, `premium-economy`, `business`), and currency |

To change the day or time it runs, edit the `cron` line in `.github/workflows/weekly-flight-report.yml`. The time is in UTC, so Taiwan time is UTC+8.

## Notes
- **Prices are two one-way fares added together.** This lets the report mix airports (for example TPE→NRT and back HND→TSA), which suits low-cost airlines like Tigerair and Peach. A full-service round-trip (EVA, China Airlines, JAL, ANA) can be cheaper than two one-ways, so click the Google Flights link to check before booking.
- TSA only has flights to **HND**, not NRT, so TSA–NRT simply shows no results.
- The data comes from the free open-source scraper [fast-flights](https://pypi.org/project/fast-flights/). There is no API key and no cost, but if Google changes its site, searches can fail until the library is updated. The email says how many searches failed. If that keeps happening, update the version in `requirements.txt`.
- Price history is saved in `data/history.json` and committed back to the repo after each run. These weekly commits also keep GitHub from pausing the schedule, which it does when a repo has no activity for 60 days.

## Running on your own PC instead (optional)
Install Python 3.12+, then run:
```
pip install -r requirements.txt
python flight_report.py --dry-run
```
With `--dry-run`, it writes `report.html` instead of sending an email. To send an email from your PC, set the environment variables `SMTP_USER`, `SMTP_PASSWORD` and `MAIL_TO`, then schedule the script with Windows Task Scheduler.
