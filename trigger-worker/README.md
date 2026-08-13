# Cloudflare two-minute trigger

This Worker is the timer and read-only data collector for the BC Parks monitor. Every two minutes it reads the canonical subscription file, stops outside the configured Pacific-time window, fetches BC Parks through Cloudflare Smart Placement, and sends the JSON to `.github/workflows/bcparks-monitor.yml`. GitHub validates availability, tracks alert state, and publishes ntfy notifications.

## Configure

1. Edit `wrangler.toml` if `GH_OWNER`, `GH_REPO`, `GH_WORKFLOW`, or `GH_REF` differs.
2. Create a fine-grained GitHub personal access token with access only to the monitor repository and **Actions: Read and write**.
3. Store the token in Cloudflare; never put it in `wrangler.toml`:

```bash
npm install
npx wrangler login
npx wrangler secret put GH_TOKEN
npm test
npm run deploy
```

Wrangler is pinned to `4.68.1`, the version used for the package's successful dry-run deployment validation.

## Verify

```bash
npm run tail
```

- The deployed Worker URL returns `{"service":"bcparks-watch-trigger","status":"ok"}`. This verifies that the Worker is deployed, but it does not perform a BC Parks request.
- Confirm that GitHub Actions shows a new `workflow_dispatch` run within two minutes.
- Run `npm run test-cron`, open the printed `/__scheduled` URL once, and confirm a GitHub run appears.

The GitHub workflow does not use its own cron: deployment testing proved that the U.S. hosted runner receives HTML instead of the public JSON. Its concurrency group still serializes duplicate Worker triggers.
