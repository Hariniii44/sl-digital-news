// Cloudflare Worker: lets the team start the "Daily news update" workflow from the website.
//
// The GitHub token lives here (as a secret), never in the web page. The page sends a team
// passcode (if one is set); the Worker checks it, applies safety limits, then asks GitHub to
// run the workflow. If TEAM_PASSCODE is not set, no passcode is asked for.
//
//   GET  /status   -> {state: "idle"|"running", started_at, last_finished_at, last_conclusion,
//                      manual_runs_left, passcode_required}
//   POST /run      -> body {"passcode": "..."} (only needed if a passcode is set); starts an update
//                     (202), or explains why not.
//
// Settings (see relay/README in the main README):
//   secrets: GITHUB_TOKEN, and optionally TEAM_PASSCODE
//   vars:    GITHUB_REPO ("owner/name"), ALLOWED_ORIGIN ("https://owner.github.io"),
//            GITHUB_BRANCH (default "main"), MAX_MANUAL_PER_DAY (default 3), COOLDOWN_MINUTES (default 10)

const WORKFLOW = "daily-news.yml";
const ACTIVE = new Set(["queued", "in_progress", "waiting", "pending", "requested"]);
const COLOMBO_OFFSET_MS = 5.5 * 3600 * 1000;

function reply(data, status, cors) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...cors },
  });
}

function github(env, path, init = {}) {
  return fetch(`https://api.github.com${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "news-refresh-relay",
      ...(init.headers || {}),
    },
  });
}

// Compare two secrets without leaking where they differ.
async function sameSecret(a, b) {
  const enc = new TextEncoder();
  const [x, y] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(String(a))),
    crypto.subtle.digest("SHA-256", enc.encode(String(b))),
  ]);
  const p = new Uint8Array(x), q = new Uint8Array(y);
  let diff = 0;
  for (let i = 0; i < p.length; i++) diff |= p[i] ^ q[i];
  return diff === 0;
}

function startOfColomboDay(now = Date.now()) {
  const t = new Date(now + COLOMBO_OFFSET_MS);
  t.setUTCHours(0, 0, 0, 0);
  return t.getTime() - COLOMBO_OFFSET_MS;
}

async function recentRuns(env) {
  const r = await github(env, `/repos/${env.GITHUB_REPO}/actions/workflows/${WORKFLOW}/runs?per_page=30`);
  if (!r.ok) throw new Error(`GitHub said ${r.status}`);
  return (await r.json()).workflow_runs || [];
}

function summarise(runs, env, now) {
  const active = runs.find((r) => ACTIVE.has(r.status));
  const finished = runs.find((r) => r.status === "completed");
  const max = Number(env.MAX_MANUAL_PER_DAY || 3);
  const dayStart = startOfColomboDay(now);
  const usedToday = runs.filter((r) => r.event === "workflow_dispatch" && Date.parse(r.created_at) >= dayStart).length;
  return {
    state: active ? "running" : "idle",
    started_at: active ? active.run_started_at || active.created_at : null,
    last_finished_at: finished ? finished.updated_at : null,
    last_conclusion: finished ? finished.conclusion : null,
    manual_runs_left: Math.max(0, max - usedToday),
    passcode_required: Boolean(env.TEAM_PASSCODE),
    _active: active,
    _latest: runs[0],
  };
}

export default {
  async fetch(request, env, ctx, now = Date.now()) {
    const origin = request.headers.get("Origin") || "";
    const cors = {
      "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN || "",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Access-Control-Max-Age": "600",
      Vary: "Origin",
    };
    const url = new URL(request.url);

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) {
      return reply({ code: "not_configured", error: "The update service is not set up yet. Please tell the maintainer." }, 500, cors);
    }

    try {
      if (request.method === "GET" && url.pathname === "/status") {
        const s = summarise(await recentRuns(env), env, now);
        const { _active, _latest, ...pub } = s;
        return reply(pub, 200, cors);
      }

      if (request.method === "POST" && url.pathname === "/run") {
        if (origin !== env.ALLOWED_ORIGIN) return reply({ code: "forbidden_origin", error: "This page is not allowed to start updates." }, 403, cors);

        let body = {};
        try { body = await request.json(); } catch { /* handled below */ }
        if (env.TEAM_PASSCODE && !(await sameSecret(body.passcode ?? "", env.TEAM_PASSCODE))) {
          await new Promise((r) => setTimeout(r, 800));   // slows down guessing
          return reply({ code: "wrong_passcode", error: "Wrong passcode." }, 401, cors);
        }

        const runs = await recentRuns(env);
        const s = summarise(runs, env, now);
        if (s._active) {
          return reply({ code: "already_running", error: "An update is already running.", state: "running", started_at: s.started_at }, 409, cors);
        }
        const cooldown = Number(env.COOLDOWN_MINUTES || 10);
        const last = runs[0] ? Date.parse(runs[0].created_at) : 0;
        const minsAgo = Math.floor((now - last) / 60000);
        if (last && minsAgo < cooldown) {
          const wait = Math.max(1, cooldown - minsAgo);
          return reply({
            code: "cooldown", retry_after_minutes: wait,
            error: `The news was updated ${minsAgo < 1 ? "less than a minute" : `${minsAgo} minute${minsAgo === 1 ? "" : "s"}`} ago. Please try again in ${wait} minute${wait === 1 ? "" : "s"}.`,
          }, 429, cors);
        }
        if (s.manual_runs_left <= 0) {
          const max = Number(env.MAX_MANUAL_PER_DAY || 3);
          return reply({
            code: "daily_limit",
            error: `Today's limit of ${max} manual update${max === 1 ? "" : "s"} has been used. It resets at midnight (Sri Lanka time), and the news also updates automatically every morning.`,
          }, 429, cors);
        }

        const d = await github(env, `/repos/${env.GITHUB_REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
          method: "POST",
          body: JSON.stringify({ ref: env.GITHUB_BRANCH || "main" }),
        });
        if (d.status !== 204) return reply({ code: "github_error", error: `GitHub did not accept the update request (code ${d.status}). Please try again in a minute, or tell the maintainer if it keeps happening.` }, 502, cors);
        return reply({ ok: true, manual_runs_left: s.manual_runs_left - 1 }, 202, cors);
      }

      return reply({ code: "not_found", error: "Not found." }, 404, cors);
    } catch (err) {
      return reply({ code: "github_unreachable", error: "Could not reach GitHub. Please try again in a minute." }, 502, cors);
    }
  },
};
