// 작가 · 조합 만들기 — register artists, then roll random combos that will find their place in the arena.
import { get, post } from '../api.js';
import { h, morph, limits, icon, toast, fmt, confirmDialog, promptDialog, shortTag, pickSelect, selectionKeys, pageHead, card } from '../ui.js';

let root, app, artists = [];
// addText / countDraft: what is typed in the artist box and "만들 개수" (the count until a start saves it).
const view = { q: '', sort: 'score', dir: -1, selected: new Set(), anchor: null, addText: '', countDraft: null };

function sorted() {
  const q = view.q.trim().toLowerCase();
  const list = artists.filter((a) => !q || a.tag.toLowerCase().includes(q));
  const key = view.sort;
  // Name order matches Windows Explorer (numbers by value); it also breaks ties in the other columns.
  const byName = (a, b) => a.tag.localeCompare(b.tag, 'ko', { numeric: true });
  return list.sort((a, b) => (key === 'tag' ? byName(a, b) * view.dir : (a[key] - b[key]) * view.dir || byName(a, b)));
}

function artistTable() {
  const list = sorted();
  const head = (key, label, num) => h('th', { class: num ? 'num' : '', onclick: () => {
    view.dir = view.sort === key ? -view.dir : key === 'tag' ? 1 : -1;
    view.sort = key;
    render();
  } }, label, view.sort === key ? h('span', { class: 'dir' }, view.dir > 0 ? '▲' : '▼') : null);
  const pick = (a, event) => {
    pickSelect(view, a.tag, event, list.map((x) => x.tag));
    render();
  };
  return h('div', { class: 'table-wrap', style: { maxHeight: '100%' } },
    h('table', { class: 'data artists' },
      h('thead', {}, h('tr', {}, head('order', '#', true), head('tag', '작가'), head('score', '점수', true), head('count', '사용', true), head('winrate', '승률', true))),
      h('tbody', {}, list.map((a) => h('tr', { key: a.tag, class: view.selected.has(a.tag) ? 'selected' : '', onclick: (e) => pick(a, e) },
        h('td', { class: 'num' }, fmt(a.order)), h('td', {}, shortTag(a.tag)), h('td', { class: 'num' },
          a.combos ? `${a.score > 0 ? '+' : ''}${fmt(a.score)}` : '–'), h('td', { class: 'num' }, fmt(a.count)),
        h('td', { class: 'num' }, a.arena_matches ? `${Math.round(a.winrate * 100)}%` : '–'))))));
}

function tableBar() {
  return h('div', { class: 'row', style: { marginTop: '10px' } },
    h('span', { class: 'note' }, view.selected.size ? `${view.selected.size}명 선택 · Delete로 삭제` : 'Shift/Ctrl로 여러 명 선택 · 한 명을 고르면 이름을 바꿀 수 있습니다'),
    h('span', { class: 'spacer' }),
    // Renaming is for one artist: its button shows next to delete while exactly one is selected.
    view.selected.size === 1 ? h('button', { class: 'btn sm', onclick: () => rename([...view.selected][0]) }, icon('pen'), '이름 바꾸기') : null,
    view.selected.size ? h('button', { class: 'btn danger sm', onclick: removeSelected }, icon('trash'), `${view.selected.size}명 삭제`) : null);
}

function render() {
  const settings = app.settings;
  const input = h('textarea', { class: 'textarea', rows: 4, value: view.addText, oninput: (e) => { view.addText = e.currentTarget.value; },
    placeholder: '예) artist:shigure ui, 1.2::artist:rurudo ::\n프롬프트를 통째로 붙여 넣어도 artist: 태그만 등록합니다.\nartist: 없이 이름만 쓰면 쉼표·줄바꿈으로 나눈 이름을 모두 등록합니다.' });
  const search = h('input', { class: 'input', type: 'search', placeholder: '작가 검색', value: view.q, oninput: (e) => { view.q = e.currentTarget.value; render(); } });
  const count = h('input', { class: 'input', type: 'number', ...limits('gen_count'), value: view.countDraft ?? settings.gen_count, style: { width: '96px' },
    oninput: (e) => { view.countDraft = e.currentTarget.value; } });
  const working = app.status?.job?.running;
  morph(root,
    pageHead({ title: '작가 · 조합 만들기', keys: [[['Delete'], '삭제'], [['Esc'], '선택 해제'], [['Ctrl', 'A'], '모두 선택'], [[], 'Shift/Ctrl 클릭으로 여러 명 선택']],
      desc: '좋아하는 작가들을 섞어 무작위 조합을 만들면, 대결에서 순위가 정해집니다.',
      // Setup step: the default list counts as chosen once edited, or confirmed here as is.
      below: app.status && !app.status.stage.checklist.artists && artists.length >= settings.gen_min
        ? h('div', { class: 'row', style: { marginTop: '14px' } }, h('button', { class: 'btn primary', onclick: confirmArtists }, icon('check'), '이 목록 그대로 사용'),
          h('span', { class: 'note' }, '작가를 더하거나 빼면 확인한 것으로 저장됩니다.')) : null }),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'minmax(0, 1.25fr) minmax(0, 1fr)', gridTemplateRows: 'minmax(0, 1fr)', alignItems: 'start', flex: 1, minHeight: 0 } },
      card({ icon: 'users', title: `작가 ${fmt(artists.length)}명`, desc: '점수는 이 작가가 들어간 조합들의 Elo에서 떼어 낸 작가의 몫입니다 (평균 0, 가중치 1 기준). 들어간 조합이 적으면 0에 가깝게 나옵니다.',
        style: { alignSelf: 'stretch', display: 'flex', flexDirection: 'column', minHeight: 0 } },
        input,
        h('div', { class: 'row', style: { margin: '10px 0 18px' } }, h('span', { class: 'spacer' }),
          h('button', { class: 'btn primary', onclick: add }, icon('plus'), '작가 등록')),
        h('label', { class: 'search', style: { display: 'block', marginBottom: '10px' } }, icon('search'), search),
        h('div', { style: { flex: 1, minHeight: 0 } }, artistTable()),  // the table takes whatever height the window has left
        tableBar()),
      card({ icon: 'sparkle', tone: 'sky', title: '무작위 조합 만들기', desc: '등록한 작가 중 몇 명을 골라 가중치를 붙인 조합을 만들고, 바로 그림을 생성합니다.' },
        h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '8px', marginBottom: '16px' } },
          rule('조합당 작가 수', pair(ruleInput('gen_min'), ruleInput('gen_max'))),
          rule('가중치 범위', pair(ruleInput('global_min_w', 0.1), ruleInput('global_max_w', 0.1))),
          rule('시드', h('div', { class: 'input-group' }, ruleInput('seed', null),
            h('button', { class: 'btn sm', onclick: () => saveRule('seed', String(Math.floor(Math.random() * app.meta.max_seed))) }, icon('refresh'), '무작위')),
          { gridColumn: 'span 2' })),
        h('div', { class: 'row' }, h('span', { class: 'field-label' }, '만들 개수'), count, h('span', { class: 'spacer' }),
          h('button', { class: 'btn primary lg', disabled: working, onclick: () => generate(Number(view.countDraft ?? settings.gen_count)) }, icon('zap'), working ? '생성 중…' : '만들기')),
        h('p', { class: 'note' },
          '작가 수와 가중치 범위는 진화와 다듬기에도 그대로 적용됩니다.'))));
}

function rule(label, value, style) {
  return h('div', { style: { background: 'var(--surface-2)', borderRadius: '10px', padding: '10px 12px', ...style } },
    h('div', { class: 'note' }, label), h('div', { style: { marginTop: '6px' } }, value));
}

const pair = (a, b) => h('div', { class: 'range-pair' }, a, h('span', {}, '~'), b);

// The combo rules and the seed are edited here, where combos are made: saved when the field is left (or Enter);
// a refused value is shown in the error toast and the field goes back to what is saved. step null: the seed
// (text: it may be empty, which draws one on the first image).
function ruleInput(key, step = 1) {
  const seed = step == null;
  return h('input', { class: 'input num', ...(seed ? { inputmode: 'numeric', placeholder: '비우면 무작위' } : { type: 'number', step, ...limits(key) }),
    value: app.settings[key],
    onchange: (e) => saveRule(key, seed ? e.currentTarget.value.trim() : Number(e.currentTarget.value)),
    onkeydown: (e) => { if (e.key === 'Enter') e.currentTarget.blur(); } });
}

async function saveRule(key, value) {
  await app.act(post('/api/settings', { changes: { [key]: value } }), '저장했습니다.').catch(() => {});
  render();
}

async function load() {
  try {
    // The engine keeps artists in the order they were added: that place is the 등록 순 (# column).
    artists = (await get('/api/artists')).map((a, i) => ({ ...a, order: i + 1 }));
    render();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function add() {
  if (!view.addText.trim()) return toast('등록할 작가를 입력해 주세요.', 'warn');
  const result = await app.act(post('/api/artists/add', { text: view.addText }));
  toast(result.added ? `작가 ${result.added}명을 등록했습니다.` : '새로 등록된 작가가 없습니다. 이미 있거나 비어 있습니다.', result.added ? 'ok' : 'info');
  view.addText = '';
  render();
}

async function confirmArtists() {
  await app.act(post('/api/settings', { changes: { artists_checked: true } }), '작가 목록을 확인했습니다.');
}

async function rename(tag) {
  // Edit just the name: the server puts "artist:" back in front.
  const name = tag.replace(/^artist:/, '');
  const value = await promptDialog({ title: '작가 이름 수정', text: '이 작가가 들어간 조합의 태그도 함께 바뀝니다.', value: name });
  if (value == null || value.trim().replace(/^artist:/, '') === name) return;
  await app.act(post('/api/artists/rename', { old: tag, new: value }), '이름을 바꿨습니다.');
  view.selected.clear();
  render();
}

async function removeSelected() {
  const tags = [...view.selected];
  const ok = await confirmDialog({ title: `작가 ${tags.length}명 삭제`, text: '명단에서만 빠지고, 이미 만든 조합은 그대로 남습니다.', ok: '삭제', danger: true });
  if (!ok) return;
  await app.act(post('/api/artists/delete', { tags }), `작가 ${tags.length}명을 삭제했습니다.`);
  view.selected.clear();
  render();
}

async function generate(count) {
  await app.act(post('/api/generate/random', { count }), `조합 ${count}개를 만들기 시작했습니다. 완성되는 대로 대결에서 자리를 찾을 수 있습니다.`);
  view.countDraft = null;  // saved as the default now
}

export default {
  async mount(el, appRef) {
    app = appRef;
    app.setFill(true);
    root = h('div', { style: { display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 } });
    el.append(root);
    await load();
  },
  onStatus(status, previous, force) {
    if (force || (previous && (previous.job?.running !== status.job?.running || previous.settings_rev !== status.settings_rev))) load();
  },
  unmount() {
    view.countDraft = null;
  },
  // Same keys as the 그림체 list (Ctrl+A picks everyone the search shows).
  onKey(event) {
    selectionKeys(event, view, { order: () => sorted().map((a) => a.tag), remove: removeSelected, changed: render });
  },
};
