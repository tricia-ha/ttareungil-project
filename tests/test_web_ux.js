'use strict';
// Exercise actual UI functions without browser APIs, models, or external calls.
const fs = require('node:fs'),
  path = require('node:path'),
  vm = require('node:vm'),
  assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
function sourcePosition(marker) {
  const pattern = marker.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/=/g, '\\s*=\\s*');
  const found = source.match(new RegExp(pattern));
  assert.ok(found, 'Missing source marker: ' + marker);
  return found.index;
}
const elements = new Map();
function element(id) {
  if (!elements.has(id))
    elements.set(id, {
      hidden: false,
      value: '',
      textContent: '',
      style: {},
      children: [],
      replaceChildren() {
        this.children = [];
      },
      append(...items) {
        this.children.push(...items);
      },
      focus() {},
      scrollIntoView() {},
    });
  return elements.get(id);
}
const context = {
  state: { image: { selected: true }, photoEditorOpen: true, draft: { kept: true } },
  $: element,
  node: (tag, cls, text) => ({ tag, cls, text }),
  setSheet: () => {},
  status: (id, text) => {
    element(id).textContent = text;
  },
  notify: () => {},
  document: { activeElement: null },
};
vm.createContext(context);
for (const [start, end] of [
  ['function personalMinutes(', 'async function refreshAccount('],
  ['function photoEditor(', 'function clearPhoto('],
  ['function updateCrop(', 'async function croppedImage('],
  ['function renderDraft(', 'function renderCard('],
]) {
  vm.runInContext(source.slice(sourcePosition(start), sourcePosition(end)), context);
}
assert.match(context.personalText({ enabled: true, cold_start: true, usable_records: 0 }), /기본/);
assert.match(context.personalText({ enabled: true, usable_records: 3, factor: 0.93 }), /7% 짧게/);
assert.match(context.personalText({ enabled: true, usable_records: 3, factor: 1.1 }), /10% 길게/);
assert.match(
  context.personalText({ enabled: true, usable_records: 3, factor: 0.9, offset_min: -2 }),
  /0.900.*2.0분/,
);
assert.equal(context.personalMinutes(10, { factor: 0.9, offset_min: -2 }), 7);
assert.ok(context.personalMinutes(0.1, { factor: 0.9, offset_min: -2 }) > 0);
assert.match(context.exclusionText('personal_history_long_outlier'), /통계적 상한/);
context.photoProblem('완전한 이용내역을 찾지 못했습니다.', [
  '날짜가 없습니다.',
  '날짜가 없습니다.',
  '거리가 없습니다.',
]);
assert.equal(element('photoFeedback').hidden, false);
assert.equal(element('photoFeedbackDetails').children.length, 2);
assert.equal(element('ocrRetry').textContent, '다시 읽기');
context.photoEditor(false);
assert.equal(element('photoEditor').hidden, true);
assert.equal(element('openPhotoEditor').textContent, '등록 계속하기');
assert.equal(context.state.image.selected, true);
assert.equal(context.state.draft.kept, true);
context.photoEditor(true);
assert.equal(element('photoEditor').hidden, false);
assert.equal(element('photoFeedback').hidden, false);
context.photoProblem('과거 날씨 연결에 실패했습니다.', [], 'confirm');
assert.equal(element('ocrRetry').textContent, '등록 다시 시도');
assert.match(element('photoFeedbackHint').textContent, /입력 내용은 유지/);
context.photoProblem('초안이 만료됐습니다.', [], 'confirm');
assert.equal(context.state.photoRetryPhase, 'recognize');
assert.equal(element('ocrRetry').textContent, '다시 읽기');
element('cropTop').value = 5;
element('cropBottom').value = 95;
context.updateCrop();
assert.equal(context.state.draft, null);
assert.equal(element('ocrReview').hidden, true);
context.state.draft = {
  reviewable_count: 0,
  cards: [{ errors: ['대여 날짜 누락'] }],
  message: '한 건을 읽지 못했습니다.',
};
context.renderDraft();
assert.equal(element('photoFeedbackDetails').children[0].text, '대여 날짜 누락');
vm.runInContext(
  source.slice(sourcePosition('const editSchema='), sourcePosition('function renderDraft(')),
  context,
);
vm.runInContext(
  source.slice(
    sourcePosition('function renderCard('),
    sourcePosition("$('confirmForm').onsubmit="),
  ),
  context,
);
context.state.draft = {
  reviewable_count: 0,
  manual_reviewable_count: 1,
  cards: [
    {
      manual_editable: true,
      reviewable: false,
      partial_record: { bicycle_number: '58397', actual_minutes: 2, distance_km: 0.37 },
      errors: ['시각 누락'],
    },
  ],
  message: '일부 항목을 읽지 못했습니다.',
};
context.renderDraft();
assert.equal(element('ocrManual').hidden, false);
element('ocrManual').onclick();
assert.equal(context.state.manualMode, true);
assert.equal(element('ocrReview').hidden, false);
const inputs = element('editFields').children.filter((n) => n.tag === 'input');
assert.equal(inputs.length, 9);
assert.equal(inputs.find((n) => n.id === 'edit_bicycle_number').value, '58397');
assert.equal(inputs.find((n) => n.id === 'edit_started_at').value, '');
assert.match(element('editModeHint').textContent, /빈칸을 원본 사진/);
console.log(
  'UI checks passed: personal explanations, photo recovery, manual editing of incomplete OCR, and blank dates.',
);

// Route updates combine modes and always put the shortest duration first.
vm.runInContext(
  source.slice(sourcePosition('function routeKey('), sourcePosition('function showRoutes(')),
  context,
);
const route = (minutes, id) => ({
  total_minutes: minutes,
  legs: [{ mode: 'bus', from: 'A', to: id, trip_id: id, start: 's', arrival: 'a' }],
});
const slow = route(15, 'slow'),
  fast = route(8, 'fast'),
  middle = route(12, 'middle');
assert.equal(
  JSON.stringify(
    context
      .orderedRoutes({ routes: [slow, middle], mixed_routes: [fast, slow] })
      .map((r) => r.total_minutes),
  ),
  '[8,12,15]',
);
assert.equal(
  context.routeKey({ ...slow, geometry_summary: { new: true } }),
  context.routeKey(slow),
);

// Polling delivers progress before the final response without releasing busy state early.
const walking = (from, to, minutes) => ({ mode: 'walk', from, to, minutes });
const train = { mode: 'subway', from: 'S', to: 'T', trip_id: 'same-train' };
const direct = { total_minutes: 10, legs: [walking('O', 'S', 1), train, walking('T', 'Z', 2)] };
const detour = { total_minutes: 12, legs: [walking('O', 'S', 1), train, walking('T', 'P', 1), walking('P', 'T', 1), walking('T', 'Z', 2)] };
const relay = { total_minutes: 10, legs: [walking('O', 'X', 1), walking('X', 'S', 1), train, walking('T', 'Z', 2)] };
assert.equal(context.routeKey(direct), context.routeKey(detour));
assert.equal(context.routeKey(direct), context.routeKey(relay));
assert.equal(JSON.stringify(context.orderedRoutes({ routes: [detour, relay, direct] })), JSON.stringify([direct]));
assert.notEqual(context.routeKey(direct), context.routeKey({ ...direct, legs: [walking('O', 'S', 1), { ...train, trip_id: 'other-train' }, walking('T', 'Z', 2)] }));
const changedTiming = { ...direct, legs: direct.legs.map((leg) => ({ ...leg, start: 'new' })) };
assert.equal(context.routeKey(direct), context.routeKey(changedTiming));
assert.notEqual(context.routeDisplayKey(direct), context.routeDisplayKey(changedTiming));

(async () => {
  const updates = [],
    states = [];
  const queue = [
    { job_id: 'j' },
    { status: 'running', revision: 1, partial_result: { routes: [slow] } },
    { status: 'running', revision: 2, partial_result: { routes: [slow, fast] } },
    { status: 'done', result: { routes: [fast, slow] } },
  ];
  context.state.activeJob = null;
  context.busy = (v) => states.push(v);
  context.api = async () => queue.shift();
  context.setTimeout = (fn) => {
    fn();
    return 0;
  };
  vm.runInContext(
    source.slice(
      sourcePosition('async function runJob('),
      sourcePosition("$('routeForm').onsubmit="),
    ),
    context,
  );
  const final = await context.runJob('/api/routes', {}, 'jobStatus', 'working', (value) =>
    updates.push(value.routes.length),
  );
  assert.deepEqual(updates, [1, 2]);
  assert.deepEqual(states, [true, false]);
  assert.equal(final.routes[0].total_minutes, 8);
  assert.equal(context.state.activeJob, null);
  process.stdout.write(
    'Live route UI checks passed: progress polling and shortest-first sorting.\n',
  );
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
