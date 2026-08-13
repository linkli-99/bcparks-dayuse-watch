import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import worker, { dispatch } from "./index.js";

const env = {
  GH_TOKEN: "test-token",
  GH_OWNER: "example-owner",
  GH_REPO: "bcparks-watch",
  GH_WORKFLOW: "bcparks-monitor.yml",
  GH_REF: "main",
};

test("dispatch calls the configured GitHub workflow", async () => {
  let captured;
  await dispatch(env, async (url, options) => {
    captured = { url, options };
    return new Response(null, { status: 204 });
  });

  assert.equal(
    captured.url,
    "https://api.github.com/repos/example-owner/bcparks-watch/actions/workflows/bcparks-monitor.yml/dispatches",
  );
  assert.equal(captured.options.method, "POST");
  assert.equal(captured.options.headers.Authorization, "Bearer test-token");
  assert.deepEqual(JSON.parse(captured.options.body), { ref: "main" });
});

test("dispatch rejects missing configuration", async () => {
  await assert.rejects(() => dispatch({}), /Missing Worker configuration/);
});

test("dispatch surfaces GitHub API errors", async () => {
  await assert.rejects(
    () => dispatch(env, async () => new Response("Bad credentials", { status: 401 })),
    /401 Bad credentials/,
  );
});

test("health endpoint does not dispatch a workflow", async () => {
  const response = await worker.fetch();
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { service: "bcparks-watch-trigger", status: "ok" });
});

test("wrangler cron is configured for every two minutes", async () => {
  const config = await readFile(new URL("../wrangler.toml", import.meta.url), "utf8");
  assert.match(config, /crons\s*=\s*\["\*\/2 \* \* \* \*"\]/);
});
