// Cloudflare Cron Trigger and Canadian-edge prefetch for the BC Parks monitor.
// GH_TOKEN is a repository-scoped fine-grained PAT stored as a Worker secret.

const REQUIRED_ENV = ["GH_TOKEN", "GH_OWNER", "GH_REPO", "GH_WORKFLOW", "GH_REF"];
const API_BASE = "https://reserve.bcparks.ca/api";
const REFERER = "https://reserve.bcparks.ca/dayuse/registration";

function validateEnv(env) {
  const missing = REQUIRED_ENV.filter((name) => !env[name]);
  if (missing.length) throw new Error(`Missing Worker configuration: ${missing.join(", ")}`);
}

function addDays(isoDate, days) {
  const value = new Date(`${isoDate}T12:00:00Z`);
  if (Number.isNaN(value.valueOf())) throw new Error("visit_date must use YYYY-MM-DD");
  value.setUTCDate(value.getUTCDate() + days);
  return value.toISOString().slice(0, 10);
}

export function pacificLocalMinute(now = new Date()) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: "America/Vancouver",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(now).filter((part) => part.type !== "literal").map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

export function monitoringWindow(config, now = new Date()) {
  const visitDate = config?.visit_date;
  const stopTime = config?.stop_after_local_time || "23:59";
  if (!/^\d{4}-\d{2}-\d{2}$/.test(visitDate || "") || !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(stopTime)) {
    throw new Error("Invalid visit_date or stop_after_local_time in subscriptions config");
  }
  const localNow = pacificLocalMinute(now);
  const starts = `${addDays(visitDate, -2)}T06:50`;
  const stops = `${visitDate}T${stopTime}`;
  return { active: localNow >= starts && localNow <= stops, localNow, starts, stops };
}

async function fetchJson(url, fetchImpl, headers = {}, attempts = 2, requireJsonType = true) {
  let lastError;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const response = await fetchImpl(url, { headers });
      const contentType = response.headers.get("content-type") || "";
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      if (requireJsonType && !contentType.toLowerCase().startsWith("application/json")) {
        throw new Error(`non-JSON response (${contentType || "missing content-type"})`);
      }
      return await response.json();
    } catch (error) {
      lastError = error;
      if (attempt < attempts) await new Promise((resolve) => setTimeout(resolve, 250 * attempt));
    }
  }
  throw lastError;
}

async function loadSubscriptions(env, fetchImpl) {
  const configPath = env.GH_CONFIG || "config/subscriptions.json";
  const url = `https://raw.githubusercontent.com/${encodeURIComponent(env.GH_OWNER)}/${encodeURIComponent(env.GH_REPO)}/${encodeURIComponent(env.GH_REF)}/${configPath}`;
  return fetchJson(url, fetchImpl, { "User-Agent": "cloudflare-bcparks-watch-trigger" }, 2, false);
}

function enabledSubscriptions(config) {
  if (!Array.isArray(config?.subscriptions)) throw new Error("subscriptions must be a JSON list");
  return config.subscriptions.filter((item) => item?.enabled !== false).map((item) => {
    const parkId = String(item?.park_id || "");
    const facility = String(item?.facility || "");
    if (!/^\d{4}$/.test(parkId) || !facility) throw new Error("Invalid enabled subscription");
    return { park_id: parkId, facility };
  });
}

export async function prefetchAvailability(env, fetchImpl = fetch, now = new Date()) {
  validateEnv(env);
  const config = await loadSubscriptions(env, fetchImpl);
  const window = monitoringWindow(config, now);
  if (!window.active) return { active: false, window };

  const headers = {
    Accept: "application/json, text/plain, */*",
    Origin: "https://reserve.bcparks.ca",
    Referer: REFERER,
    "User-Agent": "cloudflare-bcparks-read-only-monitor/1.0",
  };
  const facilitiesByPark = new Map();
  const locations = [];
  for (const subscription of enabledSubscriptions(config)) {
    let facilities = facilitiesByPark.get(subscription.park_id);
    if (!facilities) {
      const url = `${API_BASE}/facility?park=${encodeURIComponent(subscription.park_id)}&facilities=true`;
      try {
        facilities = { payload: await fetchJson(url, fetchImpl, headers) };
      } catch (error) {
        facilities = { error: String(error?.message || error) };
      }
      facilitiesByPark.set(subscription.park_id, facilities);
    }

    if (facilities.error) {
      locations.push({ ...subscription, error: `facility request: ${facilities.error}` });
      continue;
    }
    const query = new URLSearchParams({
      park: subscription.park_id,
      facility: subscription.facility,
      date: config.visit_date,
    });
    try {
      const reservation = await fetchJson(`${API_BASE}/reservation?${query}`, fetchImpl, headers);
      locations.push({ ...subscription, facilities: facilities.payload, reservation });
    } catch (error) {
      locations.push({ ...subscription, error: `reservation request: ${String(error?.message || error)}` });
    }
  }
  return {
    active: true,
    window,
    payload: {
      schema_version: 1,
      visit_date: config.visit_date,
      generated_at: new Date().toISOString(),
      locations,
    },
  };
}

export async function dispatch(env, inputs = {}, fetchImpl = fetch) {
  validateEnv(env);
  const url =
    `https://api.github.com/repos/${encodeURIComponent(env.GH_OWNER)}/` +
    `${encodeURIComponent(env.GH_REPO)}/actions/workflows/` +
    `${encodeURIComponent(env.GH_WORKFLOW)}/dispatches`;
  const response = await fetchImpl(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "cloudflare-bcparks-watch-trigger",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: env.GH_REF, inputs }),
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`GitHub workflow_dispatch failed: ${response.status} ${body}`.trim());
  }
  if (response.status !== 204) throw new Error(`GitHub workflow_dispatch returned unexpected HTTP ${response.status}`);
}

export default {
  async scheduled(_event, env, _ctx) {
    const result = await prefetchAvailability(env);
    if (!result.active) return;
    const serialized = JSON.stringify(result.payload);
    if (serialized.length > 60000) throw new Error("Prefetched availability payload is too large for workflow_dispatch");
    await dispatch(env, { availability_payload: serialized });
  },

  async fetch() {
    return Response.json({ service: "bcparks-watch-trigger", status: "ok" });
  },
};
