// Run:  node --test relay/worker.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import worker from "./worker.mjs";

const NOW = Date.parse("2026-11-16T05:00:00Z");   // 10:30 AM in Sri Lanka
const ENV = {
  GITHUB_TOKEN: "tok", GITHUB_REPO: "o/r", TEAM_PASSCODE: "letmein",
  ALLOWED_ORIGIN: "https://o.github.io", MAX_MANUAL_PER_DAY: "3", COOLDOWN_MINUTES: "10",
};
const minsAgo = (m) => new Date(NOW - m * 60000).toISOString();
const run = (over) => ({ status: "completed", conclusion: "success", event: "schedule",
  created_at: minsAgo(120), run_started_at: minsAgo(120), updated_at: minsAgo(100), ...over });

function setup(runs, dispatchStatus = 204) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    if (String(url).includes("/dispatches")) return new Response(null, { status: dispatchStatus });
    return new Response(JSON.stringify({ workflow_runs: runs }), { status: 200 });
  };
  return calls;
}
const post = (body, origin = ENV.ALLOWED_ORIGIN) => new Request("https://w.dev/run", {
  method: "POST", headers: { Origin: origin, "Content-Type": "application/json" }, body: JSON.stringify(body) });
const call = (req, env = ENV) => worker.fetch(req, env, {}, NOW);

test("status: idle", async () => {
  setup([run()]);
  const res = await call(new Request("https://w.dev/status"));
  const j = await res.json();
  assert.equal(res.status, 200);
  assert.equal(j.state, "idle");
  assert.equal(j.last_conclusion, "success");
  assert.equal(j.manual_runs_left, 3);
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), ENV.ALLOWED_ORIGIN);
});

test("status: running", async () => {
  setup([run({ status: "in_progress", conclusion: null, created_at: minsAgo(3), run_started_at: minsAgo(3) })]);
  const j = await (await call(new Request("https://w.dev/status"))).json();
  assert.equal(j.state, "running");
  assert.equal(j.started_at, minsAgo(3));
});

test("run: starts an update with the right passcode", async () => {
  const calls = setup([run()]);
  const res = await call(post({ passcode: "letmein" }));
  assert.equal(res.status, 202);
  const d = calls.find((c) => c.url.includes("/dispatches"));
  assert.equal(JSON.parse(d.init.body).ref, "main");
  assert.equal(d.init.headers.Authorization, "Bearer tok");
  assert.match(d.url, /\/repos\/o\/r\/actions\/workflows\/daily-news\.yml\/dispatches/);
});

test("run: wrong passcode is refused and nothing is started", async () => {
  const calls = setup([run()]);
  const res = await call(post({ passcode: "nope" }));
  assert.equal(res.status, 401);
  assert.equal((await res.json()).code, "wrong_passcode");
  assert.ok(!calls.some((c) => c.url.includes("/dispatches")));
});

test("run: refused from another website", async () => {
  const calls = setup([run()]);
  const res = await call(post({ passcode: "letmein" }, "https://evil.example"));
  assert.equal(res.status, 403);
  assert.equal((await res.json()).code, "forbidden_origin");
  assert.equal(calls.length, 0);
});

test("run: refused while an update is already running", async () => {
  setup([run({ status: "queued", conclusion: null, created_at: minsAgo(1) })]);
  const res = await call(post({ passcode: "letmein" }));
  assert.equal(res.status, 409);
  assert.equal((await res.json()).code, "already_running");
});

test("run: cooldown after a recent run", async () => {
  setup([run({ created_at: minsAgo(4) })]);
  const res = await call(post({ passcode: "letmein" }));
  assert.equal(res.status, 429);
  const j = await res.json();
  assert.equal(j.code, "cooldown");
  assert.equal(j.retry_after_minutes, 6);
  assert.match(j.error, /4 minutes ago.*try again in 6 minutes/);
});

test("run: daily limit of manual updates", async () => {
  setup([1, 2, 3].map((i) => run({ event: "workflow_dispatch", created_at: minsAgo(30 * i + 20) })));
  const res = await call(post({ passcode: "letmein" }));
  assert.equal(res.status, 429);
  const j = await res.json();
  assert.equal(j.code, "daily_limit");
  assert.match(j.error, /limit of 3 manual updates.*midnight/);
});

test("run: yesterday's manual runs don't count", async () => {
  setup([1, 2, 3].map((i) => run({ event: "workflow_dispatch", created_at: minsAgo(60 * 20 + i * 10) })));
  assert.equal((await call(post({ passcode: "letmein" }))).status, 202);
});

test("no passcode set: anyone on the site can start an update, limits still apply", async () => {
  const { TEAM_PASSCODE, ...open } = ENV;
  const calls = setup([run()]);
  const st = await (await call(new Request("https://w.dev/status"), open)).json();
  assert.equal(st.passcode_required, false);
  assert.equal((await call(post({}), open)).status, 202);
  assert.ok(calls.some((c) => c.url.includes("/dispatches")));
  setup([run({ created_at: minsAgo(3) })]);
  assert.equal((await call(post({}), open)).status, 429);              // cooldown still applies
  setup([run()]);
  assert.equal((await call(post({}, "https://evil.example"), open)).status, 403);   // origin still checked
});

test("status says a passcode is required when one is set", async () => {
  setup([run()]);
  assert.equal((await (await call(new Request("https://w.dev/status"))).json()).passcode_required, true);
});

test("not configured", async () => {
  setup([]);
  const res = await call(new Request("https://w.dev/status"), { ...ENV, GITHUB_TOKEN: "" });
  assert.equal(res.status, 500);
});

test("run: GitHub refuses the dispatch -> clear error", async () => {
  setup([run()], 403);
  const res = await call(post({ passcode: "letmein" }));
  assert.equal(res.status, 502);
  const j = await res.json();
  assert.equal(j.code, "github_error");
  assert.match(j.error, /code 403/);
});

test("run: GitHub unreachable -> clear error", async () => {
  globalThis.fetch = async () => { throw new Error("network down"); };
  const res = await call(post({ passcode: "letmein" }));
  assert.equal(res.status, 502);
  assert.equal((await res.json()).code, "github_unreachable");
});
