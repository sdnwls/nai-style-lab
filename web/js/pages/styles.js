// 그림체 — every combo, active and set aside. Browse, compare, fix Elo, revive or delete.
import { get, post } from '../api.js';
import { h, morph, icon, toast, pickSelect, selectionKeys, arrowTarget, pageHead, comboTile, comboPreview, previewPage, reviveCombos, removeCombos,
  confirmDialog, fmt, emptyState, galleryList, galleryToolbar } from '../ui.js';

let root, app, data = { active: [], excluded: [] }, barHost = null;
const view = { tiers: new Set(), sort: 'elo-desc', q: '', selected: new Set(), focus: null, anchor: null };

function all() {
  return [...data.active, ...data.excluded];
}

const visible = () => galleryList(data.active, data.excluded, view);

function select(id, event) {
  pickSelect(view, id, event, visible().map((c) => c.id));  // the checkbox comes as a Ctrl+click
  view.focus = view.selected.has(id) ? id : [...view.selected].at(-1) || null;
  render();
}

// After Esc / Ctrl+A: the preview keeps showing a combo only while it is still selected.
function selectionChanged() {
  if (!view.selected.has(view.focus)) view.focus = null;
  render();
}

const tile = (c) => comboTile(c, { cls: view.selected.has(c.id) ? 'selected' : '', onclick: (e) => select(c.id, e),
  check: { on: view.selected.has(c.id), toggle: () => select(c.id, { ctrlKey: true }) } });

function detail() {
  return comboPreview(all().find((x) => x.id === view.focus), app);
}

// Shown while any are selected; it floats over the page, so it lives in its own host on <body>.
function selectionBar() {
  const ids = [...view.selected];
  if (!ids.length) return null;
  const outs = all().filter((c) => view.selected.has(c.id) && c.excluded).map((c) => c.id);
  return h('div', { class: 'selection-bar' },
    h('span', { class: 'count' }, `${ids.length}개 선택`),
    outs.length ? h('button', { class: 'btn sm', onclick: () => revive(outs) }, icon('refresh'), `부활 ${outs.length}`) : null,
    h('button', { class: 'btn sm danger', onclick: () => remove(ids) }, icon('trash'), '삭제'),
    h('button', { class: 'btn sm ghost', onclick: () => { view.selected.clear(); view.focus = null; render(); } }, '선택 해제'));
}

function render() {
  const failed = data.excluded.filter((c) => c.excluded === '탈락').length;
  const dropped = data.excluded.length - failed;
  const judging = data.active.filter((c) => !c.rated || c.child).length;
  const list = visible();
  const toolbar = galleryToolbar(view, render);
  toolbar.style.marginBottom = '0';
  morph(root, previewPage(
    pageHead({ title: '그림체', desc: `전체 ${fmt(data.active.length)} · 평가중 ${fmt(judging)} · 탈락 ${fmt(failed)} · 제외 ${fmt(dropped)}`,
      keys: [[['←', '→', '↑', '↓'], '선택 이동'], [['Delete'], '삭제'], [['Esc'], '선택 해제'], [['Ctrl', 'A'], '모두 선택'],
        [[], 'Shift/Ctrl 클릭 · 체크로 여러 개 선택']],
      // The line under the description: sort and filters on the left, the purge buttons on the right.
      below: h('div', { class: 'row', style: { marginTop: '14px', gap: '8px', flexWrap: 'wrap' } }, toolbar, h('span', { class: 'spacer' }),
        h('button', { class: 'btn danger', disabled: !failed, 'aria-label': '탈락 전부 삭제', onclick: () => purge('탈락', failed) }, icon('trash'), '탈락'),
        h('button', { class: 'btn danger', disabled: !dropped, 'aria-label': '제외 전부 삭제', onclick: () => purge('제외', dropped) }, icon('trash'), '제외')) }),
    null,
  list.length ? h('div', { class: 'gallery cols-8' }, list.map(tile))
    : emptyState({ iconName: 'grid', title: '보여 줄 그림체가 없습니다', text: '필터를 바꾸거나 작가 · 조합 만들기에서 새 조합을 만들어 보세요.' }),
  detail()));
  morph(barHost, selectionBar());
}

async function load() {
  try {
    data = await get('/api/combos');
    const known = new Set(all().map((c) => c.id));
    view.selected = new Set([...view.selected].filter((id) => known.has(id)));
    if (view.focus && !known.has(view.focus)) view.focus = null;
    render();
  } catch (error) {
    toast(error.message, 'error');
  }
}

const revive = (ids) => reviveCombos(app, ids);

async function remove(ids) {
  if (!(await removeCombos(app, ids))) return;
  view.selected.clear();
  view.focus = null;
  render();
}

async function purge(reason, count) {
  const ok = await confirmDialog({ title: `${reason} 전부 삭제`, text: `${reason} 조합 ${count}개를 기록과 이미지에서 영구 삭제합니다.\n삭제하면 부활시킬 수 없습니다.`, ok: '전부 삭제', danger: true });
  if (!ok) return;
  await app.act(post('/api/combos/purge', { reason }), `${reason} ${count}개를 삭제했습니다.`);
}

export default {
  async mount(el, appRef) {
    app = appRef;
    app.setFill(true);
    root = h('div', { style: { display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 } });
    barHost = h('div', { style: { display: 'contents' } });
    el.append(root);
    document.body.append(barHost);
    if (app.params.focus) {
      view.selected = new Set([app.params.focus]);
      view.focus = app.params.focus;
    }
    await load();
  },
  onStatus(status, previous, force) {
    if (force || (previous && (previous.counts.active !== status.counts.active || previous.counts.excluded !== status.counts.excluded
        || previous.counts.votes !== status.counts.votes || previous.settings_rev !== status.settings_rev
        || previous.job?.running !== status.job?.running))) load();
  },
  onKey(event) {
    const id = arrowTarget(event, view.focus);
    if (id) {
      view.selected = new Set([id]);
      view.anchor = id;
      view.focus = id;
      return render();
    }
    selectionKeys(event, view, { order: () => visible().map((c) => c.id), remove, changed: selectionChanged });
  },
  unmount() {
    barHost?.remove();
    barHost = null;
  },
};
