# Cloudflare two-minute trigger

This Worker is the primary timer for the BC Parks monitor. A Cloudflare Cron Trigger runs every two minutes and calls GitHub's `workflow_dispatch` endpoint for `.github/workflows/bcparks-monitor.yml`. The availability check and ntfy notification still run inside GitHub Actions.

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

- The deployed Worker URL returns `{"service":"bcparks-watch-trigger","status":"ok"}`. This verifies that the Worker is deployed, but it does not validate the GitHub token.
- Confirm that GitHub Actions shows a new `workflow_dispatch` run within two minutes.
- Run `npm run test-cron`, open the printed `/__scheduled` URL once, and confirm a GitHub run appears.

The GitHub workflow also contains an offset thirty-minute schedule as a best-effort fallback. Its concurrency group serializes duplicate triggers.
