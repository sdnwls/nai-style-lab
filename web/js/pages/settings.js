// 설정 — everything saves as you type. The combo rules are edited on 작가 · 조합 만들기.
import { get, post, keepDraft } from '../api.js';
import { h, morph, limits, icon, toast, fmt, confirmDialog, promptDialog, pageHead, card } from '../ui.js';

// The settings themselves are app.conf (one copy for every page). showKey: the API key shown in clear.
// chars: the character prompts being edited (empty boxes included, which the server drops on save).
let root, app, showKey = false, chars = null;
const pending = {};
let timer = null;

// Typing only remembers the text; it is saved once, when the field is left (its change event),
// on a tab switch or leaving the page, or as the window closes (Python keeps the draft for that).
function stage(key, value) {
  pending[key] = value;
  if (key in s()) s()[key] = value;  // a re-render (tab switch) before the save returns keeps what was typed
  keepDraft({ ...pending });
}

function save(key, value) {
  stage(key, value);
  clearTimeout(timer);
  timer = setTimeout(flush, 450);
}

async function flush() {
  const changes = { ...pending };
  for (const key of Object.keys(pending)) delete pending[key];
  if (!Object.keys(changes).length) return;
  try {
    app.conf.settings = await post('/api/settings', { changes });
    keepDraft({ ...pending });  // saved: only what was typed since is left to save on close
    markSaved();
    app.refresh();
  } catch (error) {
    keepDraft({ ...pending });  // refused: like any refused change, it is not saved on close either
    toast(error.message, 'error');
    // The server kept nothing of a refused change: show what is really saved again.
    app.conf = await get('/api/settings').catch(() => app.conf);
    chars = null;
    render();
  }
}

// Same bottom-left popup as every other notice; typing is debounced, so one per pause.
function markSaved() {
  toast('저장했습니다.', 'ok', 1600);
}

const s = () => app.conf.settings;
const isMedium = () => s().effort === 'medium' && s().model === app.meta.effort_model;

function field(label, control, help) {
  return h('div', { class: 'field' }, h('label', {}, label), control, help ? h('div', { class: 'help' }, help) : null);
}

function number(key, { step = 1, width } = {}) {
  // Typed only (no spin buttons, ↑/↓ or wheel; see app.js): remember what is typed, save once when the
  // field is left (or Enter), like the text fields. Its limits come from the server's one table.
  return h('input', { class: 'input num', type: 'number', step, ...limits(key), value: s()[key], style: width ? { width } : null,
    oninput: (e) => { const v = e.currentTarget.value; if (v !== '') stage(key, step < 1 ? parseFloat(v) : parseInt(v, 10)); },
    onblur: flush, onkeydown: (e) => { if (e.key === 'Enter') e.currentTarget.blur(); } });
}

function select(key, options, labels = {}) {
  // Rendered again at once: the model and effort decide which fields there are.
  return h('select', { class: 'select', onchange: (e) => { save(key, e.currentTarget.value); render(); } },
    options.map((o) => h('option', { value: o, selected: s()[key] === o }, labels[o] ?? o)));
}

// The shared card (ui.js), called positionally: every settings card has an icon, tone, title and line.
const section = (iconName, tone, title, desc, ...body) => card({ icon: iconName, tone, title, desc }, ...body);

function connection() {
  const conf = app.conf;
  const key = h('input', { class: 'input', id: 'api-key', type: showKey ? 'text' : 'password', value: conf.api_key, placeholder: 'pst-로 시작하는 NovelAI 영구 API 토큰', autocomplete: 'off',
    onchange: (e) => saveKey(e.currentTarget.value), onkeydown: (e) => { if (e.key === 'Enter') e.currentTarget.blur(); } });
  const reveal = h('button', { class: 'btn icon-btn', 'aria-label': '키 보기', onclick: () => { showKey = !showKey; render(); } },
    icon(showKey ? 'eyeOff' : 'eye'));
  const sub = conf.subscription;
  const subView = !sub ? (conf.api_key ? h('span', { class: 'faint' }, '아직 조회하지 않았습니다.') : null)
    : sub.error ? h('span', { style: { color: 'var(--red)' } }, sub.error)
    : h('div', { class: 'row wrap', style: { gap: '8px' } },
      h('span', { class: 'badge accent' }, sub.tier), sub.active ? null : h('span', { class: 'badge red' }, '구독 비활성'),
      sub.anlas != null ? h('span', { class: 'badge' }, `Anlas ${fmt(sub.anlas)}`) : null,
      sub.remaining_percent != null ? h('span', { class: 'badge' }, `잔여 ${sub.remaining_percent}%`) : null,
      sub.estimate ? h('span', { class: 'badge mint' }, `예상 ${fmt(sub.estimate.left)} / ${fmt(sub.estimate.total)}장`) : null,
      h('span', { class: 'note' }, `${sub.checked} 조회`));
  return section('key', '', 'NovelAI 연결', 'API 키는 이 컴퓨터의 data 폴더에 저장되고, 데이터 내보내기 zip에도 들어갑니다. 폴더나 zip을 남에게 줄 때는 조심하세요.',
    field('API 키', h('div', { class: 'input-group' }, key, reveal,
      h('button', { class: 'btn primary', onclick: () => saveKey(root.querySelector('#api-key').value) }, '조회'))),
    h('div', { class: 'row', style: { marginTop: '10px' } }, subView),
    sub?.estimate ? h('p', { class: 'note' }, '예상 장수는 예측값으로 계산한 값입니다 (V5 무제한 범위만).') : null,
    h('div', { class: 'row wrap', style: { marginTop: '14px', paddingTop: '12px', borderTop: '1px solid var(--line)', gap: '10px' } },
      h('span', { class: 'note', style: { flex: 1, minWidth: '200px' } },
        'API 키 발급 방법: ≡ → Account Settings → Get Persistent API Token'),
      h('button', { class: 'btn',
        onclick: () => app.act(post('/api/open-novelai'), '기본 브라우저에서 NovelAI를 열었습니다.') }, icon('key'), '키 발급받기')));
}

function generation() {
  const meta = app.meta;
  const hasEffort = s().model === meta.effort_model;
  const medium = isMedium();  // like the site, no fields for what Medium fixes
  const seed = h('input', { class: 'input num', value: s().seed, placeholder: '비우면 무작위', inputmode: 'numeric',
    onchange: (e) => save('seed', e.currentTarget.value.trim()) });
  return section('image', 'sky', '이미지 생성', '모든 그림(무작위 조합, 진화 조합, 다듬기, 자유 생성)이 이 설정과 하나의 시드로 그려집니다.',
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3, 1fr)' } },
      field('모델', select('model', meta.models)),
      hasEffort ? field('Effort', select('effort', Object.keys(meta.efforts), meta.efforts)) : null,
      field('크기', select('size', meta.sizes)),
      medium ? null : field('샘플러', select('sampler', Object.keys(meta.samplers), meta.samplers)),
      medium ? null : field('Steps', number('steps'), '28 이하가 무제한 범위입니다.'),
      field('CFG (Scale)', number('cfg', { step: 0.1 })),
      medium ? null : field('CFG Rescale', number('cfg_rescale', { step: 0.05 })),
      // With Effort shown (High and Medium alike) the delay closes its row wide and the seed gets a row of its own;
      // one cell would be too narrow to read a seed in.
      h('div', { class: 'field', style: { gridColumn: hasEffort ? 'span 2' : 'auto' } }, h('label', {}, '생성 간 대기 (초)'),
        h('div', { class: 'range-pair' }, number('delay', { step: 0.5 }), h('span', {}, '-'), number('delay_max', { step: 0.5 }))),
      h('div', { class: 'field', style: { gridColumn: hasEffort ? '1 / -1' : 'span 2' } }, h('label', {}, '시드'),
        h('div', { class: 'input-group' }, seed,
          h('button', { class: 'btn', onclick: () => { save('seed', String(Math.floor(Math.random() * meta.max_seed))); render(); } }, icon('refresh'), '무작위'))),
      // Under the delay and seed row, across both: one line instead of wrapping in the delay's column.
      h('div', { class: 'field', style: { gridColumn: '1 / -1', marginTop: '-10px' } },
        medium ? h('div', { class: 'help' }, 'Medium은 사이트처럼 Steps 14, Euler Ancestral로 고정되고 CFG Rescale과 내 네거티브 프롬프트(캐릭터 포함)를 쓰지 않습니다.') : null,
        h('div', { class: 'help' }, 'NovelAI는 짧은 시간에 많은 이미지를 자동으로 생성하는 계정을 제한할 수 있습니다 (403).'))));
}

function prompts() {
  const base = h('textarea', { class: 'textarea grow', rows: 3, value: s().base_prompt,
    oninput: (e) => stage('base_prompt', e.currentTarget.value), onchange: flush });
  const medium = isMedium();  // Medium sends its own negative: the user's stay readable, and come back on High
  const negative = h('textarea', { class: 'textarea grow', rows: 4, value: s().negative, disabled: medium,
    oninput: (e) => stage('negative', e.currentTarget.value), onchange: flush });
  chars ??= (s().character_prompt || []).map((p, i) => ({ p, n: s().character_negative?.[i] ?? '' }));
  // Both lists go together, so the server keeps each negative with its character.
  const stageChars = () => { stage('character_prompt', chars.map((c) => c.p)); stage('character_negative', chars.map((c) => c.n)); };
  const box = (c, key, placeholder) => h('textarea', { class: 'textarea grow', rows: 2, value: c[key], placeholder, disabled: key === 'n' && medium,
    oninput: (e) => { c[key] = e.currentTarget.value; stageChars(); }, onchange: flush });
  const list = h('div', { class: 'stack' }, chars.map((c, i) =>
    h('div', { class: 'row', style: { alignItems: 'flex-start' } }, h('span', { class: 'badge', style: { marginTop: '8px' } }, `캐릭터 ${i + 1}`),
      h('div', { class: 'stack', style: { flex: 1, gap: '6px' } },
        box(c, 'p', `캐릭터 ${i + 1} 프롬프트`), box(c, 'n', `캐릭터 ${i + 1} 네거티브`)),
      h('button', { class: 'btn ghost icon-btn', 'aria-label': '삭제', onclick: () => { chars.splice(i, 1); stageChars(); flush(); render(); } }, icon('x')))),
    h('button', { class: 'btn sm', style: { alignSelf: 'flex-start' }, onclick: () => { chars.push({ p: '', n: '' }); render(); } }, icon('plus'), '캐릭터 추가'));
  return section('brush', 'rose', '프롬프트', '작가 조합은 기본 프롬프트의 {artist} 자리에 들어갑니다. {artist}가 없으면 맨 앞에 붙습니다.',
    h('div', { class: 'stack' },
      field('기본 프롬프트', base, h('span', {}, h('code', {}, '{artist}'), ' 위치에 작가 태그가 들어갑니다.')),
      field('캐릭터 프롬프트', list),
      field('네거티브 프롬프트', negative, medium ? 'Effort가 Medium이면 사이트의 고정 네거티브를 쓰므로 이 칸과 캐릭터 네거티브는 적용되지 않습니다. High로 바꾸면 다시 적용됩니다.' : null)),
    app.status?.stage.checklist.prompt ? null : h('div', { id: 'prompt-confirm', class: 'row', style: { marginTop: '14px' } },
      h('span', { class: 'note', style: { flex: 1 } }, '기본값 그대로 사용하려면 오른쪽 버튼을 누르세요.'),
      h('button', { class: 'btn primary', onclick: () => app.act(post('/api/settings', { changes: { prompt_checked: true } }), '프롬프트를 확인했습니다.') },
        icon('check'), '이대로 사용')));
}

// 다시 그리기: the listed combos (all, or the top 30%) drawn again with the prompts above (another character),
// replacing their pictures. The export sits next to it: that zip is the only way back to the old pictures.
function redraw() {
  return section('refresh', 'sky', '다시 그리기', '현재의 프롬프트와 시드로 순위는 유지한 채 그림을 재생성합니다.',
    h('div', { class: 'row wrap' },
      h('button', { class: 'btn', onclick: exportData }, icon('download'), '데이터 내보내기'),
      h('span', { class: 'spacer' }),
      h('button', { class: 'btn primary', onclick: () => startRedraw(true) }, icon('refresh'), '상위 30%'),
      h('button', { class: 'btn primary', onclick: () => startRedraw(false) }, icon('refresh'), '전체')),
    h('p', { class: 'note' }, '원래 그림은 지워지니, 되돌리려면 먼저 내보내 두세요.'));
}

async function startRedraw(top) {
  await flush();  // what was just typed is what gets drawn
  // One image per combo redrawn: say how many up front, against what the subscription has left when known.
  const n = (top ? app.status?.counts?.top : app.status?.counts?.active) ?? 0;
  const left = app.conf.subscription?.estimate?.left;
  const what = top ? '상위 30% 조합' : '목록의 모든 조합';
  const ok = await confirmDialog({ title: top ? '상위 30% 다시 그리기' : '전체 다시 그리기', ok: '다시 그리기', danger: true, text:
    `⚠ 그림을 ${fmt(n)}장 새로 생성합니다${left != null ? ` (남은 예상 장수 ${fmt(left)}장)` : ''}. 조합이 많으면 시간과 생성량이 크게 듭니다.\n\n` +
    `${what}을 바꾼 프롬프트, 시드로 다시 그리고, 원래 그림을 지웁니다. 순위는 그대로입니다.\n순위가 높은 조합부터 그리며, 중간에 멈추면 새 그림과 원래 그림이 섞입니다.\n` +
    (top ? '나머지 조합은 원래 그림 그대로라, 대결에서 캐릭터가 다른 그림끼리 붙을 수 있습니다.\n' : '') +
    '되돌리려면 먼저 데이터 내보내기를 해 두세요.' });
  if (ok) await app.act(post('/api/generate/redraw', { top }), '다시 그리기를 시작했습니다.');
}

// 업데이트: this version, the newest release (checked at launch), and installing it. Installing downloads the
// release, closes the app and starts the new version; data/ is never touched (a new data format is upgraded at its
// first launch, the old file kept).
function update() {
  const u = app.status?.update;
  if (!u) return null;
  const installing = ['downloading', 'preparing', 'restarting'].includes(u.state);
  const line = installing ? (u.state === 'downloading' ? `새 버전을 받는 중입니다${u.progress != null ? ` (${u.progress}%)` : ''}.`
      : '설치를 준비하고 있습니다. 곧 앱이 닫혔다가 새 버전으로 다시 열립니다.')
    : u.state === 'checking' ? '확인하는 중입니다.'
    : u.state === 'available' ? `새 버전 v${u.latest}이(가) 있습니다.`
    : u.state === 'latest' ? '최신 버전입니다.' : '';
  return named(section('download', 'sky', '업데이트', `지금 버전 v${u.current}`,
    line ? h('p', { class: 'note', style: { color: 'var(--text)', marginTop: 0 } }, line) : null,
    u.error ? h('p', { class: 'note', style: { color: 'var(--red)', marginTop: line ? '6px' : 0 } }, u.error) : null,
    u.state === 'available' && u.notes ? h('pre', { class: 'release-notes' }, u.notes) : null,
    h('div', { class: 'row wrap', style: { marginTop: '12px' } },
      u.state === 'available' ? h('button', { class: 'btn primary', disabled: Boolean(u.blocked) || app.status?.job?.running,
        onclick: installUpdate }, icon('download'), '업데이트 설치') : null,
      h('button', { class: 'btn', disabled: installing || u.state === 'checking', onclick: () => app.act(post('/api/update/check')) },
        icon('refresh'), '업데이트 확인'),
      u.page ? h('button', { class: 'btn ghost', onclick: () => app.act(post('/api/update/page')) }, '릴리스 페이지') : null),
    u.state === 'available' && u.blocked ? h('p', { class: 'note' }, u.blocked) : null,
    h('p', { class: 'note' }, '설치하면 앱이 닫혔다가 새 버전으로 다시 열립니다. 데이터(data 폴더)는 그대로 둡니다.')), 'update');
}

async function installUpdate() {
  const u = app.status.update;
  const ok = await confirmDialog({ title: `v${u.latest}로 업데이트`, ok: '설치', text:
    '새 버전을 받아 설치합니다. 다 받으면 앱이 닫혔다가 새 버전으로 다시 열립니다.\n데이터는 그대로이며, 데이터 형식이 바뀌는 업데이트라면 첫 실행 때 이전 파일을 백업한 뒤 바꿉니다.' });
  if (ok) await app.act(post('/api/update/install'), '새 버전을 받기 시작했습니다.');
}

function data() {
  return section('folder', 'mint', '데이터', app.meta.data_dir,
    h('div', { class: 'row wrap' },
      h('button', { class: 'btn', onclick: () => app.act(post('/api/open-folder')) }, icon('folder'), '데이터 폴더 열기'),
      h('button', { class: 'btn', onclick: exportData }, icon('download'), '데이터 내보내기'),
      h('button', { class: 'btn', onclick: importData }, icon('upload'), '데이터 불러오기')),
    h('p', { class: 'note' },
      '데이터 내보내기를 누르면 모든 설정을 zip 하나로 묶어 추출합니다. 데이터 불러오기를 통해 데이터를 불러오면 지금의 모든 데이터는 삭제됩니다.'));
}

// Python shows Windows' Save As over this window and writes the zip there (path null: cancelled).
async function exportData() {
  const { path } = await app.act(post('/api/data/export'));
  if (path) toast(`데이터를 내보냈습니다: ${path}`, 'ok');
}

// Windows' Open picks the zip (null: cancelled); Python reads that file itself, so only its name comes back here.
async function importData() {
  const file = await app.act(post('/api/data/pick'));
  if (!file) return;
  const ok = await confirmDialog({ title: '데이터 불러오기', ok: '불러오기', danger: true,
    text: `${file.name}

지금의 조합·이미지·순위·진화·작가·설정이 이 파일의 내용으로 바뀝니다. 백업을 남기지 않으니, 필요하면 먼저 내보내기 해 두세요. 파일에 API 키가 들어 있으면 그 키로 바뀝니다.` });
  if (!ok) return;
  const result = await app.act(post('/api/data/import'));
  toast(`데이터를 불러왔습니다. 조합 ${fmt(result.combos)}개, 이미지 ${fmt(result.images)}장.`, 'ok');
  chars = null;
  render();
}

const RESETS = [
  ['ratings', '순위 기록', '모든 Elo·전적·투표 기록을 지웁니다. 조합과 이미지는 남고, 대결에서 자리를 다시 찾습니다.'],
  ['evolution', '진화 기록', '세대 번호와 진화 로그만 1세대로 되돌립니다. 조합과 순위는 그대로입니다.'],
  ['settings', '설정', '생성 설정·조합 규칙·프롬프트를 기본값으로 되돌립니다. API 키와 시드는 유지합니다.'],
  ['artists', '작가 목록', '등록한 작가를 지우고 기본 작가 목록으로 되돌립니다.'],
  ['combos', '데이터만 초기화', '조합·이미지·순위·진화·다듬기 기록을 모두 지웁니다. 작가 목록·설정·API 키는 유지합니다.', true],
  ['all', '모두 초기화', '데이터를 모두 지우고 처음 설치한 상태로 되돌립니다.', true],
];

async function reset([scope, title, text, heavy]) {
  const warn = `${text}\n\n⚠ 이 작업은 절대 되돌릴 수 없습니다.`;
  const ok = heavy
    ? (await promptDialog({ title: `${title}`, text: `${warn}\n계속하려면 "초기화"를 입력하세요.`, ok: '초기화', placeholder: '초기화' }))?.trim() === '초기화'
    : await confirmDialog({ title: `${title} 초기화`, text: warn, ok: '초기화', danger: true });
  if (!ok) return;
  await app.act(post('/api/reset', { scope }), '초기화에 성공하였습니다.');
  chars = null;
  render();
}

function resets() {
  return section('trash', 'rose', '초기화', '초기화하면 바로 지워지며, 절대 되돌릴 수 없습니다.',
    h('div', { class: 'stack', style: { gap: '10px' } }, RESETS.map((item) =>
      h('div', { class: 'row', style: { justifyContent: 'space-between', gap: '12px' } },
        h('div', {}, h('div', { style: { fontWeight: 600 } }, item[1]), h('div', { class: 'note' }, item[2])),
        h('button', { class: `btn sm ${item[3] ? 'danger' : ''}`, style: { flex: 'none' }, onclick: () => reset(item) }, '초기화')))));
}

// Tabs keep every part of the settings on one screen without scrolling.
const TABS = [['basic', '연결 · 생성'], ['prompts', '프롬프트'], ['data', '데이터']];
const TAB_OF = { connection: 'basic', prompts: 'prompts', update: 'data' };
let tab = 'basic';

function render() {
  const columns = (left, right) => h('div', { class: 'grid settings-columns', style: { alignItems: 'start' } },
    h('div', { class: 'stack', style: { gap: '16px' } }, left), h('div', { class: 'stack', style: { gap: '16px' } }, right));
  const body = tab === 'prompts' ? h('div', { class: 'stack', style: { gap: '16px' } }, named(prompts(), 'prompts'), redraw())
    : tab === 'data' ? columns([update(), data()], resets())
    : columns([named(connection(), 'connection')], generation());
  morph(root,
    pageHead({ title: '설정', desc: '바꾸면 바로 저장됩니다.',
      below: h('div', { class: 'segmented', role: 'tablist', 'aria-label': '설정 항목', style: { marginTop: '14px' } },
        TABS.map(([id, label]) => h('button', { class: tab === id ? 'on' : '', role: 'tab', 'aria-selected': String(tab === id),
          onclick: () => { flush(); tab = id; render(); } }, label))) }),
    body);
}

// The setup guide and "지금 할 일" link here with ?focus=<section id>. That card stays marked for as long
// as this page is open, whether or not the step is already done, so it is always clear where you were sent.
let highlight = null;

const named = (el, id) => {
  el.id = id;
  if (id === highlight) el.style.outline = '2px solid var(--accent)';
  return el;
};

function focusSection() {
  const id = app.params.focus;
  const target = id && document.getElementById(id);
  highlight = target ? id : null;
  if (!target) return;
  target.style.outline = '2px solid var(--accent)';
  target.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// Saving the key always checks the subscription too, so a wrong key shows up right away.
// The button click follows the input's change event: both share one in-flight save.
let keySaving = null;
function saveKey(value) {
  keySaving ??= (async () => {
    try {
      value = value.trim();
      save('api_key', value);
      await flush();
      if (value) await app.act(post('/api/subscription'));
      else await app.refresh();
      render();
    } finally {
      keySaving = null;
    }
  })();
  return keySaving;
}

export default {
  async mount(el, appRef) {
    app = appRef;
    root = h('div');
    el.append(root);
    tab = TAB_OF[app.params.focus] ?? tab;
    render();
    focusSection();
  },
  unmount() {
    flush();
    highlight = null;
    chars = null;
    showKey = false;
  },
  // morph() keeps what is being typed, so the page can follow every change of the settings.
  onStatus(status, previous, force) {
    if (force || previous?.settings_rev !== status.settings_rev
        || previous?.stage.checklist.prompt !== status.stage.checklist.prompt
        || JSON.stringify(previous?.update) !== JSON.stringify(status.update)) render();
  },
};
