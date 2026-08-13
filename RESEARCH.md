# BC Parks day-use monitoring research

Research date: 2026-08-13 (America/Vancouver)

## Conclusion

A low-frequency, notification-only monitor is technically possible. The official Angular frontend reads unauthenticated JSON endpoints for parks, facilities, and availability. The updated package supports several enabled locations sharing one adjustable date. Automated booking is materially different: advancing from inventory selection to a hold invokes Cloudflare Turnstile, and the backend validates the token. This package therefore monitors and alerts only; booking remains a manual action on the official site.

This is an undocumented internal API, not a supported public API. The monitor should be treated as provisional and best effort. Written permission from BC Parks is the cleanest policy answer before leaving it running long-term.

## Live request path

The following production paths were tested for both Garibaldi/Rubble Creek and Joffre Lakes:

```text
GET https://reserve.bcparks.ca/api/facility?park=0007&facilities=true
GET https://reserve.bcparks.ca/api/reservation?park=0007&facility=Rubble%20Creek&date=2026-08-15
GET https://reserve.bcparks.ca/api/facility?park=0363&facilities=true
GET https://reserve.bcparks.ca/api/reservation?park=0363&facility=Joffre%20Lakes&date=2026-08-15
Referer: https://reserve.bcparks.ca/dayuse/registration
Accept: application/json, text/plain, */*
```

Both tested reservation endpoints returned this sold-out response:

```json
{
  "2026-08-15": {
    "DAY": {
      "capacity": "Full",
      "max": 0
    }
  }
}
```

Rubble Creek currently exposes one `DAY` parking slot. Joffre Lakes exposes one `DAY` trail slot. The public response deliberately reduces inventory to a category (`High`, `Moderate`, `Low`, or `Full`) and a maximum number bookable in the transaction. For parking, `max` is at most one; the public code permits up to four trail passes per transaction.

The frontend and backend are public source code:

- [BC Parks day-use frontend](https://github.com/bcgov/parks-reso-public)
- [Frontend facility service](https://github.com/bcgov/parks-reso-public/blob/main/src/app/services/facility.service.ts)
- [BC Parks reservation API](https://github.com/bcgov/parks-reso-api)
- [Public reservation handler](https://github.com/bcgov/parks-reso-api/blob/main/samNode/handlers/readReservation/index.js)
- [Public capacity formatter](https://github.com/bcgov/parks-reso-api/blob/main/samNode/layers/reservationLayer/reservationLayer.js)

### Important deployed behavior

A raw request without same-site request context returned the Angular application's `200 text/html` fallback, not JSON. Supplying the registration page as `Referer` returned JSON. This constraint is not an API contract and may change. The monitor validates the content type and schema so that a routing or access-policy change fails closed instead of producing a false alert.

The first deployed GitHub Actions run also received `200 text/html` from a U.S. hosted runner even with the same-site `Referer`. Cloudflare Smart Placement received `application/json` for the identical facility and reservation requests. The production design therefore performs the read at the Cloudflare edge and passes only the matching facility fields and reservation response to GitHub for independent schema validation. The direct GitHub cron was removed because it was not a working fallback.

## How booking works

1. The registration page fetches the park's facilities.
2. It fetches each facility's public reservation window.
3. The visitor selects a date, facility, slot, and quantity.
4. Clicking **Next** opens Cloudflare Turnstile.
5. After Turnstile succeeds, the frontend sends `POST /api/pass` with `commit: false`. If inventory is still available, the backend creates a temporary hold and returns a signed token.
6. The visitor has seven minutes to submit contact details. The final `POST /api/pass` with `commit: true` converts the hold to a reservation and sends the confirmation.
7. Cancellation returns capacity to the reservation object, which is why a later GET can change from `Full/max:0` to a bookable state.

Relevant source:

- [Frontend Turnstile and hold flow](https://github.com/bcgov/parks-reso-public/blob/main/src/app/registration/facility-select/facility-select.component.ts)
- [Frontend pass service](https://github.com/bcgov/parks-reso-public/blob/main/src/app/services/pass.service.ts)
- [Backend hold and commit flow](https://github.com/bcgov/parks-reso-api/blob/main/samNode/handlers/writePass/index.js)

## Anti-bot and policy assessment

### Technical controls found

- The booking hold is protected by Cloudflare Turnstile. The backend sends the Turnstile response and source IP to Cloudflare Siteverify.
- The public backend source includes optional hostname and token-age checks, a minimum hold-age check, one-time hold-token storage, and the seven-minute hold expiry.
- The public infrastructure source puts CloudFront in front of S3 and API Gateway, logs requests, and applies geographic restrictions. No explicit WAF rate rule is visible in the public template; production may have controls that are not represented there.
- The public availability GET did not require Turnstile during testing. The hold/booking POST did.

Cloudflare documents that Turnstile tokens are server-validated, expire after five minutes, and are single-use: [Turnstile server-side validation](https://developers.cloudflare.com/turnstile/get-started/server-side-validation/).

### Written policy found

- BC Parks explicitly says passes open at 7:00 a.m. PT two days before the visit and that cancelled passes can become available again: [BC Parks day-use passes](https://bcparks.ca/reservations/day-use-passes/).
- `https://reserve.bcparks.ca/robots.txt` did not provide robots directives during testing; it returned the SPA index with `X-Robots-Tag: allow`. That header concerns indexing and is not permission to automate reservations.
- No site-specific published rule expressly authorizing cancellation polling or expressly banning a low-frequency availability read was found in the day-use FAQ, day-use information page, footer disclaimer, or the two public repositories.
- The Province's open-data API terms do not automatically authorize this endpoint. The reservation endpoint is not documented as part of the [BC Parks Data API](https://open.canada.ca/data/en/dataset/fb1c834b-5a59-44f4-8ed9-6585e826f88d).

Absence of a published prohibition is not affirmative permission. Turnstile on the hold path is a clear signal that automated booking and CAPTCHA bypass are out of bounds. If BC Parks adds authentication, a CAPTCHA, 401/403, or sustained 429 responses to the read path, the compliant response is to stop—not evade it. BC Parks contact details are published at [Contact BC Parks](https://bcparks.ca/faq/).

### Recommended operating envelope

- Prefer one park, one facility, one date, every 10 minutes when urgency permits. The replicated deployment uses the requested two-minute interval; written permission is advisable for repeated use at that cadence.
- Run only from the release time until the visit date; disable immediately after booking.
- Identify the client with a descriptive user agent.
- Use bounded retry and honor rate limiting.
- Read availability only. Do not call the hold or booking endpoint, automate Turnstile, rotate IPs, or spoof identities.
- Ask BC Parks for written approval if this will be used repeatedly or shared with others.

## Test evidence

The following checks were completed against production without holding inventory:

- Browser flow: Garibaldi → Rubble Creek → 2026-08-15 displayed `ALL DAY`, `Pass availability - Full`, with the selector disabled.
- `GET /api/park`: returned Garibaldi Provincial Park as `0007` and Joffre Lakes Provincial Park as `0363`, both open and visible.
- `GET /api/config`: returned production config with booking hour 7, parking limit 1, and a production Turnstile site key.
- `GET /api/facility`: confirmed Rubble Creek and Joffre Lakes exact names, `DAY` slots, two-day booking windows, 7 a.m. openings, facility status, and weekday rules.
- Both `GET /api/reservation` calls returned `200 application/json` and `Full/max:0` for 2026-08-15.
- Multi-location integration run: one monitor invocation checked both enabled subscriptions and reported Rubble Creek and Joffre Lakes independently as `full`, with no transport or schema error.
- Repeated live probe: five of five end-to-end monitor runs returned valid JSON and the same `Full/max:0` result; zero transport, schema, or parsing failures occurred.
- Access-path negative test: omitting same-site `Referer`/`Origin` returned `200 text/html`; the monitor rejects it.
- Twenty offline Python tests cover multi-location shared-date configuration, Cloudflare payload validation, safe notification links, rejection of per-location dates, enable/disable behavior, the exact local-time cutoff, valid JSON, HTML fallback, network retry, full, available, contradictory response signals, Rubble Creek weekday rules, ntfy actions, transition deduplication, dry-run behavior, and stale-state pruning.
- Eight offline Worker tests cover edge prefetching, same-site request headers, disabled locations, the Pacific cutoff, the exact GitHub dispatch input, missing configuration, GitHub API errors, the non-dispatching health endpoint, Smart Placement, and the exact two-minute cron setting.
- `wrangler 4.68.1 deploy --dry-run` successfully parsed the Worker module, Smart Placement, and `wrangler.toml` deployment configuration.
- Deployed Cloudflare cron run `#5` prefetched the production JSON at `2026-08-13T18:24:21Z`; GitHub independently validated Rubble Creek as `Full/max:0` and completed successfully.
- Network cutoff integration test: with a deliberately unreachable API base, a same-day configuration whose stop time had passed exited successfully with `no request sent`. This verifies the cutoff occurs before any endpoint access.
- Live browser link test: opening `https://reserve.bcparks.ca/dayuse/registration` directly redirected to `https://reserve.bcparks.ca/dayuse/`. The frontend source confirms that the registration page requires in-memory router state, so there is no stable park-selected booking deep link.

No pass was held, no contact information was submitted, and no CAPTCHA was solved or bypassed.

## GitHub Actions reliability

GitHub supports scheduled workflows as often as every five minutes, but schedules run only from the default branch and can be delayed or dropped during high load. Public-repository schedules are automatically disabled after 60 days without repository activity. GitHub specifically recommends avoiding the start of the hour: [schedule event documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

The replicated deployment therefore uses a Cloudflare Cron Trigger every two minutes as the primary timer. It calls GitHub's `workflow_dispatch` API. The workflow retains an offset thirty-minute GitHub schedule as a fallback, uses a three-minute timeout, and serializes duplicate triggers with one concurrency group. It remains best effort rather than a hard real-time guarantee.

## Push notification

The workflow supports ntfy. ntfy accepts an HTTP POST and documents that an unauthenticated topic name acts like a password, so the topic must be random and stored as a GitHub secret: [ntfy publishing documentation](https://docs.ntfy.sh/publish/).

When availability first appears, the monitor sends one urgent notification with a **Book now** action to the official day-use booking entry and a park-specific **Park details** action. `state.json` suppresses repeats while the same inventory stays open and rearms after it returns to full. It never submits booking data.
