'use strict';
// Run actual screen handlers with isolated DOM, GPS and HTTP fixtures.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.dataset = {}; this.hidden = false;
    this.value = ''; this.checked = false; this.className = ''; this.attributes = {};
    this.style = { setProperty() {} };
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(k, v) { this.attributes[k] = v; }
  getAttribute(k) { return this.attributes[k]; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    return this.children.filter(x => x instanceof Element).flatMap(x => [
      ...(selector === 'details' ? x.tag === 'details' : x.className.split(' ').includes(selector.slice(1))) ? [x] : [],
      ...x.querySelectorAll(selector),
    ]);
  }
  focus() {} scrollTo() {} scrollIntoView() {} addEventListener() {}
}
const elements = new Map(), timers = new Map(); let nextTimer = 1;
const el = id => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
const ctx = {
  document: { getElementById: el, createElement: tag => new Element(tag),
    querySelectorAll: () => [], addEventListener() {}, documentElement: new Element() },
  window: { addEventListener() {}, isSecureContext: true },
  navigator: { geolocation: { clearWatch() {} } }, location: { search: '?sim=1' },
  innerWidth: 1200, URLSearchParams, console, Date, Map, Set, Blob,
  performance: { now: () => 0 }, requestAnimationFrame: () => 1, cancelAnimationFrame() {},
  setTimeout: fn => { fn(); return 1; }, clearTimeout() {},
  setInterval: fn => { const id = nextTimer++; timers.set(id, fn); return id; },
  clearInterval: id => timers.delete(id),
};
vm.createContext(ctx);
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
vm.runInContext(source.replace(/\ninit\(\);\s*$/, ''), ctx);
const state = vm.runInContext('state', ctx), rider = vm.runInContext('rider', ctx);
const route = (mode, minutes, endLat = 37.501) => ({
  total_minutes: minutes, arrival: '2026-10-30T09:10:00+09:00',
  legs: [{ mode, from: 'A', to: 'B', start: '2026-10-30T09:00:00+09:00',
    arrival: '2026-10-30T09:10:00+09:00', minutes,
    geometry: { type: 'LineString', coordinates: [[127, 37.5], [127, endLat]] } }],
});
const bike = route('bicycle', 10), transit = route('subway', 12);
const base = { routes: [transit, bike], nodes: {}, search_complete: true };
const mapStub = { on() {}, off() {}, removeLayer() {}, panTo() {} };
const layer = () => ({ addTo() { return this; }, clearLayers() {}, addLayer() {},
  setLatLng() {}, setLatLngs() {}, setRadius() {}, bringToFront() {},
  getElement: () => new Element(), bindTooltip() {} });
ctx.fakeMap = mapStub; ctx.fakeLayer = layer();
ctx.L = { marker: layer, circle: layer, polyline: layer, divIcon: x => x };

(async () => {
  ctx.showRoutes(base);
  assert.equal(state.result.routes.length, 2);
  assert.equal(state.currentRoute.total_minutes, 10, 'Shortest result selected first');
  assert.equal(el('searchView').hidden, true);
  state.filter = 'transit'; ctx.showRoutes(base, { reveal: false });
  assert.equal(state.currentRoute.total_minutes, 12);
  assert.equal(el('routeCards').children[0].textContent, '대중교통 경로 따로 찾아보기');
  state.extra.bicycle = { routes: [route('bicycle', 8)], nodes: {} };
  state.filter = 'all'; ctx.showRoutes(base);
  assert.equal(ctx.orderedRoutes(state.result)[0].total_minutes, 8);
  assert.equal(state.result.routes.length, 3, 'Additional searches preserve base candidates');

  state.extra = {}; state.filter = 'transit';
  ctx.showRoutes({ routes: [], nodes: {}, termination_reason: 'max_seconds', elapsed_seconds: 30 });
  assert.equal(state.currentRoute, null);
  assert.equal(el('goBar').hidden, true);
  const empty = el('routeCards').children[0];
  assert.ok(empty.children.some(x => x.textContent === '대중교통 경로 찾아보기'));
  assert.match(el('resultNotice').textContent, /30초/);

  let accept; const requests = [];
  ctx.fetch = async (url, options) => {
    requests.push([url, options]);
    if (url === '/api/routes') return new Promise(resolve => { accept = resolve; });
    return { ok: true, json: async () => ({ status: 'done', result: base }) };
  };
  state.initializing = false; state.filter = 'all'; ctx.showRoutes(base);
  const pending = el('routeForm').onsubmit({ preventDefault() {} });
  assert.equal(state.result, null, 'Old results removed before request completes');
  assert.equal(state.currentRoute, null);
  assert.equal(el('routeCards').children.length, 0);
  assert.equal(el('goBar').hidden, true);
  await el('routeForm').onsubmit({ preventDefault() {} });
  assert.equal(requests.length, 1, 'Repeated submit cannot create duplicate jobs');
  accept({ ok: true, json: async () => ({ job_id: 'test-job' }) });
  await pending;
  assert.equal(state.currentRoute.total_minutes, 10);

  // Editing or swapping an input invalidates an older autocomplete response immediately.
  const scheduled = new Map(); let response;
  ctx.setTimeout = fn => { const id = nextTimer++; scheduled.set(id, fn); return id; };
  ctx.clearTimeout = id => scheduled.delete(id);
  ctx.fetch = () => new Promise(resolve => { response = resolve; });
  el('origin').value = '이전 장소';
  const lookup = ctx.searchPlace('origin');
  el('origin').value = '새로운 장소';
  el('origin').oninput();
  response({ ok: true, json: async () => [{ name: '이전 장소', latitude: 37.5, longitude: 127 }] });
  await lookup;
  assert.equal(el('originSuggestions').hidden, true, 'Older requests cannot replace current suggestions');
  assert.equal(scheduled.size, 1);
  ctx.setPoint('origin', { name: '확정 장소', latitude: 37.5, longitude: 127 });
  assert.equal(scheduled.size, 0, 'Selecting a point cancels pending autocomplete');

  // Guide simulation uses no real geolocation permission or external service.
  vm.runInContext('map = fakeMap; routeLayer = fakeLayer;', ctx);
  state.currentRoute = bike;
  ctx.startGuide();
  assert.equal(rider.active, true); assert.equal(timers.size, 1);
  assert.match(el('guideSub').textContent, /남은 경로 거리/);
  assert.ok(!el('guideSub').textContent.includes('분'), 'GPS distance cannot replace timetable ETA');
  ctx.stopGuide();
  assert.equal(timers.size, 0); assert.equal(rider.active, false);
  state.currentRoute = route('bicycle', 1, 37.5001);
  ctx.startGuide();
  assert.equal(rider.arrived, true);
  assert.equal(timers.size, 0, 'Immediate arrival must not leak a simulation interval');
  ctx.stopGuide();

  let resolveLock, released = 0;
  ctx.navigator.wakeLock = { request: () => new Promise(resolve => { resolveLock = resolve; }) };
  rider.active = true;
  const lock = ctx.keepAwake(true);
  ctx.stopGuide();
  resolveLock({ release: async () => { released++; } });
  await lock;
  assert.equal(released, 1, 'Late screen-lock requests must release after navigation stops');
  assert.equal(rider.wake, null);
  console.log('Web update checks passed: filters, preserved routes, empty results, duplicate jobs, GPS lifecycle and wake locks.');
})().catch(e => { console.error(e); process.exitCode = 1; });
