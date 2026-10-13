'use strict';
// Run: node taskboard/frontend/tests/test_downloads.cjs
// Isolated editor and browse tests; no backend, network or dependencies.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../static/dashboard.html'), 'utf8');
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).filter(s => s.trim());
scripts.forEach(script => new vm.Script(script));
const code = scripts.join('\n');
function extract(name) {
  const marker = new RegExp(`    (?:async )?function ${name}\\(`);
  const start = code.search(marker);
  assert.ok(start >= 0, `${name} exists`);
  const remaining = code.slice(start + 1);
  const next = remaining.search(/\n    (?:async )?function /);
  return next < 0 ? code.slice(start) : code.slice(start, start + 1 + next);
}
const nodes = {};
for (const m of html.matchAll(/<(\w+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
  const attrs = Object.fromEntries([...m[2].matchAll(/([\w-]+)="([^"]*)"/g)].map(x => [x[1], x[2]]));
  nodes[m[3]] = { id: m[3], tagName: m[1].toUpperCase(), _value: attrs.value || '',
    get value() { return this._value; }, set value(v) { this._value = String(v); }, attrs,
    hidden: false, disabled: false, textContent: '', innerHTML: '', style: {}, dataset: {},
    getAttribute(k) { return this.attrs[k]; }, setAttribute(k, v) { this.attrs[k] = v; },
    classList: { toggle() {}, add() {}, remove() {} }, focus() {} };
}
let toasts = [];
let fetches = [];
let browseCalls = [];
const state = { jobs: [], clouds: [{ key: 'mail', remote_name: 'mail', root_path: '/MYSPACE' },
  { key: 'other', remote_name: 'other', root_path: '' }], excludePathEntries: [],
  editingKey: null, lastSnapshot: null, browseTarget: 'source', browseEntries: [], browseParent: null };
const noOp = () => {};
const ctx = {
  Intl, Date, Number, String, Boolean, Object, Array, JSON, Error, Set, URLSearchParams, state,
  document: { getElementById: id => nodes[id], querySelector: () => null, querySelectorAll: () => [], body: { style: {} } },
  window: { scrollTo() {} }, weekdayGrid: { querySelectorAll: () => [] },
  normalizeOptionalInteger: v => v === '' ? null : Number(v), normalizeOptionalFloat: v => v === '' ? null : Number(v),
  normalizeDebugDump: v => v || null, syncKeyFromTitle: noOp,
  syncExcludePathDraft: noOp, renderExcludePathEntries: noOp, renderMailruOptionsHint: noOp,
  updateDirectoryScanUI: noOp, updateRetentionPolicyUI: noOp, refreshAllCustomSelects: noOp,
  setEditorVisibility: noOp, resetEditorCollapsibles: noOp, updateRunsLayout: noOp,
  updateJobKindVisibility: noOp, updateScheduleVisibility: noOp,
  renderJobCloudOptions: key => { nodes.jobCloud.value = key; },
  renderQueueOptions: key => { nodes.jobProfile.value = key; }, getDefaultQueueKey: () => 'lite', nextOrder: () => 10,
  commandPreview: job => `rclone ${job.transfer_mode || 'copy'} ${job.source_path || ''} ${job.destination_path || ''}`,
  renderRetentionPolicyRuntimeStatus: noOp, queueDefinitions: () => [{ key: 'lite', enabled: true }],
  renderJobs: noOp, checkedWeekdays: () => [], setToast: (m, tone) => toasts.push({ m, tone }),
  renderBrowseList: noOp, setBrowseSelection: noOp,
  fetch: async url => { fetches.push(url); return { ok: true, json: async () => ({ path: 'VIDEOS', parent: '', directories: [{ path: 'VIDEOS/Clips', name: 'Clips' }], files: [{ path: 'VIDEOS/one.mp4', name: 'one.mp4', size_bytes: 100 }] }) }; },
};
vm.createContext(ctx);
const functions = ['formValue','isTogglePressed','setTogglePressed','escapeHtml','normalizePosixPath',
  'selectedJobKind','selectedTransferMode','selectedTransferDirection','findCloudByKey','findCloudByRemoteName',
  'inferCloudByDestination','composeCloudDestination','remotePathParts','relativeSourcePath','cloudRelativeSubpath',
  'currentDownloadSource','normalizeExcludePathEntry','buildExcludePathPatterns','addExcludePathEntry',
  'downloadOverlaps','renderDownloadOverlaps','addDownloadOverlapExclude',
  'collectJobRcloneOptions','fillJobRcloneOptions','collectRetentionRcloneOptions','collectRetentionDirectoryScanSettings',
  'fillRetentionPolicySettings','fillRetentionRcloneOptions','collectArchiveSettings','fillArchiveSettings',
  'collectDirectoryScanSettings','fillDirectoryScanSettings','directoryScanSupported','directoryScanValidationError',
  'retentionPolicySupported','parseRetentionAgeSeconds','retentionPolicyValidationError',
  'collectFormJob','fillForm','updateTransferDirection','downloadValidationError','validateJob',
  'transferDirectionLabel','transferOperationLabel','browserUsesCloud','browserSourceSubpath','browserCloudPathAllowed',
  'updateBrowseChrome','openBrowser','closeBrowser','browseUp','chooseCurrentDirectory','browse','selectBrowseEntry'];
functions.forEach(name => vm.runInContext(extract(name), ctx));
const plain = v => JSON.parse(JSON.stringify(v));
function standardJob(extra = {}) {
  return { key: 'job', title: 'Задача', kind: 'backup', transfer_mode: 'copy', source_path: '/mnt/source',
    destination_path: 'mail:/MYSPACE/PHOTO', cloud_key: 'mail', destination_subpath: 'PHOTO',
    profile: 'lite', enabled: true, options: {}, schedule: { enabled: false, mode: 'manual', weekdays: [] }, ...extra };
}
function set(id, value) { nodes[id].value = String(value); }
function resetForm(job) {
  state.jobs = job ? [job] : [];
  ctx.fillForm(job || null);
  set('jobProfile', 'lite'); set('jobKind', job?.transfer_mode || 'copy');
}

// Legacy uploads retain paths and DTO behavior without requiring new settings.
resetForm(standardJob());
let dto = plain(ctx.collectFormJob());
assert.equal(dto.transfer_direction, 'upload');
assert.equal(dto.source_path, '/mnt/source');
assert.equal(dto.destination_path, 'mail:/MYSPACE/PHOTO');
assert.equal(dto.source_subpath, null);
assert.equal(dto.destination_mount, null);
assert.doesNotThrow(() => ctx.validateJob(dto));

// Download source cloud root is not the upload destination; local destination is independent.
const upload = standardJob({ transfer_mode: 'sync', options: { max_age: '7d', extra_args: ['--max-depth 2'], exclude: ['*.tmp'],
  exclude_paths: [{ path: '/mnt/source/private', kind: 'directory' }] },
  archive: { enabled: true }, retention: { enabled: true, min_age: '7d' }, watcher_enabled: true,
  directory_scan: { enabled: true } });
resetForm(upload);
set('jobSourceSubpath', 'VIDEOS'); set('jobDownloadDestination', '/mnt/pve/hdd/videos'); set('jobDestinationMount', '/mnt/pve/hdd');
set('transferDirection', 'download'); ctx.updateTransferDirection();
assert.equal(nodes.jobKind.value, 'copy');
assert.equal(nodes.maxAge.value, '');
assert.equal(nodes.extraArgs.value, '');
assert.equal(state.excludePathEntries.length, 0);
dto = plain(ctx.collectFormJob());
assert.equal(dto.transfer_direction, 'download');
assert.equal(dto.source_subpath, 'VIDEOS');
assert.equal(dto.source_path, 'mail:/MYSPACE/VIDEOS');
assert.equal(dto.destination_path, '/mnt/pve/hdd/videos');
assert.equal(dto.destination_subpath, null);
assert.equal(dto.destination_mount, '/mnt/pve/hdd');
for (const section of ['retention','archive','directory_scan']) assert.equal(dto[section].enabled, false);
assert.equal(dto.retention.directory_scan.enabled, false);
assert.equal(dto.watcher_enabled, false);
assert.doesNotThrow(() => ctx.validateJob(dto));
set('maxAge', '30d');
assert.equal(nodes.jobSource.value, '/mnt/source');
assert.equal(nodes.jobDestinationSubpath.value, 'PHOTO');
set('transferDirection', 'upload'); ctx.updateTransferDirection();
assert.equal(nodes.jobKind.value, 'sync');
assert.equal(nodes.maxAge.value, '7d');
assert.equal(nodes.extraArgs.value, '--max-depth 2');
assert.equal(state.excludePathEntries[0].path, '/mnt/source/private');
set('transferDirection', 'download'); ctx.updateTransferDirection();
assert.equal(nodes.maxAge.value, '30d');
assert.equal(nodes.jobDownloadDestination.value, '/mnt/pve/hdd/videos');

// Saved/JSON-cloned download jobs load without swapping source and local destination.
const downloaded = plain(ctx.collectFormJob());
resetForm(JSON.parse(JSON.stringify(downloaded)));
assert.equal(nodes.jobSource.value, '');
assert.equal(nodes.jobDestination.value, '');
assert.equal(nodes.jobDestinationSubpath.value, '');
assert.equal(nodes.jobDownloadDestination.value, '/mnt/pve/hdd/videos');
assert.equal(nodes.jobSourceSubpath.value, 'VIDEOS');
assert.equal(nodes.jobDownloadSourcePreview.value, 'mail:/MYSPACE/VIDEOS');
assert.deepEqual(plain(ctx.collectFormJob()).options, downloaded.options);
assert.equal(ctx.composeCloudDestination('mail', ''), 'mail:/MYSPACE');
assert.equal(ctx.composeCloudDestination('other', ''), 'other:');
assert.equal(ctx.cloudRelativeSubpath('mail', 'mail:/MYSPACE/VIDEOS'), 'VIDEOS');
assert.equal(ctx.cloudRelativeSubpath('mail', 'mail:/MYSPACE_OTHER/VIDEOS'), null);

// Fail closed for unsafe configurations, but leave uploads unchanged.
for (const change of [ { transfer_mode: 'sync' }, { cloud_key: null }, { source_path: '/local' },
  { destination_path: 'mail:/remote' }, { destination_path: '/mnt/pve/hdd/../else' },
  { destination_mount: '/mnt/pve/other' }, { destination_mount: 'relative' }, { source_subpath: '../PRIVATE' },
  { source_subpath: '/PRIVATE' }, { archive: { enabled: true } }, { retention: { enabled: true } },
  { watcher_enabled: true }, { directory_scan: { enabled: true } } ]) {
  assert.ok(ctx.downloadValidationError({ ...downloaded, ...change }), JSON.stringify(change));
}
assert.equal(ctx.downloadValidationError({ ...downloaded, destination_mount: null }), '');
assert.equal(ctx.downloadValidationError({ ...downloaded, destination_mount: '/mnt/pve/hdd/' }), '');

// Cloud exclusions are bounded, anchored and escaped; local exclusion behavior remains legacy.
assert.equal(ctx.relativeSourcePath('mail:/MYSPACE', 'mail:/MYSPACE/BACKUPS'), 'BACKUPS');
assert.equal(ctx.relativeSourcePath('mail:/MYSPACE', 'mail:/MYSPACE_OTHER/BACKUPS'), null);
assert.equal(ctx.relativeSourcePath('mail:/MYSPACE', 'other:/MYSPACE/BACKUPS'), null);
assert.equal(ctx.relativeSourcePath('mail:/MYSPACE', 'mail:/MYSPACE'), '');
assert.equal(ctx.remotePathParts('mail:/MYSPACE/../private'), null);
assert.deepEqual(plain(ctx.buildExcludePathPatterns('mail:/MYSPACE', [
  { path: 'mail:/MYSPACE/BACKUPS/Proxmox', kind: 'directory' },
  { path: 'mail:/MYSPACE/a[1].mp4', kind: 'file' },
  { path: 'other:/MYSPACE/wrong', kind: 'directory' },
])), ['/BACKUPS/Proxmox/**', '/a\\[1\\].mp4']);
assert.deepEqual(plain(ctx.buildExcludePathPatterns('/mnt/source', [{ path: '/mnt/source/private', kind: 'directory' }])), ['private/**']);
state.excludePathEntries = [];
assert.equal(ctx.addExcludePathEntry('mail:/MYSPACE/VIDEOS/Clips','directory'), true);
assert.equal(ctx.addExcludePathEntry('mail:/MYSPACE/VIDEOS','directory'), false);
assert.equal(ctx.addExcludePathEntry('mail:/MYSPACE/VideosOther','directory'), false);

// Overlaps include disabled/archive uploads and never recommend excluding the whole source.
const overlapJobs = [standardJob({ key: 'proxmox', title: 'Proxmox', enabled: false, destination_subpath: 'BACKUPS/Proxmox' }),
  standardJob({ key: 'archive', title: 'Архив', destination_subpath: 'Archives', archive: { enabled: true } }),
  standardJob({ key: 'down', transfer_direction: 'download', destination_path: '/mnt/hdd' }),
  standardJob({ key: 'othercloud', cloud_key: 'other', destination_subpath: 'BACKUPS' })];
let overlaps = plain(ctx.downloadOverlaps('mail:/MYSPACE', overlapJobs));
assert.equal(overlaps.length, 2);
assert.equal(overlaps[0].enabled, false);
assert.equal(overlaps[0].exclude_pattern, '/BACKUPS/Proxmox/**');
assert.equal(overlaps[1].job_key, 'archive');
assert.equal(ctx.downloadOverlaps('mail:/MYSPACE', [], {}, [{ job_key:'annotated', title:'Backend annotation', enabled:false, cloud_path:'mail:/MYSPACE/Archives' }])[0].job_key, 'annotated');
assert.equal(ctx.downloadOverlaps('mail:/MYSPACE', [standardJob({ key:'proxmox', destination_subpath:'NEW' })], {}, [{ job_key:'proxmox', cloud_path:'mail:/MYSPACE/OLD' }])[0].relative_path, 'NEW');
overlaps = plain(ctx.downloadOverlaps('mail:/MYSPACE', overlapJobs, { exclude_paths: [{ path: 'mail:/MYSPACE/BACKUPS', kind: 'directory' }] }));
assert.equal(overlaps[0].covered, true);
assert.equal(overlaps[1].covered, false);
assert.equal(ctx.downloadOverlaps('mail:/MYSPACE/BACKUPS/Proxmox', overlapJobs)[0].whole_source, true);
assert.equal(ctx.downloadOverlaps('mail:/MYSPACE/BACKUPS/Proxmox/old', overlapJobs)[0].exclude_pattern, null);
state.jobs = overlapJobs;
ctx.renderDownloadOverlaps({ transfer_direction:'download', cloud_key:'mail', source_path:'mail:/MYSPACE', options:{} });
assert.ok(nodes.downloadOverlapList.innerHTML.includes('Proxmox'));
assert.ok(nodes.downloadOverlapList.innerHTML.includes('задача выключена'));
assert.equal(ctx.transferDirectionLabel({ transfer_direction:'download' }), 'Облако → диск');
assert.equal(ctx.transferDirectionLabel({}), 'Диск → облако');
assert.equal(ctx.transferOperationLabel({ transfer_direction:'download', operation_label:'Копирование' }), 'Скачивание');

// Actual visibility logic must disable unsupported operations without erasing drafts.
const syncOption = { disabled:false };
const uploadField = { style:{}, dataset:{ transferDirection:'upload' } };
const downloadField = { style:{}, dataset:{ transferDirection:'download' } };
const retentionBlock = { style:{} };
ctx.document.querySelector = selector => selector.includes('option[value="sync"]') ? syncOption : null;
ctx.document.querySelectorAll = selector => {
  if (selector === "[data-job-kind='backup']" || selector === '[data-transfer-direction]') return [uploadField,downloadField];
  if (selector === "[data-retention-block='true']") return [retentionBlock];
  if (selector === '[id^="archive"]') return Object.values(nodes).filter(n=>n.id.startsWith('archive'));
  return [];
};
vm.runInContext(extract('updateJobKindVisibility'),ctx);
set('transferDirection','download');set('jobKind','copy');ctx.updateJobKindVisibility(false);
assert.equal(syncOption.disabled,true);
assert.equal(uploadField.style.display,'none');assert.equal(downloadField.style.display,'');
assert.equal(retentionBlock.style.display,'none');
assert.equal(nodes.jobWatcherEnabled.disabled,true);assert.equal(nodes.archiveEnabled.disabled,true);
assert.equal(ctx.directoryScanSupported(),false);assert.equal(ctx.retentionPolicySupported(),false);
assert.ok(nodes.transferDirectionHint.textContent.includes('не удаляются'));
set('transferDirection','upload');ctx.updateJobKindVisibility(false);
assert.equal(syncOption.disabled,false);assert.equal(nodes.archiveEnabled.disabled,false);
assert.equal(uploadField.style.display,'');assert.equal(downloadField.style.display,'none');
set('transferDirection','download');

(async () => {
  // Cloud source browse writes source_subpath only. Local destination and mount are separate targets.
  resetForm(downloaded);
  state.browseTarget = 'cloudSource';
  ctx.selectBrowseEntry({ path:'FILMS/2026', type:'directory' });
  assert.equal(nodes.jobSourceSubpath.value, 'FILMS/2026/');
  assert.equal(nodes.jobDestinationSubpath.value, '');
  assert.equal(nodes.jobDownloadDestination.value, '/mnt/pve/hdd/videos');
  state.browseTarget='destination';ctx.selectBrowseEntry({ path:'/mnt/pve/hdd/films', type:'directory' });
  assert.equal(nodes.jobDownloadDestination.value, '/mnt/pve/hdd/films/');
  state.browseTarget='destinationMount';ctx.selectBrowseEntry({ path:'/mnt/pve/hdd', type:'directory' });
  assert.equal(nodes.jobDestinationMount.value, '/mnt/pve/hdd');

  set('jobSourceSubpath','VIDEOS');state.browseTarget='exclude';fetches=[];
  await ctx.browse('VIDEOS');
  const request=new URL(fetches[0],'http://test');
  assert.equal(request.pathname,'/api/clouds/browse');
  assert.equal(request.searchParams.get('cloud_key'),'mail');
  assert.equal(request.searchParams.get('include_files'),'true');
  assert.equal(request.searchParams.get('path'),'VIDEOS');
  assert.equal(state.browseParent,null); // source subtree confines the browser
  assert.equal(state.browseEntries.find(e=>e.type==='file').path,'VIDEOS/one.mp4');
  const before=fetches.length;await ctx.browse('OTHER');assert.equal(fetches.length,before);
  state.excludePathEntries=[];
  ctx.selectBrowseEntry({ path:'VIDEOS/one.mp4',type:'file' });
  assert.equal(state.excludePathEntries[0].path,'mail:/MYSPACE/VIDEOS/one.mp4');
  assert.equal(state.excludePathEntries[0].kind,'file');
  state.browseTarget='cloudSource';fetches=[];
  await ctx.browse('VIDEOS');
  assert.equal(new URL(fetches[0],'http://test').searchParams.has('include_files'),false);
  assert.equal(state.browseParent,''); // empty root is a valid parent, not null
  ctx.browse = p=>browseCalls.push(p);ctx.browseUp();assert.equal(browseCalls.at(-1),'');

  assert.match(html,/data-transfer-direction="download"/);
  assert.match(code,/syncOption\.disabled = download/);
  assert.match(code,/job\.retention = \{ .*enabled: false/);
  assert.match(code,/job\.archive = \{ .*enabled: false/);
  assert.match(code,/job\.directory_scan = \{ .*enabled: false/);
  assert.match(code,/job\.transfer_direction !== "download"/);
  console.log('PASS: downloads syntax; upload defaults; safe DTO/clone roundtrip; per-direction drafts; local/remote browse; bounded anchored exclusions; mount validation; overlap warnings; download labels');
})().catch(error=>{console.error(error);process.exitCode=1;});
