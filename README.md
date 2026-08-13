# BC Parks multi-location availability monitor

This is a read-only cancellation monitor for one target date and one or more BC Parks locations. A smart-placed Cloudflare Worker checks the public availability data every two minutes, GitHub Actions validates the response and tracks alert state, and ntfy sends a push when a cancellation appears. It never holds or books a pass.

The supplied configuration monitors Garibaldi's Rubble Creek parking for August 15, 2026. Joffre Lakes is included but disabled and can be enabled without changing the code.

## Architecture

```text
Cloudflare Cron Trigger (every 2 minutes)
  -> BC Parks read-only availability GET
  -> GitHub workflow_dispatch with the response
  -> scripts/check_availability.py validation
  -> ntfy alert when Full changes to Available
  -> state.json commit only when notification state changes
```

The BC Parks origin returned its SPA HTML fallback to a U.S. GitHub-hosted runner during deployment testing. The Worker therefore performs the read with Cloudflare Smart Placement and passes the JSON into the workflow. GitHub's own cron is intentionally disabled because it cannot perform a reliable direct read from that runner region. The workflow concurrency group prevents overlapping checks.

## Configure the date and locations

Edit `config/subscriptions.json`:

```json
{
  "visit_date": "2026-08-15",
  "stop_after_local_time": "23:59",
  "subscriptions": [
    {
      "enabled": true,
      "label": "Garibaldi - Rubble Creek parking",
      "park_id": "0007",
      "facility": "Rubble Creek",
      "slot": "DAY",
      "booking_url": "https://reserve.bcparks.ca/dayuse/",
      "park_url": "https://bcparks.ca/garibaldi-park/"
    },
    {
      "enabled": false,
      "label": "Joffre Lakes trail passes",
      "park_id": "0363",
      "facility": "Joffre Lakes",
      "slot": "DAY",
      "booking_url": "https://reserve.bcparks.ca/dayuse/",
      "park_url": "https://bcparks.ca/joffre-lakes-park/"
    }
  ]
}
```

- Every enabled location uses the single top-level `visit_date`; per-location dates are rejected.
- Set `enabled` to subscribe or unsubscribe from a location.
- `stop_after_local_time` is Pacific time. No BC Parks request is made after this time on the target date.
- Change the shared date in this file and commit it; the Worker reads this canonical configuration before each check.
- `booking_url` becomes the notification's main tap target and **Book now** action.
- `park_url` becomes a secondary **Park details** action.

Confirmed production identifiers:

| Location | Park ID | Facility | Slot |
|---|---:|---|---|
| Garibaldi – Rubble Creek parking | `0007` | `Rubble Creek` | `DAY` |
| Joffre Lakes trail passes | `0363` | `Joffre Lakes` | `DAY` |

## Booking-link limitation

BC Parks does not expose a durable URL that opens with a park already selected. The live Angular application passes the park through in-memory navigation state; opening `/dayuse/registration` directly redirects to `/dayuse/`.

For that reason, the alert opens `https://reserve.bcparks.ca/dayuse/`, the fastest reliable booking entry, where you tap **Book a Pass** for the named park. The notification itself includes the exact park, facility, date, availability, booking button, and separate park-details button.

## GitHub repository setup

Copy the whole project to a repository, including:

- `.github/workflows/bcparks-monitor.yml`
- `config/subscriptions.json`
- `scripts/check_availability.py`
- `state.json`
- `tests/test_check_availability.py`
- `trigger-worker/`

In **Settings → Secrets and variables → Actions** configure:

| Type | Name | Required | Value |
|---|---|---:|---|
| Secret | `NTFY_TOPIC` | Yes | A random, unguessable 8–64 character ntfy topic |
| Secret | `NTFY_TOKEN` | No | Token for an authenticated/self-hosted ntfy server |
| Variable | `NTFY_SERVER` | No | Defaults to `https://ntfy.sh` |

The workflow requests `contents: write` only so it can commit `state.json`. It writes state only when an alert is sent, availability closes and rearms the alert, or the configured date/locations make an old state entry obsolete.

Enable Actions, then run **BC Parks availability monitor → Run workflow → Send an ntfy test notification**. This tests ntfy without calling BC Parks.

## Cloudflare setup

The included `trigger-worker/wrangler.toml` currently targets:

```toml
GH_OWNER = "linkli-99"
GH_REPO = "bcparks-dayuse-watch"
GH_WORKFLOW = "bcparks-monitor.yml"
GH_REF = "main"
```

Change these values if your destination repository differs. Then create a fine-grained GitHub PAT with access only to that repository and **Actions: Read and write** permission:

```bash
cd trigger-worker
npm install
npx wrangler login
npx wrangler secret put GH_TOKEN
npm test
npm run deploy
npm run tail
```

Within two minutes, GitHub Actions should show a successful run whose event is `workflow_dispatch`. See `trigger-worker/README.md` for verification details.

## Test locally

```bash
python3 -m unittest discover -s tests -v
BCPARKS_FORCE_CHECK=true BCPARKS_DRY_RUN=true python3 scripts/check_availability.py
cd trigger-worker && npm test
```

`BCPARKS_DRY_RUN=true` prints a would-be notification without contacting ntfy or changing `state.json`.

## Reliability and safety

- Validates HTTP status, JSON content type, response shape, date, slot, and consistency between `capacity` and `max`.
- Treats the site's `200 text/html` fallback as an error, not as availability.
- Uses bounded retry for network errors, HTTP 429, and transient 5xx responses.
- Continues checking other locations when one fails and reports a non-zero partial-error result.
- Makes no BC Parks request before the release window or after the configured cutoff.
- Sends one alert per availability transition. It rearms only after the location becomes full again.
- Reads availability only. If the endpoint begins returning authentication, CAPTCHA, sustained rate limiting, or a different schema, stop and review the policy rather than working around it.

The availability endpoint is undocumented and has no service-level agreement. Two-minute monitoring is substantially more aggressive than the original ten-minute research recommendation; written permission from BC Parks remains the cleanest policy basis for repeated use.

See [RESEARCH.md](RESEARCH.md) for endpoint testing, booking-flow analysis, and anti-bot findings.
