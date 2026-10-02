'use strict';
// Run with: node taskboard/frontend/tests/test_directory_scan.cjs
// No browser, backend, dependencies or network calls required.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../static/dashboard.html'), 'utf8');
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).filter(script => script.trim());
scripts.forEach(script => new vm.Script(script));
const code = scripts.join('\n');
const start = code.indexOf('    function directoryScanSupported()');
const end = code.indexOf('    function fillArchiveSettings', start);
assert.ok(start >= 0 && end > start, 'Directory-scan editor functions exist');
// Also guard the actual job DTO integration, not only the standalone collector.
assert.match(code, /directory_scan: collectDirectoryScanSettings\(\)/);
assert.match(code, /fillDirectoryScanSettings\(job\?\.directory_scan \|\| \{\}\)/);
assert.match(code, /const scanError = directoryScanValidationError\(job\.directory_scan\)/);
let mode = 'copy';
let archive = false;
let instant = '2026-03-09T03:30:00Z';
class Clock extends Date {
  constructor(...args) { super(...(args.length ? args : [instant])); }
}
const controlIds = new Set([
  'directoryScanEnabled', 'directoryScanPathTemplate', 'directoryScanLookbackDays',
  'directoryScanTimezone', 'directoryScanFullEnabled', 'directoryScanFullIntervalHours',
  'directoryScanFullIgnoreAge',
]);
const nodes = {};
for (const match of html.matchAll(/id="(directoryScan[^"]+)"/g)) {
  const id = match[1];
  nodes[id] = {
    id, value: '', attrs: {}, disabled: false, hidden: false, textContent: '',
    setAttribute(key, value) { this.attrs[key] = value; },
    getAttribute(key) { return this.attrs[key]; },
    classList: { toggle() {} },
  };
}
const context = {
  Intl, Date: Clock, Number, String, Boolean, Object, Error,
  selectedJobKind: () => mode === 'command' ? 'command' : 'backup',
  selectedTransferMode: () => mode,
  formValue: id => String(nodes[id].value).trim(),
  isTogglePressed: id => id === 'archiveEnabled' ? archive : nodes[id].attrs['aria-pressed'] === 'true',
  setTogglePressed: (id, pressed) => nodes[id].attrs['aria-pressed'] = pressed ? 'true' : 'false',
  document: {
    getElementById: id => nodes[id],
    querySelectorAll: () => Object.values(nodes).filter(node => controlIds.has(node.id)),
  },
};
vm.createContext(context);
vm.runInContext(code.slice(start, end), context);
const collect = () => JSON.parse(JSON.stringify(context.collectDirectoryScanSettings()));
const defaults = {
  enabled: false, path_template: '%Y-%m-%d', lookback_days: 7, timezone: 'UTC',
  full_scan_enabled: true, full_scan_interval_hours: 168, full_scan_ignore_age: false,
};
context.fillDirectoryScanSettings();
assert.deepEqual(collect(), defaults);
const custom = {
  ...defaults, enabled: true, path_template: 'cameras/*/%Y/%m/%d', lookback_days: 3650,
  timezone: 'Europe/Moscow', full_scan_interval_hours: 87600, full_scan_ignore_age: true,
};
context.fillDirectoryScanSettings(custom);
assert.deepEqual(collect(), custom);
// Simulate save/load and clone through JSON DTO serialization.
const clone = JSON.parse(JSON.stringify({ directory_scan: collect() }));
context.fillDirectoryScanSettings(clone.directory_scan);
assert.deepEqual(collect(), custom);
for (const template of ['%Y-%m-%d', '%Y/%m/%d', '%Y%m%d', 'cameras/*/%Y-%m-%d', 'CAM_1/a.b/%Y-%m-%d', 'cam..era/%Y-%m-%d', 'a'.repeat(248) + '%Y-%m-%d']) {
  assert.equal(context.directoryScanValidationError({ ...defaults, path_template: template }), '', template);
}
for (const template of ['', '/x/%Y-%m-%d', '../%Y-%m-%d', 'x/**/%Y-%m-%d', 'x/[a]/%Y-%m-%d',
  'x/{a}/%Y-%m-%d', '%Y-%m', '%Y-%m-%d-%H', 'C:/%Y-%m-%d', 'x\\%Y-%m-%d',
  'x ?/%Y-%m-%d', 'x?/%Y-%m-%d', 'камера/%Y-%m-%d', 'x\n/%Y-%m-%d',
  '%Y//%m/%d', '%Y/%m/%d/', '%Y/./%m/%d', 'a'.repeat(249) + '%Y-%m-%d']) {
  assert.ok(context.directoryScanValidationError({ ...defaults, path_template: template }), template);
}
for (const value of [0, 3651, 1.5, NaN]) {
  assert.ok(context.directoryScanValidationError({ ...defaults, lookback_days: value }));
}
for (const value of [0, 87601, 1.5, NaN]) {
  assert.ok(context.directoryScanValidationError({ ...defaults, full_scan_interval_hours: value }));
}
for (const timezone of ['UTC', 'Europe/Moscow', 'America/New_York', 'Etc/GMT+5']) {
  assert.equal(context.directoryScanValidationError({ ...defaults, timezone }), '');
}
for (const timezone of ['', 'Bad/Timezone', '+03:00']) {
  assert.ok(context.directoryScanValidationError({ ...defaults, timezone }));
}
for (const unsupported of ['sync', 'command']) {
  mode = unsupported;
  context.updateDirectoryScanUI();
  assert.equal(collect().enabled, false);
  assert.equal(nodes.directoryScanEnabled.disabled, true);
  assert.equal(nodes.directoryScanWarning.hidden, false);
  assert.equal(nodes.directoryScanEnabled.attrs['aria-pressed'], 'true');
  assert.equal(collect().path_template, custom.path_template);
}
mode = 'copy'; archive = true;
context.updateDirectoryScanUI();
assert.equal(collect().enabled, false);
archive = false;
context.updateDirectoryScanUI();
assert.deepEqual(collect(), custom);
assert.equal(nodes.directoryScanEnabled.disabled, false);
assert.equal(nodes.directoryScanWarning.hidden, true);
context.fillDirectoryScanSettings({ ...custom, full_scan_enabled: false });
context.updateDirectoryScanUI();
assert.equal(nodes.directoryScanFullIntervalHours.disabled, true);
assert.equal(nodes.directoryScanFullIgnoreAge.disabled, true);
assert.equal(collect().full_scan_ignore_age, true, 'Disabled preference is retained');
// Disabled invalid drafts must not block saving unrelated job fields.
const validationStart = code.indexOf('    function validateJob(job)');
const validationEnd = code.indexOf('    async function upsertCurrentJob', validationStart);
context.queueDefinitions = () => [{ key: 'default', enabled: true }];
vm.runInContext(code.slice(validationStart, validationEnd), context);
const invalidDraft = { ...defaults, path_template: '../bad', timezone: 'Bad/Timezone', lookback_days: 0 };
context.fillDirectoryScanSettings(invalidDraft);
context.updateDirectoryScanUI();
assert.deepEqual(collect(), invalidDraft);
const unrelatedJob = {
  key: 'draft', title: 'Changed title', profile: 'default', kind: 'command',
  command: ['true'], schedule: { enabled: false }, directory_scan: collect(),
};
assert.doesNotThrow(() => context.validateJob(unrelatedJob));
assert.throws(() => context.validateJob({ ...unrelatedJob, directory_scan: { ...invalidDraft, enabled: true } }));
assert.ok(nodes.directoryScanPreview.textContent.includes('Черновик'));
// Backend subtracts elapsed 24-hour days, then converts endpoints into local dates.
// Midnight-adjacent DST examples intentionally span fewer/more civil dates.
for (const sample of [
  { now: '2026-03-09T03:30:00Z', timezone: 'America/New_York', days: 3, first: '2026-03-05', last: '2026-03-08', count: 4 },
  { now: '2026-03-09T04:30:00Z', timezone: 'America/New_York', days: 1, first: '2026-03-07', last: '2026-03-09', count: 3 },
  { now: '2026-11-02T04:30:00Z', timezone: 'America/New_York', days: 3, first: '2026-10-30', last: '2026-11-01', count: 3 },
  { now: '2026-11-02T04:30:00Z', timezone: 'America/New_York', days: 1, first: '2026-11-01', last: '2026-11-01', count: 1 },
  { now: '2026-01-01T00:30:00Z', timezone: 'UTC', days: 2, first: '2025-12-30', last: '2026-01-01', count: 3 },
  { now: '2026-01-01T00:30:00Z', timezone: 'UTC', days: 1, first: '2025-12-31', last: '2026-01-01', count: 2 },
  { now: '2026-01-01T00:30:00Z', timezone: 'America/New_York', days: 1, first: '2025-12-30', last: '2025-12-31', count: 2 },
]) {
  instant = sample.now;
  context.fillDirectoryScanSettings({ ...defaults, enabled: true, timezone: sample.timezone, lookback_days: sample.days });
  context.updateDirectoryScanUI();
  const preview = nodes.directoryScanPreview.textContent;
  assert.ok(preview.includes(sample.first), preview);
  assert.ok(preview.includes(sample.last), preview);
  assert.equal(preview.includes(' … '), sample.count > 1, preview);
  assert.ok(preview.includes(`(${sample.count})`), preview);
}
console.log('PASS: inline JS syntax; directory-scan defaults/DTO clone roundtrip; template, range and timezone validation; unsupported mode/archive preservation; full-scan controls; UTC/DST previews');
