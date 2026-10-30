'use strict';
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const context = vm.createContext({});
vm.runInContext(source.slice(source.indexOf('function displayLegs('), source.indexOf('function routeKey(')), context);
const at = (minutes) => new Date(Date.UTC(2026, 9, 30, 4) + minutes * 60000).toISOString();
const ride = (mode, from, to, start, departure, arrival, trip = 'train') => ({
  mode, from, to, start: at(start), departure: at(departure), arrival: at(arrival),
  trip_id: trip, minutes: arrival - start, wait_minutes: departure - start,
});
const alight = (where, when) => ({ mode: 'alight', from: where, to: where, start: at(when), arrival: at(when), minutes: 0 });
const modes = (rows) => Array.from(rows, (r) => r.mode).join(',');
const sum = (rows) => rows.reduce((total, r) => total + r.minutes, 0);
const close = (a, b) => assert.ok(Math.abs(a - b) < 1e-8, `${a} != ${b}`);

// The reported station pair: 6m50s preparation/wait + 2m aboard.
const original = { legs: [ride('subway', 'A', 'B', 0, 41 / 6, 53 / 6)] };
const frozen = JSON.stringify(original);
let rows = context.displayLegs(original);
assert.equal(modes(rows), 'wait,subway');
close(rows[0].minutes, 41 / 6);
close(rows[1].minutes, 2);
assert.equal(rows[1].start, original.legs[0].departure);
assert.equal(rows[0].wait_for, 'subway');
close(sum(rows), 53 / 6);
assert.equal(JSON.stringify(original), frozen, 'Presentation must not mutate optimizer output');

// Staying on one vehicle includes its intermediate dwell in riding time.
rows = context.displayLegs({ legs: [
  ride('subway', 'A', 'B', 0, 3, 5),
  ride('subway', 'B', 'C', 5, 5.5, 7.5),
] });
assert.equal(modes(rows), 'wait,subway');
close(rows[0].minutes, 3);
close(rows[1].minutes, 4.5);
assert.equal(rows[1].to, 'C');
close(sum(rows), 7.5);

// A transfer has its own wait; buses follow the same rules as trains.
rows = context.displayLegs({ legs: [
  ride('subway', 'A', 'B', 0, 2, 4), alight('B', 4),
  { mode: 'walk', from: 'B', to: 'C', start: at(4), arrival: at(5), minutes: 1 },
  ride('bus', 'C', 'D', 5, 8, 10, 'bus'),
] });
assert.equal(modes(rows), 'wait,subway,walk,wait,bus');
close(rows[3].minutes, 3);
close(rows[4].minutes, 2);
close(sum(rows), 10);

// Explicit and embedded waits are combined rather than counted twice.
rows = context.displayLegs({ legs: [
  { mode: 'wait', from: 'A', to: 'A', start: at(0), arrival: at(1), minutes: 1 },
  ride('bus', 'A', 'B', 1, 4, 6, 'bus'),
] });
assert.equal(modes(rows), 'wait,bus');
close(rows[0].minutes, 4);
close(sum(rows), 6);

// Getting off and boarding again is not the same as staying aboard.
rows = context.displayLegs({ legs: [
  ride('subway', 'A', 'B', 0, 2, 4), alight('B', 4),
  ride('subway', 'B', 'C', 4, 7, 9),
] });
assert.equal(modes(rows), 'wait,subway,wait,subway');
close(sum(rows), 9);

// Legacy results without departure can use their saved wait metadata.
const legacy = ride('bus', 'A', 'B', 0, 3, 5, 'bus');
delete legacy.departure;
rows = context.displayLegs({ legs: [legacy] });
assert.equal(modes(rows), 'wait,bus');
close(rows[1].minutes, 2);
delete legacy.wait_minutes;
rows = context.displayLegs({ legs: [legacy] });
assert.equal(modes(rows), 'bus');
close(sum(rows), 5);
console.log('Transit detail checks passed: separate waiting/riding, onboard dwell, transfers, no double counting, unchanged inputs.');
