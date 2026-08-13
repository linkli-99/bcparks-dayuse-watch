// Cloudflare Cron Trigger for the BC Parks availability GitHub workflow.
// GH_TOKEN is a fine-grained PAT stored with `wrangler secret put GH_TOKEN`.

const REQUIRED_ENV = ["GH_TOKEN", "GH_OWNER", "GH_REPO", "GH_WORKFLOW", "GH_REF"];

function validateEnv(env) {
  const missing = REQUIRED_ENV.filter((name) => !env[name]);
  if (missing.length) throw new Error(`Missing Worker configuration: ${missing.join(", ")}`);
}

export async function dispatch(env, fetchImpl = fetch) {
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
    body: JSON.stringify({ ref: env.GH_REF }),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`GitHub workflow_dispatch failed: ${response.status} ${body}`.trim());
  }
  if (response.status !== 204) {
    throw new Error(`GitHub workflow_dispatch returned unexpected HTTP ${response.status}`);
  }
}

export default {
  async scheduled(_event, env, _ctx) {
    await dispatch(env);
  },

  async fetch() {
    return Response.json({ service: "bcparks-watch-trigger", status: "ok" });
  },
};
