'use strict';
// Run with: node taskboard/frontend/tests/test_retention.cjs
// Isolated editor/progress tests: no browser, backend, dependencies or network.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../static/dashboard.html'), 'utf8');
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).filter(s => s.trim());
scripts.forEach(script => new vm.Script(script));
const code = scripts.join('\n');
function extract(name) {
  const start = code.indexOf(`    function ${name}(`);
  assert.ok(start >= 0, `${name} exists`);
  const following = [code.indexOf('\n    function ', start + 1), code.indexOf('\n    async function ', start + 1)].filter(n => n >= 0);
  return code.slice(start, Math.min(...following));
}
const nodes = {};
for (const m of html.matchAll(/id="(retention[^"]+)"/g)) {
  const id = m[1];
  nodes[id] = { id, value: '', attrs: {}, disabled: false, textContent: '', innerHTML: '', style: {},
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; },
    classList: { toggle() {} } };
}
nodes.copyProgressList = { innerHTML: '' };
const policyIds = new Set(['retentionIntervalEnabled', 'retentionIntervalHours', 'retentionDirectoryScanEnabled',
  'retentionDirectoryScanPathTemplate', 'retentionDirectoryScanTimezone', 'retentionDirectoryScanOverlapDays',
  'retentionDirectoryScanFullEnabled', 'retentionDirectoryScanFullIntervalHours']);
let mode = 'copy';
const state = { jobs: [], editingKey: null, lastSnapshot: { active_operations: [] }, selectedRcloneLogStepId: null };
const context = {
  Intl, Date, Number, String, Boolean, Object, JSON, Error, Array, state,
  selectedJobKind: () => mode === 'command' ? 'command' : 'backup',
  selectedTransferMode: () => mode,
  formValue: id => String(nodes[id].value).trim(),
  isTogglePressed: id => nodes[id]?.attrs['aria-pressed'] === 'true',
  setTogglePressed: (id, pressed) => nodes[id].attrs['aria-pressed'] = pressed ? 'true' : 'false',
  normalizeOptionalInteger: v => v === '' ? null : Number(v),
  normalizeOptionalFloat: v => v === '' ? null : Number(v), normalizeDebugDump: v => v || null,
  document: { getElementById: id => nodes[id], querySelectorAll: () => Object.values(nodes).filter(n => policyIds.has(n.id)) },
  queueDefinitions: () => [{ key: 'lite', enabled: true }], buildExcludePathPatterns: () => [],
  formatRunTimestamp: v => `DATE(${v})`, formatCompactNumber: n => String(n),
  formatRunDisplayId: v => String(v), escapeHtml: v => String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'),
  formatRunStatusLabel: v => v, formatRunHistoryTimestamp: v => v || '',
  formatRunTransferStats: () => 'BOGUS 0 B', formatRunTransferStatsCompact: () => 'transfer stats',
  runHistorySummary: () => '', runHistoryLogStepId: () => null, runHistoryTriggerTagLabel: () => '',
  runHistorySummaryTooltip: () => '', statusClass: () => 'ok',
  renderJobs: () => {}, copyProgressPercent: () => 40, copyProgressSummary: () => 'copy stats',
  copyResourceSummary: () => '', antiBotDelayLabel: () => '', formatProgressPercent: n => `${n}%`,
};
vm.createContext(context);
for (const name of ['collectRetentionRcloneOptions', 'retentionPolicySupported', 'collectRetentionDirectoryScanSettings',
  'fillRetentionPolicySettings', 'parseRetentionAgeSeconds', 'retentionPolicyValidationError', 'updateRetentionPolicyUI',
  'retentionPolicyScheduleLabel', 'renderRetentionPolicyRuntimeStatus', 'directoryScanValidationError', 'validateJob',
  'cleanupOperationLabel', 'isCleanupOperationActive', 'retentionOperationForRun', 'retentionPolicyProgressLabel', 'cleanupProgressSummary',
  'cleanupProgressCard', 'renderCopyProgress', 'runHistorySummaryLabel', 'runHistoryTileViewModel', 'jobCleanupLabel',
  'formatRunStepDetails', 'applyLatestJobRuns']) {
  vm.runInContext(extract(name), context);
}
const json = value => JSON.parse(JSON.stringify(value));
const collect = () => json(context.collectRetentionRcloneOptions(mode === 'command' ? 'command' : 'backup'));
const defaults = { enabled: false, path_template: '%Y-%m-%d', timezone: 'UTC', overlap_days: 1,
  full_scan_enabled: true, full_scan_interval_hours: 168 };
context.fillRetentionPolicySettings();
assert.equal(collect().interval_enabled, false);
assert.equal(collect().interval_hours, 24);
assert.deepEqual(collect().directory_scan, defaults);
const customScan = { ...defaults, enabled: true, path_template: 'cameras/*/%Y/%m/%d', timezone: 'Europe/Moscow',
  overlap_days: 3650, full_scan_interval_hours: 87600 };
const custom = { enabled: true, min_age: '1w2d12.5h', interval_enabled: true, interval_hours: 48, directory_scan: customScan };
context.fillRetentionPolicySettings(custom);
context.setTogglePressed('retentionEnabled', true); nodes.retentionMinAge.value = custom.min_age;
assert.equal(collect().interval_hours, 48);
assert.deepEqual(collect().directory_scan, customScan);
const clone = json({ retention: collect() });
context.fillRetentionPolicySettings(clone.retention);
assert.deepEqual(collect().directory_scan, customScan);
assert.equal(context.retentionPolicyValidationError(custom), '');

// The exact copy path/timezone validator is reused, with no lookback setting.
for (const template of ['%Y-%m-%d', '%Y/%m/%d', 'cam..era/%Y-%m-%d', 'cam*/%Y%m%d']) {
  assert.equal(context.retentionPolicyValidationError({ ...custom, directory_scan: { ...customScan, path_template: template } }), '', template);
}
for (const template of ['/%Y/%m/%d', '%Y//%m/%d', '%Y/%m/%d/', '%Y/../%m/%d', '%Y/./%m/%d', '%Y/**/%m/%d',
  '%Y/%m', '%Y-%m-%d/%H', 'cam ?/%Y-%m-%d', 'a'.repeat(249) + '%Y-%m-%d']) {
  assert.ok(context.retentionPolicyValidationError({ ...custom, directory_scan: { ...customScan, path_template: template } }), template);
}
for (const timezone of ['Bad/Timezone', '../UTC', '', 'Not A Zone']) {
  assert.ok(context.retentionPolicyValidationError({ ...custom, directory_scan: { ...customScan, timezone } }));
}
for (const hours of [0, 87601, 1.5]) {
  assert.ok(context.retentionPolicyValidationError({ ...custom, interval_hours: hours }));
  assert.ok(context.retentionPolicyValidationError({ ...custom, directory_scan: { ...customScan, full_scan_interval_hours: hours } }));
}
for (const days of [-1, 3651, 0.5]) {
  assert.ok(context.retentionPolicyValidationError({ ...custom, directory_scan: { ...customScan, overlap_days: days } }));
}
assert.equal(context.retentionPolicyValidationError({ ...custom, directory_scan: { ...customScan, overlap_days: 0 } }), '');
for (const valid of ['7d', '1w2d12h', '1.5h', '.5m', '1d1h1m1s', '0.1s', '1h0m', ' 7d ']) {
  assert.ok(context.parseRetentionAgeSeconds(valid) > 0, valid);
  assert.equal(context.retentionPolicyValidationError({ ...custom, min_age: valid }), '', valid);
}
for (const invalid of ['0s', '-1d', '7', '2026-09-25', '1ms', '1M', '1month', '1y', '1e3s', '1d 2h', 'Infinityh', '']) {
  assert.equal(context.parseRetentionAgeSeconds(invalid), null, invalid);
  assert.ok(context.retentionPolicyValidationError({ ...custom, min_age: invalid }), invalid);
}
// Only active settings are validated; disabled malformed drafts are saveable.
const bad = { enabled: false, min_age: 'date', interval_enabled: true, interval_hours: 0,
  directory_scan: { ...customScan, path_template: '../bad', overlap_days: -1 } };
assert.equal(context.retentionPolicyValidationError(bad), '');
assert.equal(context.retentionPolicyValidationError({ ...bad, enabled: true, interval_enabled: false,
  directory_scan: { ...bad.directory_scan, enabled: false } }), '');
assert.equal(context.retentionPolicyValidationError({ ...custom, interval_enabled: false, interval_hours: 0,
  directory_scan: { ...customScan, full_scan_enabled: false, full_scan_interval_hours: 0 } }), '');
const job = { key: 'test', title: 'Test', kind: 'backup', profile: 'lite', transfer_mode: 'copy', source_path: '/src',
  destination_path: 'remote:/dst', retention: bad, schedule: { enabled: false }, options: {} };
context.validateJob(job);
assert.throws(() => context.validateJob({ ...job, retention: { ...bad, enabled: true } }));
assert.throws(() => context.validateJob({ ...job, transfer_mode: 'sync', retention: custom }));

// Turning retention off or switching task kind disables controls, not drafts.
context.fillRetentionPolicySettings(custom);
context.setTogglePressed('retentionEnabled', true);
for (const unsupported of ['sync', 'command']) {
  mode = unsupported; context.updateRetentionPolicyUI();
  assert.equal(collect().enabled, false);
  assert.deepEqual(collect().directory_scan, customScan);
  assert.ok([...policyIds].every(id => nodes[id].disabled));
}
mode = 'copy'; context.updateRetentionPolicyUI();
assert.equal(collect().enabled, true);
assert.equal(nodes.retentionDirectoryScanPathTemplate.disabled, false);
context.setTogglePressed('retentionEnabled', false); context.updateRetentionPolicyUI();
assert.ok([...policyIds].every(id => nodes[id].disabled));
assert.deepEqual(collect().directory_scan, customScan);
context.setTogglePressed('retentionEnabled', true); context.setTogglePressed('retentionIntervalEnabled', false);
context.setTogglePressed('retentionDirectoryScanFullEnabled', false); context.updateRetentionPolicyUI();
assert.equal(nodes.retentionIntervalHours.disabled, true);
assert.equal(nodes.retentionDirectoryScanFullIntervalHours.disabled, true);
assert.equal(nodes.retentionDirectoryScanOverlapDays.disabled, false);
context.setTogglePressed('retentionDirectoryScanEnabled', false); context.updateRetentionPolicyUI();
assert.equal(nodes.retentionDirectoryScanPathTemplate.disabled, true);
assert.equal(nodes.retentionDirectoryScanEnabled.disabled, false);

// Retention is a separate operation, not a 0 B transfer/progress percentage.
const cleanup = { step_id: 10, run_id: 2, job_key: 'test', title: 'Test', profile: 'lite', status: 'running',
  step_kind: 'retention', copy_completed: true, listed_count: 12, deleted_count: 2,
  retention_policy: { mode: 'window' } };
assert.equal(context.cleanupOperationLabel(cleanup), 'Копирование завершено • очистка облака');
assert.equal(context.cleanupOperationLabel({ ...cleanup, copy_completed: false }), 'Очистка облака');
assert.equal(context.cleanupOperationLabel({ ...cleanup, phase_label: 'Серверный этап' }), 'Серверный этап');
assert.equal(context.cleanupOperationLabel({ ...cleanup, operation_label: 'Очистка' }), 'Очистка');
const summary = context.cleanupProgressSummary(cleanup);
assert.ok(summary.includes('Просмотрено элементов: 12'));
assert.ok(summary.includes('Удалено файлов: 2'));
assert.ok(summary.includes('Выборочный обход'));
assert.ok(!summary.includes('0 B'));
context.renderCopyProgress([], [cleanup]);
assert.ok(nodes.copyProgressList.innerHTML.includes('Копирование завершено'));
assert.ok(!nodes.copyProgressList.innerHTML.includes('copy-progress-bar'));
assert.ok(!nodes.copyProgressList.innerHTML.includes('0 B'));
context.renderCopyProgress([{ step_id: 9, run_id: 1, job_key: 'other', status: 'running' }], [cleanup]);
assert.ok(nodes.copyProgressList.innerHTML.includes('copy-progress-bar'));
assert.ok(nodes.copyProgressList.innerHTML.includes('Копирование завершено'));
state.lastSnapshot.active_operations = [{ ...cleanup, status: 'queued', copy_completed: false }];
assert.equal(context.runHistorySummaryLabel({ id: 2, status: 'running' }), 'transfer stats');
assert.equal(context.jobCleanupLabel({ key: 'test' }), '');
context.renderCopyProgress([{ step_id: 9, run_id: 2, job_key: 'test', status: 'running' }], state.lastSnapshot.active_operations);
assert.ok(!nodes.copyProgressList.innerHTML.includes('Копирование завершено'));
assert.ok(!nodes.copyProgressList.innerHTML.includes('Удалено файлов'));
assert.equal(context.isCleanupOperationActive({ ...cleanup, status: 'queued', copy_completed: true }), true);
state.lastSnapshot.active_operations = [cleanup];
assert.equal(context.runHistorySummaryLabel({ id: 2, status: 'running' }), 'Копирование завершено • очистка облака');
assert.equal(context.runHistorySummaryLabel({ id: 2, status: 'succeeded' }), 'transfer stats');
assert.equal(context.jobCleanupLabel({ key: 'test' }), 'Копирование завершено • очистка облака');
assert.equal(context.runHistoryTileViewModel({ id: 2, status: 'running' }).statusLabel, 'очистка');
const due = { mode: 'skip_interval', reason: 'interval has not elapsed', next_due_at: '2030-01-02T00:00:00Z' };
assert.ok(context.retentionPolicyProgressLabel(due).includes('Очистка отложена до'));
const details = context.formatRunStepDetails({ ...cleanup, step_order: 2, description: 'Cleanup', status: 'skipped',
  progress: { retention_policy: due }, retention_policy: undefined });
assert.ok(details.includes('Очистка отложена до'));
assert.ok(!details.includes('BOGUS 0 B'));
state.editingKey = 'test'; state.jobs = [{ key: 'test' }];
context.applyLatestJobRuns({ latest_job_runs: { test: { run_id: 2, status: 'running' } },
  backup_jobs: [{ key: 'test', retention_status: { next_due_at: due.next_due_at, last_success_at: '2026-01-01T00:00:00Z' } }] });
context.renderRetentionPolicyRuntimeStatus();
assert.ok(nodes.retentionPolicyRuntimeStatus.textContent.includes('Очистка отложена до'));
assert.ok(nodes.retentionPolicyRuntimeStatus.textContent.includes('Последняя успешная очистка'));
assert.equal(state.jobs[0].last_run_phase, 'Копирование завершено • очистка облака');

assert.match(code, /fillRetentionPolicySettings\(retention\)/);
assert.match(code, /directory_scan: collectRetentionDirectoryScanSettings\(\)/);
assert.match(code, /renderRetentionPolicyRuntimeStatus\(\);/);
console.log('PASS: inline JS syntax; retention DTO defaults/load/save/clone; active-only validators; fixed-duration age; unsupported/off drafts; control dependencies; copy/cleanup phases, counters and skipped-interval display');
