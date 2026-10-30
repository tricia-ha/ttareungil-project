'use strict';
const $ = (id) => document.getElementById(id);
const state = {
  mode: 'integrated',
  session: {},
  points: {
    origin: { name: '홍익대학교 서울캠퍼스', latitude: 37.550874837441, longitude: 126.925554591431 },
    destination: { name: '건국대학교 서울캠퍼스', latitude: 37.543536094587516, longitude: 127.07741635877292 },
  },
  markers: {},
  picking: null,
  result: null,
  draft: null,
  file: null,
  image: null,
  objectUrl: null,
  activeJob: null,
  jobPending: false,
  initializing: true,
  photoEpoch: 0,
  photoEditorOpen: false,
  photoRetryPhase: 'recognize',
  manualMode: false,
  stations: new Map(),
  sheet: 'mid',
  filter: 'all',
  base: null,
  extra: {},
  searched: new Set(),
  lastQuery: null,
};
const modeNames = { integrated: '통합', transit: '대중교통', bicycle: '따릉이' };
const names = {
  walk: '도보',
  bicycle: '따릉이',
  bus: '버스',
  subway: '지하철',
  wait: '대기',
  alight: '하차',
};
const colors = { walk: '#778272', bicycle: '#4cc40a', bus: '#1183b1', subway: '#7953ae' };
let map = null,
  routeLayer = null,
  toastTimer;
function node(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
function notify(text) {
  $('toast').textContent = text;
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($('toast').hidden = true), 4500);
}
function status(id, text, error = false, busy = false) {
  const n = $(id);
  n.textContent = text;
  n.className = 'status' + (error ? ' error' : '') + (busy ? ' busy' : '');
}
async function api(path, options = {}) {
  const response = await fetch(path, { credentials: 'same-origin', ...options });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error('서버 응답을 확인할 수 없습니다. 서버가 실행 중인지 확인해 주세요.');
  }
  if (!response.ok)
    throw new Error(typeof data.detail === 'string' ? data.detail : '요청을 처리하지 못했습니다.');
  return data;
}
const jsonRequest = (method, data) => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(data),
});
function kstLocal(iso) {
  const d = new Date(iso);
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Seoul',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
    .format(d)
    .replace(' ', 'T');
}
function clock(iso) {
  if (!iso) return '';
  return new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(iso));
}
function minute(v) {
  return Number.isFinite(+v) ? Math.round(+v * 10) / 10 : '—';
}
function setSheet(name) {
  state.sheet = name;
  const sheetH = name === 'full' ? '92dvh' : name === 'peek' ? '132px' : '62dvh';
  $('sheet').style.setProperty('--sheet-h', sheetH);
  document.documentElement.style.setProperty('--sheet-h', sheetH);
  setTimeout(() => map?.invalidateSize(), 330);
}
function fitMap() {
  if (!map) return;
  const layers = routeLayer?.getLayers().length ? [routeLayer] : Object.values(state.markers);
  if (!layers.length) return;
  const bounds = L.featureGroup(layers).getBounds();
  if (bounds.isValid())
    map.fitBounds(bounds, {
      maxZoom: 16,
      paddingTopLeft: [30, 100],
      paddingBottomRight: [30, innerWidth <= 800 ? Math.min(innerHeight * 0.62, 500) : 30],
    });
}
function initMap() {
  if (!window.L) {
    $('mapCaption').textContent = '지도를 불러오지 못했습니다. 네트워크를 확인해 주세요.';
    return;
  }
  map = L.map('map', { zoomControl: innerWidth > 800 }).setView([37.552, 126.955], 12);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '© OpenStreetMap contributors',
  }).addTo(map);
  routeLayer = L.featureGroup().addTo(map);
  map.on('click', (e) => {
    if (!state.picking) return;
    setPoint(state.picking, {
      latitude: e.latlng.lat,
      longitude: e.latlng.lng,
      name: '지도에서 선택한 위치',
    });
    state.picking = null;
    $('mapWrap').classList.remove('picking');
    $('pickHint').textContent = '선택한 핀을 끌어서 위치를 조정할 수 있습니다.';
    setSheet('mid');
  });
  renderMarkers();
}
function renderMarkers() {
  if (!map) return;
  for (const key of ['origin', 'destination']) {
    if (state.markers[key]) map.removeLayer(state.markers[key]);
    delete state.markers[key];
    const p = state.points[key];
    if (!p) continue;
    const marker = L.marker([p.latitude, p.longitude], {
      draggable: true,
      title: key === 'origin' ? '출발' : '도착',
    }).addTo(map);
    marker.bindTooltip(node('span', '', (key === 'origin' ? '출발 · ' : '도착 · ') + p.name), {
      direction: 'top',
    });
    marker.on('dragend', () => {
      const v = marker.getLatLng();
      setPoint(key, { latitude: v.lat, longitude: v.lng, name: '지도에서 조정한 위치' });
    });
    state.markers[key] = marker;
  }
}
function setPoint(key, p) {
  if (!(p.latitude > 37 && p.latitude < 38.2 && p.longitude > 126.3 && p.longitude < 127.8)) {
    notify('서울·수도권의 위치를 선택해 주세요.');
    return;
  }
  state.points[key] = { name: p.name, latitude: p.latitude, longitude: p.longitude };
  $(key).value = p.name;
  cancelPlaceSearch(key);
  renderMarkers();
}
const placeSeq = { origin: 0, destination: 0 };
function cancelPlaceSearch(key) {
  clearTimeout(placeTimers[key]);
  placeSeq[key]++;
  $(key + 'Suggestions').hidden = true;
}
async function searchPlace(key, { silent = false } = {}) {
  const q = $(key).value.trim();
  const box = $(key + 'Suggestions');
  const seq = ++placeSeq[key];
  if (q.length < 2) {
    if (!silent) return notify('장소 이름을 두 글자 이상 입력해 주세요.');
    box.hidden = true;
    return;
  }
  if (!silent) {
    box.replaceChildren(node('li', '', '검색 중…'));
    box.hidden = false;
  }
  try {
    const values = await api('/api/places?q=' + encodeURIComponent(q));
    if (seq !== placeSeq[key]) return;
    box.hidden = false;
    box.replaceChildren();
    if (!values.length) {
      box.append(node('li', 'hint', '검색 결과가 없습니다. 지도에서 선택해 주세요.'));
      return;
    }
    for (const p of values) {
      const li = node('li');
      const b = node('button', '', p.name + (p.address ? ' · ' + p.address : ''));
      b.type = 'button';
      b.onclick = () => {
        setPoint(key, p);
        fitMap();
      };
      li.append(b);
      box.append(li);
    }
  } catch (e) {
    if (seq !== placeSeq[key]) return;
    box.hidden = true;
    if (!silent) notify(e.message);
  }
}
function setMode(mode) {
  state.mode = mode;
  document
    .querySelectorAll('[data-mode]')
    .forEach((b) => b.setAttribute('aria-selected', String(b.dataset.mode === mode)));
  $('bikeOptions').hidden = mode === 'transit';
  $('searchButton').textContent = modeNames[mode] + ' 경로 찾기 ↗';
}
function showView(name) {
  const results = name === 'results';
  state.view = name;
  $('searchView').hidden = results;
  $('resultNav').hidden = !results;
  $('results').hidden = !results;
  $('routesPanel').classList.toggle('view-results', results);
  if (results) $('routeSummary').textContent = `${$('origin').value} → ${$('destination').value}`;
  $('sheet').scrollTo({ top: 0 });
}
$('backToSearch').onclick = () => {
  showView('search');
  if (innerWidth <= 800) setSheet('mid');
};
function showPanel(personal) {
  $('personalPanel').hidden = !personal;
  $('routesPanel').hidden = personal;
  $('navRoutes').classList.toggle('active', !personal);
  $('navPersonal').classList.toggle('active', personal);
  setSheet(personal ? 'full' : 'mid');
  if (personal) refreshAccount().catch((e) => status('personalStatus', e.message, true));
}
function busy(on) {
  on = on || state.initializing;
  $('searchButton').disabled = on;
  $('recognizeButton').disabled = on;
  $('confirmButton').disabled = on;
  document.querySelectorAll('[data-mode]').forEach((b) => (b.disabled = on));
  for (const id of [
    'screenshotFile',
    'ocrRetry',
    'ocrRecrop',
    'ocrChooseAnother',
    'ocrManual',
    'cropTop',
    'cropBottom',
  ])
    $(id).disabled = on;
}
async function runJob(path, options, target, working, onPartial = null, existingJob = null) {
  if (state.activeJob || state.jobPending) throw new Error('진행 중인 작업이 끝난 뒤 다시 시도해 주세요.');
  state.jobPending = true;
  busy(true);
  let id;
  try {
    const value = existingJob || await api(path, options);
    id = value.job_id;
    state.activeJob = id;
    status(target, '요청을 접수했습니다. PC 자원에 여유가 생기면 탐색을 시작합니다.', false, true);
    const began = Date.now();
    let revision = 0;
    let snapshot = existingJob;
    for (;;) {
      let j;
      if (snapshot) {
        j = snapshot;
        snapshot = null;
      } else {
        await new Promise((r) => setTimeout(r, 1100));
        try {
          j = await api('/api/jobs/' + id);
        } catch (e) {
          if (Date.now() - began < 10000) continue;
          throw e;
        }
      }
      if (onPartial && j.partial_result && j.revision > revision) {
        revision = j.revision;
        onPartial(j.partial_result);
      }
      if (j.status === 'done') {
        const outcome = routeOutcome(j.result);
        status(target, outcome.message, outcome.error);
        return j.result;
      }
      if (j.status === 'error') throw new Error(j.error);
      status(
        target,
        onPartial && j.progress
          ? routeProgressText(j, working)
          : j.status === 'queued'
          ? '다른 작업 또는 PC 자원 때문에 대기 중입니다. 잠시 기다려 주세요.'
          : onPartial && j.partial_result
            ? `${orderedRoutes(j.partial_result).length}개 경로를 찾았습니다. 더 짧은 경로를 확인하고 있습니다.`
            : working,
        false,
        true,
      );
    }
  } finally {
    state.activeJob = null;
    state.jobPending = false;
    busy(false);
  }
}
function routeProgressText(job, fallback) {
  const progress = job.progress;
  if (!progress?.message) return fallback;
  const lines = [progress.message];
  if (job.partial_result?.routes?.length) {
    lines.push(`${orderedRoutes(job.partial_result).length}개 경로를 찾았습니다. 확보한 경로는 먼저 확인할 수 있습니다.`);
  }
  if (progress.detail) lines.push(progress.detail);
  if (Number.isFinite(progress.elapsed_seconds)) {
    lines.push(`전체 경과 ${Math.max(0, Math.floor(progress.elapsed_seconds))}초 · 현재 단계 ${Math.max(0, Math.floor(progress.stage_elapsed_seconds || 0))}초`);
  }
  return lines.join('\n');
}
function routeOutcome(result) {
  if (!Array.isArray(result?.routes)) return { message: '완료했습니다.', error: false };
  const count = orderedRoutes(result).length;
  if (count) return { message: `${count}개 경로를 찾았습니다.`, error: false };
  let message = '현재 자료와 설정에서 경로를 완성하지 못했습니다.';
  if (result.termination_reason === 'max_seconds') {
    const seconds = Math.round(Number(result.elapsed_seconds));
    message = `탐색시간${seconds > 0 ? ' ' + seconds + '초' : ''} 한도에 도달했지만 경로를 완성하지 못했습니다.`;
  } else if (result.termination_reason === 'connector_limit') {
    message = '도보 조회 한도 안에서 경로를 완성하지 못했습니다.';
  }
  const usage = result.api_usage;
  if (usage && Number.isFinite(usage.new_api_calls) && Number.isFinite(usage.max_calls))
    message += ` 도보 조회 ${usage.new_api_calls}/${usage.max_calls}회 사용.`;
  return { message, error: true };
}
function clearRouteDisplay(message) {
  if (rider.active) stopGuide();
  if (routeLayer) routeLayer.clearLayers();
  state.currentRoute = null;
  state.displayedRouteKey = null;
  state.selectedRouteKey = null;
  $('goBar').hidden = true;
  $('routePersonalNote').hidden = true;
  $('mapCaption').textContent = message;
}
$('routeForm').onsubmit = async (e) => {
  e.preventDefault();
  if (state.initializing || state.activeJob || state.jobPending) {
    notify('기존 탐색을 확인 중입니다. 완료 후 다시 검색해 주세요.');
    return;
  }
  if (!state.points.origin || !state.points.destination) {
    notify('장소 검색 결과를 선택하거나 지도에서 위치를 지정해 주세요.');
    return;
  }
  if (rider.active) stopGuide();
  let departure = 'auto';
  if ($('departureMode').value === 'scheduled') {
    if (!$('departure').value) return notify('출발 날짜와 시각을 입력해 주세요.');
    departure = $('departure').value + ':00+09:00';
    if (new Date(departure) <= new Date()) return notify('미래 출발시각을 선택해 주세요.');
  }
  try {
    let received = false;
    const mode = state.mode;
    state.result = null;
    state.base = null;
    $('routeCards').replaceChildren();
    clearRouteDisplay('새 경로를 탐색하고 있습니다.');
    showView('search');
    state.selectedRouteKey = null;
    state.displayedRouteKey = null;
    state.filter = 'all';
    state.extra = {};
    state.searched = new Set();
    state.lastQuery = {
      origin: state.points.origin,
      destination: state.points.destination,
      departure,
      use_kma: $('useWeather').checked,
      use_inventory: true,
      personalize: $('usePersonal').checked,
    };
    const result = await runJob(
      '/api/routes',
      jsonRequest('POST', { ...state.lastQuery, mode }),
      'jobStatus',
      '모델 준비 및 경로 탐색 중입니다. 처음 실행은 더 오래 걸릴 수 있습니다.',
      (partial) => {
        showRoutes(partial, { reveal: !received });
        if (!received) setSheet('full');
        received = true;
      },
    );
    state.searched.add(mode);
    showRoutes(result, { reveal: !received });
    if (!received) setSheet('full');
  } catch (error) {
    status('jobStatus', error.message, true);
  }
};
async function resumeRoute(job) {
  const q = job.route_query;
  if (q) {
    setPoint('origin', q.origin);
    setPoint('destination', q.destination);
    setMode(q.mode);
    $('useWeather').checked = q.use_kma;
    $('usePersonal').checked = q.personalize;
    const scheduled = q.departure !== 'auto';
    $('departureMode').value = scheduled ? 'scheduled' : 'auto';
    $('departure').hidden = !scheduled;
    if (scheduled) $('departure').value = kstLocal(new Date(q.departure));
  }
  state.selectedRouteKey = null;
  state.displayedRouteKey = null;
  state.filter = 'all';
  state.extra = {};
  state.searched = new Set();
  if (q)
    state.lastQuery = {
      origin: q.origin,
      destination: q.destination,
      departure: q.departure,
      use_kma: q.use_kma,
      use_inventory: true,
      personalize: q.personalize,
    };
  let received = false;
  const result = await runJob(
    null, null, 'jobStatus', '이전 탐색을 이어서 확인하고 있습니다.',
    (partial) => {
      showRoutes(partial, { reveal: !received });
      if (!received) setSheet('full');
      received = true;
    },
    job,
  );
  state.searched.add(q?.mode || 'integrated');
  showRoutes(result, { reveal: !received });
  if (!received) setSheet('full');
}
function labelFor(id) {
  const n = state.result?.nodes?.[id];
  return (
    n?.name ||
    n?.station_name ||
    (id?.startsWith('bike:') ? state.stations.get(id.slice(5))?.name : null) ||
    { origin: '출발지', destination: '도착지' }[id] ||
    id ||
    ''
  );
}
function displayLegs(route) {
  const values = [];
  let previous = null;
  for (const leg of route.legs || []) {
    if (leg.mode === 'alight') continue;
    const gap = previous && leg.start ? (new Date(leg.start) - new Date(previous)) / 60000 : 0;
    if (gap > 0.5 && leg.mode !== 'wait')
      values.push({ mode: 'wait', minutes: gap, from: leg.from, to: leg.from, start: previous });
    const last = values.at(-1);
    if (
      last &&
      ['bus', 'subway'].includes(leg.mode) &&
      last.mode === leg.mode &&
      last.trip_id === leg.trip_id
    ) {
      last.to = leg.to;
      last.arrival = leg.arrival;
      last.minutes += +leg.minutes;
    } else values.push({ ...leg, minutes: +leg.minutes });
    previous = leg.arrival;
  }
  return values;
}
function routeKey(route) {
  const tokens = [];
  let start = null,
    end = null;
  const flush = () => {
    if (start !== null && start !== end) tokens.push(['walk', start, end, null]);
    start = end = null;
  };
  for (const leg of route.legs || []) {
    if (leg.mode === 'walk') {
      if (start !== null && end !== leg.from) flush();
      if (start === null) start = leg.from;
      end = leg.to;
    } else if (leg.mode !== 'wait' && leg.mode !== 'alight') {
      flush();
      tokens.push([leg.mode, leg.from, leg.to, leg.trip_id ?? null]);
    }
  }
  flush();
  return JSON.stringify(tokens);
}
function orderedRoutes(result) {
  const routes = new Map();
  const walking = (r) =>
    (r.legs || [])
      .filter((leg) => leg.mode === 'walk')
      .reduce((sum, leg) => sum + +(leg.minutes || 0), 0);
  for (const route of [...(result.routes || []), ...(result.mixed_routes || [])]) {
    const key = routeKey(route);
    const old = routes.get(key);
    if (
      !old ||
      +route.total_minutes < +old.total_minutes ||
      (+route.total_minutes === +old.total_minutes && walking(route) < walking(old))
    )
      routes.set(key, route);
  }
  return [...routes.values()].sort((a, b) => +a.total_minutes - +b.total_minutes);
}
function routeDisplayKey(route) {
  return (
    routeKey(route) +
    JSON.stringify(
      (route.legs || []).map((leg) => [
        leg.start,
        leg.arrival,
        leg.minutes,
        leg.display_geometry || leg.geometry || null,
      ]),
    )
  );
}
const categoryNames = {
  mixed: '따릉이 + 대중교통',
  transit: '대중교통',
  bicycle: '따릉이',
  walk: '도보',
};
const FILTERS = [
  ['all', '전체'],
  ['mixed', '통합'],
  ['transit', '대중교통'],
  ['bicycle', '따릉이'],
];
function routeCategory(route) {
  const modes = new Set((route.legs || []).map((leg) => leg.mode));
  const bike = modes.has('bicycle');
  const transit = modes.has('bus') || modes.has('subway');
  return bike && transit ? 'mixed' : bike ? 'bicycle' : transit ? 'transit' : 'walk';
}
function withExtras(base) {
  const extras = Object.values(state.extra);
  if (!extras.length) return base;
  return {
    ...base,
    routes: [
      ...(base.routes || []),
      ...extras.flatMap((e) => [...(e.routes || []), ...(e.mixed_routes || [])]),
    ],
    nodes: Object.assign({}, ...extras.map((e) => e.nodes || {}), base.nodes || {}),
  };
}
function renderFilter(all) {
  const box = $('routeFilter');
  box.replaceChildren();
  for (const [key, label] of FILTERS) {
    const n = key === 'all' ? all.length : all.filter((r) => routeCategory(r) === key).length;
    const b = node('button', 'filter-chip' + (n ? '' : ' empty'));
    b.type = 'button';
    b.setAttribute('role', 'tab');
    b.setAttribute('aria-selected', String(state.filter === key));
    b.append(label, node('small', '', String(n)));
    b.onclick = () => {
      state.filter = key;
      showRoutes(state.base, { reveal: false });
    };
    box.append(b);
  }
}
async function searchMore(mode) {
  if (!state.lastQuery || state.activeJob || state.jobPending) return;
  try {
    const result = await runJob(
      '/api/routes',
      jsonRequest('POST', { ...state.lastQuery, mode }),
      'jobStatus',
      categoryNames[mode] + ' 경로를 따로 찾고 있습니다.',
      (partial) => {
        state.extra[mode] = partial;
        showRoutes(state.base, { reveal: false });
      },
    );
    state.extra[mode] = result;
    state.searched.add(mode);
    showRoutes(state.base, { reveal: false });
  } catch (e) {
    status('jobStatus', e.message, true);
  }
}
function emptyState() {
  const box = node('div', 'empty-state');
  const name = categoryNames[state.filter];
  if (state.filter === 'mixed') {
    box.append(node('p', '', '따릉이와 대중교통을 함께 이용하는 경로가 없습니다.'));
  } else if (state.searched.has(state.filter)) {
    box.append(node('p', '', name + '(으)로 갈 수 있는 경로를 찾지 못했습니다.'));
  } else {
    box.append(node('p', '', '아직 ' + name + ' 경로를 따로 찾지 않았습니다.'));
    const b = node('button', 'secondary wide', name + ' 경로 찾아보기');
    b.type = 'button';
    b.onclick = () => searchMore(state.filter);
    box.append(b);
  }
  return box;
}
function showRoutes(base, { reveal = true } = {}) {
  state.base = base;
  const result = withExtras(base);
  state.result = result;
  if (reveal || state.view === 'results') showView('results');
  const all = orderedRoutes(result);
  renderFilter(all);
  const routes =
    state.filter === 'all' ? all : all.filter((route) => routeCategory(route) === state.filter);
  const selected = Math.max(
    0,
    routes.findIndex((route) => routeKey(route) === state.selectedRouteKey),
  );
  $('resultCount').textContent = all.length + '개';
  const notices = [];
  if (result.status === 'searching')
    notices.push('찾은 경로부터 표시합니다. 더 짧은 경로를 계속 확인하고 있습니다.');
  if (result.status === 'partial')
    notices.push('탐색 한도 내에서 확보한 경로입니다. 전체 탐색이 완료되지 않았습니다.');
  if (!all.length) notices.push(routeOutcome(result).message);
  $('resultNotice').textContent = notices.join(' ');
  $('resultNotice').hidden = !notices.length;
  const container = $('routeCards');
  const expanded = new Set(
    [...container.querySelectorAll('.route-card')]
      .filter((card) => card.querySelector('details')?.open)
      .map((card) => card.dataset.routeKey),
  );
  container.replaceChildren();
  if (!routes.length && state.filter !== 'all') container.append(emptyState());
  if (routes.length && ['transit', 'bicycle'].includes(state.filter) && !state.searched.has(state.filter)) {
    const more = node('button', 'secondary wide', categoryNames[state.filter] + ' 경로 따로 찾아보기');
    more.type = 'button';
    more.disabled = !!(state.activeJob || state.jobPending);
    more.onclick = () => searchMore(state.filter);
    container.append(more);
  }
  routes.forEach((route, i) => {
    const card = node('article', 'route-card' + (i === selected ? ' selected' : ''));
    card.dataset.routeKey = routeKey(route);
    const button = node('button', 'route-select');
    button.type = 'button';
    button.setAttribute(
      'aria-label',
      `${i + 1}번 경로 ${minute(route.total_minutes)}분 지도에서 보기`,
    );
    const top = node('div', 'route-top');
    const total = node('div', 'route-time', minute(route.total_minutes));
    total.append(node('small', '', ' 분'));
    const tags = node('div', 'route-tags');
    if (i === 0 && routes.length > 1) tags.append(node('span', 'pill best', '가장 빠름'));
    tags.append(node('span', 'pill', categoryNames[routeCategory(route)]));
    top.append(total, tags);
    button.append(top);
    const strip = node('div', 'mode-strip');
    const legs = displayLegs(route);
    for (const leg of legs.filter((l) => l.mode !== 'wait'))
      strip.append(node('span', 'transport-tag ' + leg.mode, names[leg.mode] || leg.mode));
    button.append(
      strip,
      node(
        'p',
        'hint',
        `${clock(result.departure)} 출발 → ${clock(route.arrival)} 도착 · 환승·대기 포함`,
      ),
    );
    button.onclick = () => {
      state.selectedRouteKey = routeKey(route);
      container.querySelectorAll('.route-card').forEach((c) => c.classList.remove('selected'));
      card.classList.add('selected');
      drawRoute(route);
      if (innerWidth <= 800) setSheet('mid');
    };
    card.append(button);
    const detail = document.createElement('details');
    detail.open = expanded.has(routeKey(route));
    detail.append(node('summary', '', '구간별 이동 보기'));
    const list = node('ol', 'leg-list');
    for (const leg of legs) {
      const li = node('li');
      li.append(node('span', 'leg-time', clock(leg.start)));
      const middle = node('div');
      middle.append(
        node(
          'strong',
          '',
          (names[leg.mode] || leg.mode) + (leg.route_name ? ' · ' + leg.route_name : ''),
        ),
      );
      middle.append(
        node(
          'p',
          '',
          leg.mode === 'wait' ? '탑승·출발 대기' : labelFor(leg.from) + ' → ' + labelFor(leg.to),
        ),
      );
      li.append(middle, node('span', '', minute(leg.minutes) + '분'));
      list.append(li);
    }
    detail.append(list);
    card.append(detail);
    container.append(card);
  });
  const diagnostics = $('diagnostics');
  diagnostics.replaceChildren();
  const personal = result.personalization || {};
  const lines = [
    `따릉이 시간 기준: ${result.bicycle_time_quantile?.toUpperCase() || '대중교통 시간표'}`,
    `개인화: ${personal.enabled ? '배율 ' + Number(personal.factor).toFixed(3) + ' · 고정 보정 ' + Number(personal.offset_min || 0).toFixed(2) + '분 · 사용 이력 ' + personal.usable_records + '건' : '기본 예측'}`,
    `기상청 예보: ${result.weather_requested ? '요청함' : '사용 안 함'} · 재고 모델: ${result.inventory_connected ? '연결됨' : '사용 안 함'}`,
    `종료 상태: ${result.termination_reason || result.status}`,
    `새 도보 API 호출: ${result.api_usage?.new_api_calls ?? 0}회`,
  ];
  for (const text of [...lines, ...(result.data_gaps || [])])
    diagnostics.append(node('p', '', text));
  if (routes[selected] && routeDisplayKey(routes[selected]) !== state.displayedRouteKey)
    drawRoute(routes[selected]);
  if (!routes.length) {
    clearRouteDisplay('현재 선택한 유형에서 표시할 경로가 없습니다.');
  }
}
function drawRoute(route) {
  if (rider.active && routeKey(rider.route) !== routeKey(route)) stopGuide();
  state.currentRoute = route;
  $('goBar').hidden = rider.active;
  state.displayedRouteKey = routeDisplayKey(route);
  appliedPersonalization(route);
  if (!map) return;
  routeLayer.clearLayers();
  let missing = 0;
  for (const leg of route.legs || []) {
    if (['alight', 'wait'].includes(leg.mode)) continue;
    const geometry = leg.geometry || leg.display_geometry;
    if (!geometry) {
      missing++;
      continue;
    }
    try {
      const line = L.geoJSON(geometry, {
        style: {
          color: colors[leg.mode] || '#777',
          weight: leg.mode === 'walk' ? 4 : 6,
          opacity: 0.9,
          dashArray:
            leg.mode === 'walk' ? '5 7' : leg.display_geometry && !leg.geometry ? '4 6' : null,
        },
      });
      line.bindTooltip(names[leg.mode] || leg.mode);
      routeLayer.addLayer(line);
    } catch {
      missing++;
    }
  }
  $('mapCaption').textContent = missing
    ? `${missing}개 구간의 지도 선을 확보하지 못했습니다. 구간 목록을 확인해 주세요.`
    : '선택한 경로 · ' + minute(route.total_minutes) + '분 예상';
  if (!rider.active) fitMap();
  rider.trail?.bringToFront();
}
function personalMinutes(base, p) {
  const a = Number(p?.factor ?? 1),
    b = Number(p?.offset_min ?? 0);
  return Math.max(
    base * Number(p?.minimum_ratio ?? 0.7),
    Math.min(base * Number(p?.maximum_ratio ?? 1.5), a * base + b),
  );
}
function personalText(p) {
  if (!p?.enabled) return '기본 예상시간을 사용합니다.';
  if (p.cold_start || !p.usable_records)
    return '아직 보정에 사용할 기록이 없어 기본 예상시간을 사용합니다.';
  const a = Number(p.factor),
    b = Number(p.offset_min || 0);
  if (!Number.isFinite(a) || !Number.isFinite(b)) return '기본 예상시간을 사용합니다.';
  if (Math.abs(b) >= 0.05)
    return `따릉이 기본 예상 × ${a.toFixed(3)} ${b < 0 ? '−' : '+'} ${Math.abs(b).toFixed(1)}분으로 보정합니다.`;
  const percent = Math.round(Math.abs(a - 1) * 1000) / 10;
  return percent < 0.1
    ? '등록한 기록을 반영했으며 기본 예상시간과 거의 같습니다.'
    : `따릉이 예상시간을 기본보다 ${percent}% ${a < 1 ? '짧게' : '길게'} 보정합니다.`;
}
function exclusionText(reason) {
  return (
    {
      personal_history_long_outlier:
        '평소 이력에 비해 이용시간이 통계적 상한을 넘어 보정에서 제외했습니다. 기록은 보관됩니다.',
      exercise_or_leisure: '운동·레저로 표시된 기록이라 보정에서 제외했습니다.',
      reported_pause: '중간에 멈춘 기록이라 보정에서 제외했습니다.',
      different_return_station: '예정한 대여소와 반납 대여소가 달라 보정에서 제외했습니다.',
      different_route: '예정한 경로와 달라 보정에서 제외했습니다.',
    }[reason] || '이 기록은 현재 보정에 사용되지 않습니다. 기록은 보관됩니다.'
  );
}
function appliedPersonalization(route) {
  const n = $('routePersonalNote');
  n.hidden = false;
  if (!(route.legs || []).some((l) => l.mode === 'bicycle')) {
    n.textContent = '이 경로에는 따릉이 구간이 없어 개인화가 적용되지 않습니다.';
    return;
  }
  n.textContent = personalText(state.result?.personalization);
}
async function refreshAccount() {
  state.session = await api('/api/session');
  const signed = !!state.session.profile_id;
  $('signedOut').hidden = !!state.session.username;
  $('createProfile').textContent =
    signed && !state.session.username ? '이 아이디로 기존 기록 연결' : '가입하기';
  $('passwordSettings').hidden = !state.session.username;
  $('passwordUsername').value = state.session.username || '';
  $('usernameText').textContent = state.session.username
    ? state.session.username + '님'
    : '아이디를 설정해 주세요';
  $('signedIn').hidden = !signed;
  $('personalOption').hidden = !signed;
  $('accountChip').textContent = state.session.username
    ? state.session.username + '님'
    : signed
      ? '내 기록 연결됨'
      : '기본 예측';
  if (signed) {
    const p = state.session.profile || {};
    $('factorText').textContent = p.cold_start ? '기본 예상시간 사용' : '내 이용기록 반영 중';
    $('profileStats').textContent = `현재 보정에 사용한 기록 ${p.usable_records || 0}건`;
    $('personalExplanation').textContent = personalText(p);
    $('personalExample').textContent = p.usable_records
      ? `예를 들어 기본 예상이 10분인 따릉이 구간은 약 ${minute(personalMinutes(10, p))}분으로 안내합니다.`
      : '이용내역을 등록하면 다음 경로 탐색부터 보정에 사용할 수 있습니다.';
    await refreshRides();
  }
}
async function refreshRides() {
  if (!state.session.profile_id) return;
  const data = await api('/api/rides');
  const list = $('rideList');
  list.replaceChildren();
  if (!data.count) {
    list.append(node('p', 'hint', '아직 등록한 기록이 없습니다. 첫 이용내역을 추가해 보세요.'));
    return;
  }
  for (const r of [...data.records].reverse()) {
    const card = node('div', 'history-item');
    const top = node('div', 'history-head');
    top.append(node('strong', '', kstLocal(r.started_at).replace('T', ' ')));
    const del = node('button', 'textbtn', '삭제');
    del.onclick = async () => {
      if (!confirm('이 이용 기록을 삭제할까요?')) return;
      try {
        await api('/api/rides/' + encodeURIComponent(r.receipt), { method: 'DELETE' });
        await refreshAccount();
      } catch (e) {
        notify(e.message);
      }
    };
    top.append(del);
    card.append(
      top,
      node(
        'p',
        '',
        `${state.stations.get(r.start_station_id)?.name || r.start_station_id} → ${state.stations.get(r.end_station_id)?.name || r.end_station_id}`,
      ),
      node(
        'p',
        '',
        `등록한 이용시간 ${minute(r.actual_minutes)}분 · 기본 예상 ${minute(r.base_p50_min)}분`,
      ),
      node('span', 'pill', r.used_for_update ? '보정에 사용할 기록' : '보관 · 보정 제외'),
    );
    if (!r.used_for_update) card.append(node('p', 'hint', exclusionText(r.exclusion_reason)));
    list.append(card);
  }
}
async function accountRequest(path) {
  const username = $('username').value.trim(),
    password = $('password').value;
  if (!username || password.length < 8) {
    status('personalStatus', '아이디와 8자 이상의 비밀번호를 입력해 주세요.', true);
    return;
  }
  $('createProfile').disabled = true;
  $('loginProfile').disabled = true;
  try {
    await api(path, jsonRequest('POST', { username, password }));
    $('password').value = '';
    await refreshAccount();
    status(
      'personalStatus',
      path === '/api/account'
        ? '가입했습니다. 이용내역을 등록하면 개인화가 적용됩니다.'
        : '로그인했습니다.',
    );
  } catch (e) {
    status('personalStatus', e.message, true);
  } finally {
    $('createProfile').disabled = false;
    $('loginProfile').disabled = false;
  }
}
$('createProfile').onclick = () => accountRequest('/api/account');
$('accountForm').onsubmit = (e) => {
  e.preventDefault();
  accountRequest('/api/session');
};
$('passwordForm').onsubmit = async (e) => {
  e.preventDefault();
  const current = $('currentPassword').value,
    next = $('newPassword').value;
  if (next !== $('repeatPassword').value) {
    status('passwordStatus', '새 비밀번호와 확인 값이 다릅니다.', true);
    return;
  }
  $('changePassword').disabled = true;
  try {
    await api(
      '/api/account/password',
      jsonRequest('POST', { current_password: current, new_password: next }),
    );
    $('passwordForm').reset();
    $('passwordUsername').value = state.session.username || '';
    status(
      'passwordStatus',
      '변경했습니다. 기존 이용기록은 유지되며 다른 기기에서는 다시 로그인해야 합니다.',
    );
  } catch (e) {
    status('passwordStatus', e.message, true);
  } finally {
    $('changePassword').disabled = false;
  }
};
$('logout').onclick = async () => {
  try {
    await api('/api/session', { method: 'DELETE' });
    clearPhoto();
    photoEditor(false);
    await refreshAccount();
  } catch (e) {
    notify(e.message);
  }
};
$('deleteProfile').onclick = async () => {
  if (!confirm('계정과 모든 이용 기록을 삭제할까요? 되돌릴 수 없습니다.')) return;
  try {
    await api('/api/account', { method: 'DELETE' });
    clearPhoto();
    photoEditor(false);
    await refreshAccount();
  } catch (e) {
    notify(e.message);
  }
};
function photoEditor(open) {
  state.photoEditorOpen = open;
  $('photoEditor').hidden = !open;
  $('historySection').hidden = open;
  $('openPhotoEditor').hidden = open;
  if (!open) $('openPhotoEditor').textContent = state.image ? '등록 계속하기' : '이용내역 등록';
  if (open) {
    setSheet('full');
    $('photoEditor').scrollIntoView({ block: 'start', behavior: 'smooth' });
  } else {
    $('openPhotoEditor').focus();
    $('openPhotoEditor').scrollIntoView({ block: 'center', behavior: 'smooth' });
  }
}
function hidePhotoFeedback() {
  $('photoFeedback').hidden = true;
}
function photoProblem(message, details = [], phase = 'recognize') {
  state.photoRetryPhase = /초안|만료/.test(message) ? 'recognize' : phase;
  $('photoFeedbackTitle').textContent =
    phase === 'confirm' ? '등록을 완료하지 못했습니다' : '사진을 확인해 주세요';
  $('photoFeedbackMessage').textContent = message || '이용내역을 읽지 못했습니다.';
  const list = $('photoFeedbackDetails');
  list.replaceChildren();
  for (const text of [...new Set(details)].filter(Boolean).slice(0, 6))
    list.append(node('li', '', text));
  let hint =
    phase === 'confirm'
      ? '사진과 수정한 내용을 유지했습니다. 아래 항목을 확인한 뒤 다시 등록할 수 있습니다.'
      : '자전거 번호, 출발·도착 대여소, 날짜·시각, 이용시간과 거리가 한 건 안에 모두 보이도록 자르기를 조정해 주세요.';
  if (/날씨|기상|연결|네트워크/.test(message))
    hint =
      '입력 내용은 유지됩니다. 잠시 후 다시 시도해 주세요. 등록이 완료되기 전에는 개인화에 반영되지 않습니다.';
  if (/패키지|설치/.test(message))
    hint =
      '서버에서 OCR 실행 환경을 확인해야 합니다. 사진을 다시 올리기보다 서버 담당자에게 이 메시지를 알려 주세요.';
  if (/만료/.test(message)) hint = '사진을 다시 읽어 확인 화면을 만든 뒤 등록해 주세요.';
  $('photoFeedbackHint').textContent = hint;
  $('ocrRetry').textContent = state.photoRetryPhase === 'confirm' ? '등록 다시 시도' : '다시 읽기';
  $('ocrRetry').hidden = !state.image;
  $('ocrRecrop').hidden = !state.image;
  $('ocrManual').hidden = !state.draft?.manual_reviewable_count;
  $('photoFeedback').hidden = false;
  status('ocrStatus', '');
  if (state.photoEditorOpen) {
    $('photoFeedback').focus({ preventScroll: true });
    $('photoFeedback').scrollIntoView({ block: 'center', behavior: 'smooth' });
  }
}
function clearPhoto() {
  state.manualMode = false;
  state.photoEpoch++;
  if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
  state.objectUrl = null;
  state.image = null;
  state.file = null;
  state.draft = null;
  $('screenshotFile').value = '';
  $('screenshotPreview').removeAttribute('src');
  $('cropPanel').hidden = true;
  $('ocrReview').hidden = true;
  hidePhotoFeedback();
  status('ocrStatus', '');
}
$('openPhotoEditor').onclick = () => photoEditor(true);
$('photoBack').onclick = () => photoEditor(false);
$('photoMinimize').onclick = () => {
  if (innerWidth <= 800) {
    setSheet('peek');
    notify('패널을 줄였습니다. 위쪽 손잡이를 누르면 이어서 볼 수 있습니다.');
  } else photoEditor(false);
};
$('ocrRetry').onclick = () => {
  if (state.photoRetryPhase === 'confirm') $('confirmForm').requestSubmit();
  else $('recognizeButton').click();
};
$('ocrRecrop').onclick = () => {
  hidePhotoFeedback();
  $('photoPreviewDetails').open = true;
  $('cropControls').scrollIntoView({ block: 'center', behavior: 'smooth' });
  $('cropTop').focus({ preventScroll: true });
};
$('ocrChooseAnother').onclick = () => $('screenshotFile').click();
$('screenshotFile').onchange = async () => {
  const file = $('screenshotFile').files[0];
  if (!file) return;
  clearPhoto();
  photoEditor(true);
  const epoch = state.photoEpoch;
  if (!['image/png', 'image/jpeg'].includes(file.type)) {
    photoProblem('PNG 또는 JPG 사진을 선택해 주세요.');
    return;
  }
  if (file.size > 12 * 1024 * 1024) {
    photoProblem('사진 크기가 12MB를 넘습니다. 한 이용내역만 잘라 저장한 사진을 선택해 주세요.');
    return;
  }
  state.file = file;
  state.objectUrl = URL.createObjectURL(file);
  const image = new Image();
  image.src = state.objectUrl;
  try {
    await image.decode();
    if (epoch !== state.photoEpoch) return;
    if (image.naturalWidth * image.naturalHeight > 20000000)
      throw new Error('사진 해상도가 너무 큽니다. 2천만 화소 이하의 스크린샷을 선택해 주세요.');
    state.image = image;
    $('screenshotPreview').src = state.objectUrl;
    $('cropTop').value = 0;
    $('cropBottom').value = 100;
    $('photoPreviewDetails').open = true;
    $('cropPanel').hidden = false;
    updateCrop();
  } catch (e) {
    if (epoch !== state.photoEpoch) return;
    clearPhoto();
    photoProblem(e.message || '사진 파일을 읽지 못했습니다. 다른 PNG/JPG 사진을 선택해 주세요.');
  }
};
function updateCrop() {
  let top = +$('cropTop').value,
    bottom = +$('cropBottom').value;
  if (bottom <= top + 4) {
    if (document.activeElement === $('cropTop')) top = bottom - 5;
    else bottom = top + 5;
  }
  $('cropTop').value = Math.max(0, top);
  $('cropBottom').value = Math.min(100, bottom);
  $('cropTopValue').value = $('cropTop').value + '%';
  $('cropBottomValue').value = $('cropBottom').value + '%';
  $('cropShadeTop').style.height = $('cropTop').value + '%';
  $('cropShadeBottom').style.height = 100 - $('cropBottom').value + '%';
  state.draft = null;
  $('ocrReview').hidden = true;
  $('confirmRead').checked = false;
  hidePhotoFeedback();
}
async function croppedImage() {
  if (!state.image) throw new Error('사진을 먼저 선택해 주세요.');
  const image = state.image;
  const top = Math.floor((image.naturalHeight * +$('cropTop').value) / 100),
    bottom = Math.ceil((image.naturalHeight * +$('cropBottom').value) / 100);
  const canvas = document.createElement('canvas');
  canvas.width = image.naturalWidth;
  canvas.height = bottom - top;
  canvas
    .getContext('2d')
    .drawImage(image, 0, top, image.naturalWidth, bottom - top, 0, 0, canvas.width, canvas.height);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
  if (!blob || blob.size > 12 * 1024 * 1024)
    throw new Error('선택한 범위가 너무 큽니다. 한 이용내역만 남겨 주세요.');
  return blob;
}
$('recognizeButton').onclick = async () => {
  hidePhotoFeedback();
  state.draft = null;
  $('ocrReview').hidden = true;
  const epoch = state.photoEpoch;
  try {
    const image = await croppedImage();
    const draft = await runJob(
      '/api/screenshots',
      { method: 'POST', headers: { 'Content-Type': 'image/png' }, body: image },
      'ocrStatus',
      '이용내역을 읽고 있습니다. 처음에는 OCR 준비에 시간이 걸립니다. 패널을 접거나 내 기록으로 이동해도 작업은 계속됩니다.',
    );
    if (epoch !== state.photoEpoch) return;
    state.draft = draft;
    renderDraft();
  } catch (e) {
    if (epoch === state.photoEpoch) photoProblem(e.message, [], 'recognize');
  }
};
const editSchema = [
  ['start_station_id', '출발 대여소 번호', 'text'],
  ['start_station_name', '출발 대여소 이름', 'text'],
  ['end_station_id', '도착 대여소 번호', 'text'],
  ['end_station_name', '도착 대여소 이름', 'text'],
  ['started_at', '대여 날짜·시각 (한국시간)', 'datetime-local'],
  ['returned_at', '반납 날짜·시각 (한국시간)', 'datetime-local'],
  ['actual_minutes', '앱에 표시된 이용시간 (분)', 'number'],
  ['distance_km', '앱에 표시된 이용거리 (km)', 'number'],
  ['bicycle_number', '자전거 번호', 'text'],
];
function renderDraft(manual = false) {
  const d = state.draft;
  state.manualMode = manual;
  const editable = (d.cards || [])
    .map((card, index) => ({ card, index }))
    .filter(({ card }) => card.reviewable || (manual && card.manual_editable));
  $('ocrReview').hidden = !editable.length;
  if (!editable.length) {
    photoProblem(
      d.message || '완전한 이용내역을 찾지 못했습니다.',
      (d.cards || []).flatMap((c) => c.errors || []),
    );
    return;
  }
  hidePhotoFeedback();
  const select = $('cardSelect');
  select.replaceChildren();
  for (const { card, index } of editable) {
    const r = card.record || card.partial_record || {};
    const o = node(
      'option',
      '',
      `${index + 1}번째 내역 · ${r.start_station_id || '출발 미확인'} → ${r.end_station_id || '도착 미확인'}${card.reviewable ? '' : ' · 직접 입력'}`,
    );
    o.value = index;
    select.append(o);
  }
  select.onchange = renderCard;
  $('editModeHint').textContent = manual
    ? 'OCR이 읽은 일부 값만 채웠습니다. 빈칸을 원본 사진과 대조해 입력해 주세요. 다른 이용내역의 값을 섞지 마세요.'
    : '읽은 값도 수정할 수 있습니다. 원본과 대조한 뒤 등록하세요.';
  renderCard();
  status(
    'ocrStatus',
    manual
      ? '원본 사진을 보고 필수 항목을 입력한 뒤 등록하세요.'
      : '글자 인식이 끝났습니다. 원본과 비교하고 필요한 내용을 수정한 뒤 등록해 주세요.',
  );
  if (state.photoEditorOpen) $('ocrReview').scrollIntoView({ block: 'start', behavior: 'smooth' });
  else notify('사진 인식이 끝났습니다. 등록 계속하기에서 결과를 확인해 주세요.');
}
$('ocrManual').onclick = () => renderDraft(true);
function renderCard() {
  const card = state.draft.cards[+$('cardSelect').value];
  if (!card) return;
  const names = card.observed_station_names || {};
  const record = {
    ...(card.record || card.partial_record || {}),
    start_station_name: names.origin || '',
    end_station_name: names.destination || '',
  };
  const fields = $('editFields');
  fields.replaceChildren();
  for (const [key, label, type] of editSchema) {
    const lab = node('label', '', label);
    lab.htmlFor = 'edit_' + key;
    const input = node('input');
    input.id = 'edit_' + key;
    input.name = key;
    input.type = type;
    input.required = true;
    if (type === 'number') {
      input.min = '0.001';
      input.step = 'any';
    }
    input.value =
      type === 'datetime-local' ? (record[key] ? kstLocal(record[key]) : '') : (record[key] ?? '');
    input.oninput = () => {
      $('confirmRead').checked = false;
    };
    fields.append(lab, input);
  }
  const mapping = $('mappingInfo');
  mapping.replaceChildren();
  for (const [side, m] of Object.entries(card.station_mapping?.stations || {})) {
    mapping.append(
      node(
        'p',
        'mapping-row' + (m.status === 'matched' ? '' : ' bad'),
        `${side === 'origin' ? '출발' : '도착'} · 목록 이름: ${m.mapped_name || '미확인'} · ${m.status === 'matched' ? '번호·이름 일치' : m.message}`,
      ),
    );
    for (const candidate of m.status === 'matched' ? [] : m.candidates || []) {
      const b = node(
        'button',
        'textbtn',
        `${candidate.station_id} ${candidate.station_name || candidate.name || ''}로 수정`,
      );
      b.type = 'button';
      b.onclick = () => {
        $('edit_' + (side === 'origin' ? 'start' : 'end') + '_station_id').value =
          candidate.station_id;
        $('edit_' + (side === 'origin' ? 'start' : 'end') + '_station_name').value =
          candidate.station_name || candidate.name || '';
        $('confirmRead').checked = false;
      };
      mapping.append(b);
    }
  }
  for (const text of card.errors || []) mapping.append(node('p', 'mapping-row bad', text));
  for (const text of card.warnings || []) mapping.append(node('p', 'hint', text));
  $('confirmRead').checked = false;
}
$('confirmForm').onsubmit = async (e) => {
  e.preventDefault();
  if (!$('confirmRead').checked) return;
  const edits = {};
  for (const [key, , type] of editSchema) {
    let value = $('edit_' + key).value.trim();
    if (type === 'number') {
      value = Number(value);
      if (!Number.isFinite(value) || value <= 0)
        return photoProblem('이용시간과 거리는 0보다 큰 숫자로 입력해 주세요.', [], 'confirm');
    }
    if (type === 'datetime-local') value += ':00+09:00';
    edits[key] = value;
  }
  try {
    hidePhotoFeedback();
    const r = await runJob(
      '/api/screenshots/confirm',
      jsonRequest('POST', {
        draft_id: state.draft.draft_id,
        card_index: +$('cardSelect').value,
        confirmed: true,
        manual: !!state.manualMode,
        edits,
      }),
      'ocrStatus',
      '과거 날씨와 기본 P50을 계산하고 개인화 기록에 반영하고 있습니다.',
    );
    clearPhoto();
    photoEditor(false);
    await refreshAccount();
    $('ocrReview').hidden = true;
    status(
      'personalStatus',
      r.duplicate
        ? '이미 등록한 기록입니다. 중복으로 보정하지 않았습니다.'
        : r.used_for_update
          ? '등록했고 보정에 반영했습니다. ' + personalText(r.profile)
          : '기록은 저장했지만 이번 보정에는 반영하지 않았습니다. ' +
            exclusionText(r.exclusion_reason),
    );
    notify(
      r.duplicate
        ? '이미 등록된 기록입니다.'
        : r.used_for_update
          ? '기록 저장 · 보정 반영 완료'
          : '기록 저장 · 보정 제외',
    );
  } catch (e) {
    photoProblem(e.message, [], 'confirm');
  }
};
document
  .querySelectorAll('[data-search]')
  .forEach((b) => (b.onclick = () => searchPlace(b.dataset.search)));
document.querySelectorAll('[data-pick]').forEach(
  (b) =>
    (b.onclick = () => {
      state.picking = b.dataset.pick;
      $('mapWrap').classList.add('picking');
      $('pickHint').textContent =
        (state.picking === 'origin' ? '출발' : '도착') + ' 위치를 지도에서 눌러 주세요.';
      setSheet('peek');
    }),
);
document
  .querySelectorAll('[data-mode]')
  .forEach((b) => (b.onclick = () => setMode(b.dataset.mode)));
const placeTimers = {};
for (const key of ['origin', 'destination']) {
  $(key).oninput = () => {
    cancelPlaceSearch(key);
    state.points[key] = null;
    renderMarkers();
    clearTimeout(placeTimers[key]);
    placeTimers[key] = setTimeout(() => searchPlace(key, { silent: true }), 350);
  };
  $(key).onkeydown = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      clearTimeout(placeTimers[key]);
      searchPlace(key);
    }
  };
}
document.querySelectorAll('[data-clear]').forEach(
  (b) =>
    (b.onclick = () => {
      const key = b.dataset.clear;
      cancelPlaceSearch(key);
      $(key).value = '';
      state.points[key] = null;
      $(key + 'Suggestions').hidden = true;
      renderMarkers();
      $(key).focus();
    }),
);
$('swap').onclick = () => {
  cancelPlaceSearch('origin');
  cancelPlaceSearch('destination');
  [state.points.origin, state.points.destination] = [state.points.destination, state.points.origin];
  [$('origin').value, $('destination').value] = [$('destination').value, $('origin').value];
  renderMarkers();
};
$('locate').onclick = () => {
  if (!navigator.geolocation) return notify('현재 위치를 지원하지 않는 브라우저입니다.');
  navigator.geolocation.getCurrentPosition(
    (v) => {
      setPoint('origin', {
        name: '현재 위치',
        latitude: v.coords.latitude,
        longitude: v.coords.longitude,
      });
      fitMap();
    },
    () => notify('위치 권한 또는 HTTPS 접속을 확인해 주세요. 지도에서도 선택할 수 있습니다.'),
    { timeout: 8000 },
  );
};
$('departureMode').onchange = () => {
  $('departure').hidden = $('departureMode').value !== 'scheduled';
};
$('departure').value = kstLocal(new Date(Date.now() + 15 * 60000));
$('navRoutes').onclick = () => showPanel(false);
$('navPersonal').onclick = () => showPanel(true);
$('fitMap').onclick = fitMap;
$('refreshRides').onclick = () => refreshAccount().catch((e) => notify(e.message));
$('cropTop').oninput = updateCrop;
$('cropBottom').oninput = updateCrop;
let drag = null;
$('dragzone').addEventListener('pointerdown', (e) => {
  if (innerWidth > 800 || e.target.closest('a,button')) return;
  drag = { y: e.clientY, h: $('sheet').getBoundingClientRect().height };
  $('dragzone').setPointerCapture(e.pointerId);
  $('sheet').classList.add('dragging');
});
$('dragzone').addEventListener('pointermove', (e) => {
  if (!drag) return;
  const h = Math.min(innerHeight * 0.94, Math.max(132, drag.h + drag.y - e.clientY));
  $('sheet').style.setProperty('--sheet-h', h + 'px');
});
$('dragzone').addEventListener('pointerup', (e) => {
  if (!drag) return;
  const h = $('sheet').getBoundingClientRect().height;
  const tapped = Math.abs(e.clientY - drag.y) < 5;
  drag = null;
  if (tapped) {
    $('sheet').classList.remove('dragging');
    setSheet(state.sheet === 'full' ? 'mid' : 'full');
    return;
  }
  $('sheet').classList.remove('dragging');
  setSheet(h > innerHeight * 0.77 ? 'full' : h < innerHeight * 0.38 ? 'peek' : 'mid');
});
$('dragzone').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    setSheet(state.sheet === 'full' ? 'peek' : 'full');
  }
});
/* ===== 이동 안내: 내 위치를 마스코트 얼굴로 지도에 표시 =====
   - '이 경로로 출발하기'를 누르면 위치 추적을 시작합니다. (?sim=1 을 주소에 붙이면 경로를 따라 가상 이동)
   - 마스코트가 GPS 위치를 따라 부드럽게 움직이고, 지나온 길은 회색으로 바뀝니다. */
const rider = {
  active: false, watchId: null, simId: null, marker: null, ring: null, trail: null,
  route: null, path: [], cum: [], total: 0, progress: 0,
  shown: null, last: null, raf: 0, idleTimer: 0,
  follow: true, offCount: 0, lastWarn: 0, wake: null, wakeEpoch: 0, arrived: false,
};
const M_LAT = 111320;
const fmtDist = (m) => (m >= 1000 ? (m / 1000).toFixed(1) + 'km' : Math.max(10, Math.round(m / 10) * 10) + 'm');
function metersBetween(a, b) {
  const k = Math.cos((((a[0] + b[0]) / 2) * Math.PI) / 180);
  return Math.hypot((b[0] - a[0]) * M_LAT, (b[1] - a[1]) * M_LAT * k);
}
function collectLines(g, out) {
  if (!g) return;
  if (g.type === 'FeatureCollection') g.features.forEach((f) => collectLines(f, out));
  else if (g.type === 'Feature') collectLines(g.geometry, out);
  else if (g.type === 'GeometryCollection') g.geometries.forEach((x) => collectLines(x, out));
  else if (g.type === 'LineString') out.push(g.coordinates);
  else if (g.type === 'MultiLineString') g.coordinates.forEach((c) => out.push(c));
}
function routePath(route) {
  const lines = [];
  for (const leg of route.legs || []) {
    if (['alight', 'wait'].includes(leg.mode)) continue;
    collectLines(leg.geometry || leg.display_geometry, lines);
  }
  const pts = [];
  for (const line of lines)
    for (const [lng, lat] of line) {
      const q = pts.at(-1);
      if (!q || q[0] !== lat || q[1] !== lng) pts.push([lat, lng]);
    }
  return pts;
}
function projectOnPath(lat, lng) {
  const { path, cum } = rider;
  const k = Math.cos((path[0][0] * Math.PI) / 180);
  const px = lng * M_LAT * k, py = lat * M_LAT;
  let best = { d: Infinity, s: 0, i: 0, t: 0 };
  for (let i = 0; i < path.length - 1; i++) {
    if (cum[i + 1] < rider.progress - 200) continue; // 200m 넘게 뒤로 되감기지 않게
    const ax = path[i][1] * M_LAT * k, ay = path[i][0] * M_LAT;
    const dx = path[i + 1][1] * M_LAT * k - ax, dy = path[i + 1][0] * M_LAT - ay;
    const len2 = dx * dx + dy * dy;
    const t = len2 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2)) : 0;
    const d = Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
    if (d < best.d) best = { d, i, t, s: cum[i] + t * Math.sqrt(len2) };
  }
  return best;
}
function pointAtDistance(s) {
  const { path, cum } = rider;
  let i = 0;
  while (i < path.length - 2 && cum[i + 1] < s) i++;
  const seg = cum[i + 1] - cum[i] || 1;
  const t = Math.max(0, Math.min(1, (s - cum[i]) / seg));
  return [path[i][0] + (path[i + 1][0] - path[i][0]) * t, path[i][1] + (path[i + 1][1] - path[i][1]) * t];
}
function createRider(pos) {
  rider.marker = L.marker(pos, {
    icon: L.divIcon({
      className: 'rider-icon',
      html: '<div class="rider-shadow"></div><div class="rider-badge"><img src="/assets/head.webp" alt="" /></div>',
      iconSize: [56, 68],
      iconAnchor: [28, 68],
    }),
    zIndexOffset: 2000,
    keyboard: false,
    title: '내 위치',
  }).addTo(map);
  rider.ring = L.circle(pos, {
    radius: 10, color: '#4cc40a', weight: 1, fillColor: '#81f104', fillOpacity: 0.18, interactive: false,
  }).addTo(map);
  rider.shown = pos;
}
function moveRider(to) {
  const from = rider.shown || to;
  const t0 = performance.now();
  cancelAnimationFrame(rider.raf);
  const step = (now) => {
    const k = Math.min(1, (now - t0) / 900);
    rider.shown = [from[0] + (to[0] - from[0]) * k, from[1] + (to[1] - from[1]) * k];
    rider.marker.setLatLng(rider.shown);
    if (k < 1) rider.raf = requestAnimationFrame(step);
  };
  rider.raf = requestAnimationFrame(step);
}
function setHopping(on) {
  rider.marker?.getElement()?.classList.toggle('moving', on);
}
function setFollow(on) {
  rider.follow = on;
  $('guideFollow').setAttribute('aria-pressed', String(on));
  $('guideFollow').classList.toggle('on', on);
  if (on && rider.shown) map.panTo(rider.shown, { animate: true });
}
function onFix(c) {
  if (!rider.active || rider.arrived) return;
  const pos = [c.latitude, c.longitude];
  const moved = rider.last ? metersBetween(rider.last, pos) : 0;
  rider.last = pos;
  if (!rider.marker) createRider(pos);
  moveRider(pos);
  rider.ring.setLatLng(pos);
  rider.ring.setRadius(Math.min(Math.max(c.accuracy || 10, 8), 150));
  if (moved > 2) {
    setHopping(true);
    clearTimeout(rider.idleTimer);
    rider.idleTimer = setTimeout(() => setHopping(false), 2500);
  }
  if (rider.follow) map.panTo(pos, { animate: true, duration: 0.8 });
  if (rider.path.length < 2) {
    $('guideTitle').textContent = '안내 중';
    $('guideSub').textContent = '내 위치를 표시하고 있어요';
    return;
  }
  const pr = projectOnPath(pos[0], pos[1]);
  const off = pr.d > 80;
  if (!off) {
    // 경로 위에 있을 때만 진행 위치·지나온 길을 갱신 (이탈 중에는 마지막 위치 유지)
    rider.progress = pr.s;
    const a = rider.path[pr.i], b = rider.path[pr.i + 1];
    const cut = [a[0] + (b[0] - a[0]) * pr.t, a[1] + (b[1] - a[1]) * pr.t];
    rider.trail.setLatLngs([...rider.path.slice(0, pr.i + 1), cut]);
  }
  const remain = Math.max(0, rider.total - rider.progress);
  if (!off && remain < 30) return arrive();
  rider.offCount = off ? rider.offCount + 1 : 0;
  const lost = rider.offCount >= 3;
  $('guideReroute').hidden = !lost;
  if (lost) {
    $('guideTitle').textContent = '경로에서 벗어났어요';
    $('guideSub').textContent = '현재 위치에서 경로를 다시 찾을 수 있어요';
    if (Date.now() - rider.lastWarn > 60000) {
      rider.lastWarn = Date.now();
      notify('경로에서 벗어났어요. 현재 위치에서 다시 찾아볼까요?');
    }
  } else {
    $('guideTitle').textContent = '안내 중';
    $('guideSub').textContent = `남은 경로 거리 ${fmtDist(remain)}`;
  }
}
function onGeoError(err) {
  if (err.code === 1) {
    notify('위치 권한이 꺼져 있어요. 브라우저 설정에서 위치 접근을 허용해 주세요.');
    stopGuide();
  } else {
    $('guideSub').textContent = '내 위치를 확인하는 중이에요. 하늘이 트인 곳에서 더 정확해요.';
  }
}
async function keepAwake(on) {
  const epoch = ++rider.wakeEpoch;
  const previous = rider.wake;
  rider.wake = null;
  try {
    if (previous && !previous.released) await previous.release();
    if (!on || !rider.active || rider.arrived || epoch !== rider.wakeEpoch || !('wakeLock' in navigator)) return;
    const next = await navigator.wakeLock.request('screen');
    if (epoch !== rider.wakeEpoch || !rider.active || rider.arrived) await next.release();
    else rider.wake = next;
  } catch {}
}
function stopTracking() {
  if (rider.watchId !== null) navigator.geolocation.clearWatch(rider.watchId);
  clearInterval(rider.simId);
  rider.watchId = rider.simId = null;
  keepAwake(false);
}
function arrive() {
  rider.arrived = true;
  stopTracking();
  clearTimeout(rider.idleTimer);
  $('guideTitle').textContent = '도착했어요! 🎉';
  $('guideSub').textContent = '안전하게 잘 도착했어요.';
  $('guideReroute').hidden = true;
  $('guideFollow').hidden = true;
  $('guideStop').textContent = '닫기';
  setHopping(false);
}
function stopGuide() {
  stopTracking();
  cancelAnimationFrame(rider.raf);
  clearTimeout(rider.idleTimer);
  map?.off('dragstart', onUserDrag);
  for (const k of ['marker', 'ring', 'trail']) {
    if (rider[k]) map.removeLayer(rider[k]);
    rider[k] = null;
  }
  rider.active = false;
  rider.arrived = false;
  rider.shown = rider.last = null;
  $('guideCard').hidden = true;
  $('guideFollow').hidden = false;
  $('guideStop').textContent = '종료';
  $('goBar').hidden = !state.currentRoute;
}
function onUserDrag() {
  if (rider.follow) setFollow(false);
}
function startGuide() {
  const route = state.currentRoute;
  const sim = new URLSearchParams(location.search).has('sim');
  if (!map || !route) return notify('먼저 경로를 선택해 주세요.');
  if (!sim && !window.isSecureContext)
    return notify('내 위치는 https 주소에서만 사용할 수 있어요.');
  if (!sim && !('geolocation' in navigator))
    return notify('이 브라우저에서는 위치 정보를 사용할 수 없어요.');
  if (rider.active) stopGuide();
  rider.route = route;
  rider.path = routePath(route);
  rider.cum = [0];
  for (let i = 1; i < rider.path.length; i++)
    rider.cum.push(rider.cum[i - 1] + metersBetween(rider.path[i - 1], rider.path[i]));
  rider.total = rider.cum.at(-1) || 0;
  rider.progress = 0;
  rider.offCount = 0;
  rider.arrived = false;
  rider.active = true;
  rider.trail = L.polyline([], { color: '#8e9a89', weight: 7, opacity: 0.95, interactive: false }).addTo(map);
  map.on('dragstart', onUserDrag);
  $('goBar').hidden = true;
  $('guideCard').hidden = false;
  $('guideReroute').hidden = true;
  $('guideTitle').textContent = '안내 중';
  $('guideSub').textContent = '내 위치를 찾는 중…';
  setFollow(true);
  if (innerWidth <= 800) setSheet('peek');
  keepAwake(true);
  if (sim) {
    if (rider.path.length < 2) {
      stopGuide();
      return notify('가상 이동에 쓸 경로 선이 없어요.');
    }
    let s = 0;
    const tick = () => {
      onFix({ latitude: pointAtDistance(s)[0], longitude: pointAtDistance(s)[1], accuracy: 8 });
      s = Math.min(rider.total, s + 25);
    };
    tick();
    if (!rider.arrived) rider.simId = setInterval(tick, 1000);
  } else {
    rider.watchId = navigator.geolocation.watchPosition(
      (p) => onFix(p.coords),
      onGeoError,
      { enableHighAccuracy: true, maximumAge: 1000, timeout: 15000 },
    );
  }
}
$('startGuide').onclick = startGuide;
$('guideStop').onclick = stopGuide;
$('guideFollow').onclick = () => setFollow(!rider.follow);
$('guideReroute').onclick = () => {
  const c = rider.last;
  stopGuide();
  if (!c) return;
  setPoint('origin', { name: '현재 위치', latitude: c[0], longitude: c[1] });
  showView('search');
  $('routeForm').requestSubmit();
};
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && rider.active && !rider.arrived && (!rider.wake || rider.wake.released))
    keepAwake(true);
});

async function init() {
  busy(true);
  let previous = null;
  try {
    initMap();
    setMode('integrated');
    const stations = await api('/api/stations');
    state.stations = new Map(stations.map((s) => [s.station_id, s]));
    await refreshAccount();
    previous = (await api('/api/routes/current')).job;
    const health = await api('/api/health');
    const missing = Object.entries(health.configured)
      .filter(([, ok]) => !ok)
      .map(([k]) => ({ kakao: '카카오', weather: '기상청', inventory: '서울 재고' })[k]);
    status(
      'serverStatus',
      missing.length
        ? `서버 연결됨 · ${missing.join('·')} 키 설정을 확인해 주세요.`
        : '서버 연결됨 · V5.2.1 · 날씨·재고 모델 연결 준비됨',
      !!missing.length,
    );
  } catch (e) {
    status('serverStatus', e.message, true);
  } finally {
    state.initializing = false;
    busy(false);
  }
  if (previous) resumeRoute(previous).catch((e) => status('jobStatus', e.message, true));
}
init();
