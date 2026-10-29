'use strict';
const fs = require('node:fs'),
  path = require('node:path'),
  vm = require('node:vm'),
  assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const elements = new Map();
const context = {
  state: { activeJob: null, jobPending: false, initializing: false, points: {} },
  $: (id) => {
    if (!elements.has(id)) elements.set(id, {});
    return elements.get(id);
  },
  busy: () => {},
  status: () => {},
  orderedRoutes: (r) => r.routes,
  setTimeout: (fn) => { fn(); return 0; },
  setPoint: (key, value) => { context.state.points[key] = value; },
  setMode: (mode) => { context.state.mode = mode; },
  kstLocal: () => '2026-10-30T17:00',
  showRoutes: () => {},
  setSheet: () => {},
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('async function runJob('), source.indexOf("$('routeForm').onsubmit")), context);
vm.runInContext(source.slice(source.indexOf('async function resumeRoute('), source.indexOf('function labelFor(')), context);

(async () => {
  const requests = [], partials = [], toggles = [];
  const result = { routes: [{ total_minutes: 10 }] };
  context.busy = (on) => toggles.push(on);
  context.api = async (url) => {
    requests.push(url);
    return { status: 'done', result };
  };
  const restored = await context.runJob(null, null, 'status', 'working', (r) => partials.push(r), {
    job_id: 'same-job', status: 'running', revision: 3, partial_result: result,
  });
  assert.equal(restored, result);
  assert.deepEqual(requests, ['/api/jobs/same-job']);
  assert.deepEqual(partials, [result]);
  assert.deepEqual(toggles, [true, false]);
  assert.equal(context.state.activeJob, null);
  assert.equal(context.state.jobPending, false);

  requests.length = 0;
  await context.runJob(null, null, 'status', 'working', null, { job_id: 'completed', status: 'done', result });
  assert.equal(requests.length, 0, 'A completed job must be shown without resubmission or polling');

  let accept;
  context.api = async (url) => {
    requests.push(url);
    if (url === '/api/routes') return new Promise((resolve) => { accept = resolve; });
    return { status: 'done', result };
  };
  const first = context.runJob('/api/routes', {}, 'status', 'working');
  assert.equal(context.state.jobPending, true);
  await assert.rejects(() => context.runJob('/api/routes', {}, 'status', 'working'), /진행 중/);
  assert.deepEqual(requests, ['/api/routes']);
  accept({ job_id: 'accepted' });
  await first;
  assert.equal(requests.filter((url) => url === '/api/routes').length, 1);

  context.api = async () => { throw new Error('No network request expected'); };
  const shown = [];
  context.showRoutes = (r) => shown.push(r);
  await context.resumeRoute({
    job_id: 'restored-finished', status: 'done', result,
    route_query: {
      origin: { name: '녹천역', latitude: 37.644, longitude: 127.052 },
      destination: { name: '중계역', latitude: 37.644, longitude: 127.064 },
      mode: 'transit', departure: '2026-10-30T17:00:00+09:00',
      use_kma: false, personalize: true,
    },
  });
  assert.equal(context.state.points.origin.name, '녹천역');
  assert.equal(context.state.points.destination.name, '중계역');
  assert.equal(context.state.mode, 'transit');
  assert.equal(elements.get('departureMode').value, 'scheduled');
  assert.equal(elements.get('departure').hidden, false);
  assert.equal(elements.get('useWeather').checked, false);
  assert.equal(elements.get('usePersonal').checked, true);
  assert.deepEqual(shown, [result]);
  const failed = { routes: [], termination_reason: 'max_seconds', elapsed_seconds: 30.001,
    api_usage: { new_api_calls: 20, max_calls: 20 } };
  const outcome = context.routeOutcome(failed);
  assert.equal(outcome.error, true);
  assert.match(outcome.message, /30초/);
  assert.match(outcome.message, /20\/20회/);
  const messages = [];
  context.status = (id, message, error) => messages.push({ message, error });
  await context.runJob(null, null, 'jobStatus', 'working', null, { job_id: 'empty', status: 'done', result: failed });
  assert.equal(messages.at(-1).error, true);
  let cleared = 0;
  context.routeLayer = { clearLayers: () => cleared++ };
  context.clearRouteDisplay('표시할 경로가 없습니다.');
  assert.equal(cleared, 1);
  assert.equal(elements.get('routePersonalNote').hidden, true);
  assert.equal(elements.get('mapCaption').textContent, '표시할 경로가 없습니다.');
  process.stdout.write('Route resume checks passed: existing job polling, completed result, duplicate-click guard, query restoration.\n');
})().catch((error) => { console.error(error); process.exitCode = 1; });
