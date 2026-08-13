import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import worker, { dispatch, monitoringWindow, prefetchAvailability } from "./index.js";

const env = {
  GH_TOKEN: "test-token",
  GH_OWNER: "example-owner",
  GH_REPO: "bcparks-watch",
  GH_WORKFLOW: "bcparks-monitor.yml",
  GH_REF: "main",
  GH_CONFIG: "config/subscriptions.json",
};

const config = {
  visit_date: "2026-08-15",
  stop_after_local_time: "23:59",
  subscriptions: [
    { enabled: true, park_id: "0007", facility: "Rubble Creek" },
    { enabled: false, park_id: "0363", facility: "Joffre Lakes" },
  ],
};

function jsonResponse(payload, contentType = "application/json") {
  return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": contentType } });
}

test("dispatch calls the configured GitHub workflow with prefetched input", async () => {
  let captured;
  await dispatch(env, { availability_payload: "{\"ok\":true}" }, async (url, options) => {
    captured = { url, options };
    return new Response(null, { status: 204 });
  });

  assert.equal(
    captured.url,
    "https://api.github.com/repos/example-owner/bcparks-watch/actions/workflows/bcparks-monitor.yml/dispatches",
  );
  assert.equal(captured.options.method, "POST");
  assert.equal(captured.options.headers.Authorization, "Bearer test-token");
  assert.deepEqual(JSON.parse(captured.options.body), {
    ref: "main",
    inputs: { availability_payload: "{\"ok\":true}" },
  });
});

test("dispatch rejects missing configuration", async () => {
  await assert.rejects(() => dispatch({}), /Missing Worker configuration/);
});

test("dispatch surfaces GitHub API errors", async () => {
  await assert.rejects(
    () => dispatch(env, {}, async () => new Response("Bad credentials", { status: 401 })),
    /401 Bad credentials/,
  );
});

test("monitoring window stops after the configured Pacific cutoff", () => {
  assert.equal(monitoringWindow(config, new Date("2026-08-16T06:58:00Z")).active, true);
  assert.equal(monitoringWindow(config, new Date("2026-08-16T07:00:00Z")).active, false);
});

test("prefetch reads BC Parks at the edge and omits disabled locations", async () => {
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    if (String(url).includes("raw.githubusercontent.com")) return jsonResponse(config, "text/plain");
    if (String(url).includes("/facility?")) {
      return jsonResponse([{ name: "Rubble Creek", status: { state: "open" }, visible: true }]);
    }
    if (String(url).includes("/reservation?")) {
      return jsonResponse({ "2026-08-15": { DAY: { capacity: "Full", max: 0 } } });
    }
    throw new Error(`Unexpected URL: ${url}`);
  };

  const result = await prefetchAvailability(env, fetchImpl, new Date("2026-08-13T18:00:00Z"));
  assert.equal(result.active, true);
  assert.equal(result.payload.locations.length, 1);
  assert.equal(result.payload.locations[0].park_id, "0007");
  assert.equal(result.payload.locations[0].reservation["2026-08-15"].DAY.max, 0);
  const reservationCall = calls.find((call) => call.url.includes("/reservation?"));
  assert.equal(reservationCall.options.headers.Referer, "https://reserve.bcparks.ca/dayuse/registration");
  assert.equal(reservationCall.options.headers.Origin, "https://reserve.bcparks.ca");
});

test("prefetch sends no BC Parks request after cutoff", async () => {
  const calls = [];
  const result = await prefetchAvailability(env, async (url) => {
    calls.push(String(url));
    return jsonResponse(config, "text/plain");
  }, new Date("2026-08-16T07:00:00Z"));
  assert.equal(result.active, false);
  assert.equal(calls.filter((url) => url.includes("reserve.bcparks.ca")).length, 0);
});

test("health endpoint does not dispatch or probe", async () => {
  const response = await worker.fetch();
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { service: "bcparks-watch-trigger", status: "ok" });
});

test("wrangler cron is configured for every two minutes with smart placement", async () => {
  const configText = await readFile(new URL("../wrangler.toml", import.meta.url), "utf8");
  assert.match(configText, /crons\s*=\s*\["\*\/2 \* \* \* \*"\]/);
  assert.match(configText, /placement\s*=\s*\{\s*mode\s*=\s*"smart"\s*\}/);
});
