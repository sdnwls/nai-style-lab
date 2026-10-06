// 대결 — the one place the user spends most time. Pick the one you like; the app decides what to compare.
import { get, post } from '../api.js';
import { h, morph, listen, icon, toast, keysButton, saveSetting, art, tierChip, record, tagList, copyText, openOriginal, lightbox, emptyState, fmt, capped, eloShown, removeCombos } from '../ui.js';

const KIND = {
  tie:     { icon: 'scale', tone: 'amber' },
  place:   { icon: 'target', tone: 'sky' },
  select:  { icon: 'dna', tone: '' },
  improve: { icon: 'wand', tone: 'rose' },
  league:  { icon: 'swords', tone: 'mint' },
  top:     { icon: 'crown', tone: 'mint' },
};
const MODES = [['auto', '자동'], ['league', '전체 리그'], ['top', '상위 30%']];

// stamp: which match is on screen. ready: false until both of its pictures have loaded (no vote on a picture not yet seen).
// reveal: the match just judged, shown for REVEAL_MS with how its ratings moved ({last: the pair after the vote, side});
// in blind mode it is also when the hidden values are shown.
let root, app, current = null, busy = false, stamp = '', ready = false, reveal = null;
const REVEAL_MS = 1000;
const MASK = '???';
// Evolution (select) and refine (improve) matches keep their combos until that step is done: no delete there.
const deletable = (kind) => kind !== 'select' && kind !== 'improve';

function header(match) {
  const kind = KIND[match.kind] || KIND.league;
  const ctx = match.context || { title: '대결', detail: '' };
  return h('div', { class: 'arena-head' },
    h('div', { class: 'row' }, h('div', { class: 'context', style: { flex: 1 } }, h('div', { class: `icon-badge ${kind.tone}` }, icon(kind.icon)),
      h('div', { style: { minWidth: 0 } }, h('div', { class: 'title' }, match.kind ? ctx.title : '대결'),
        h('div', { class: 'detail' }, match.kind ? ctx.detail : '마음에 드는 쪽을 고르기만 하면 됩니다.'))),
      keysButton([[['←', '→'], '선택'], [['4', '6'], '선택 (숫자 패드)'], [['Enter'], '비슷함'], [['⌫'], '되돌리기'], [[], '휠 확대(양쪽 함께) · 드래그 이동']])),
    // The controls get their own line under the title and its description.
    h('div', { class: 'arena-controls' }, h('button', { class: `btn icon-btn ${blind() ? 'primary' : ''}`, 'aria-label': '블라인드', 'aria-pressed': String(blind()), onclick: toggleBlind },
      icon(blind() ? 'eyeOff' : 'eye')),  // icon only: on = filled and the eye crossed out
    h('div', { class: 'segmented', role: 'tablist', 'aria-label': '대결 모드' },
      MODES.map(([mode, label]) => h('button', { class: match.mode === mode ? 'on' : '', role: 'tab', 'aria-selected': String(match.mode === mode),
        onclick: () => setMode(mode) }, label))),
      h('div', { class: 'spacer' }),
      h('button', { class: 'btn', disabled: !match.undo, onclick: undo }, icon('undo'), '되돌리기')));
}

// The Elo line: in the reveal after a vote, the new Elo and how far it moved ("10,380 +140").
// Still being evaluated: a newcomer finding its place, or a child of the running evolution. Its Elo is only where the
// search has got to (it jumps as the search narrows), so it is shown as 평가중, not as a rating.
const evaluating = (c) => !c.rated || c.child;
// Placed by this vote: it has a real rating now.
const settled = (combo, after) => evaluating(combo) && after && !evaluating(after);

function eloText(combo, after) {
  if (settled(combo, after)) return eloShown(after.elo);
  if (evaluating(combo)) return '평가중';
  if (!after) return eloShown(combo.elo);
  if (after.elo === combo.elo) return [eloShown(combo.elo), h('span', { class: 'delta same' }, ' +0')];  // e.g. 비슷함
  const delta = after.elo - combo.elo;
  return [eloShown(after.elo),
    combo.rated ? h('span', { class: `delta ${delta > 0 ? 'up' : 'down'}` }, ` ${delta > 0 ? '+' : ''}${fmt(delta)}`) : null];
}

function fighter(combo, side, rival, kind) {
  const key = side === 'a' ? '←' : '→';
  // Blind: every value is ??? until the vote; badges that only one side would carry (평가중) are left out altogether.
  const masked = blind() && !reveal;
  const after = reveal?.last[side];
  // Picked / the other one, or both marked alike after 비슷함 ('even').
  const outcome = !reveal?.side ? '' : reveal.side === 'even' ? 'even' : reveal.side === side ? 'picked' : 'dimmed';
  const frame = art(combo, { thumb: false, corners: [
    // 평가중 is shown under the picture (tier and Elo), as for a newcomer being placed: no second mark on it.
    ['tl', masked || kind === 'select' || !combo.role ? null : h('span', { class: 'badge glass' }, combo.role)],
  ] });
  // A click opens it big, as in the preview; not the click that ends a pan (syncZoom marks the frame on screen).
  listen(frame, 'click', (event) => { if (!event.currentTarget.__panned && combo.image) lightbox(combo.image); });
  // Refinement compares weights of the same artists: highlight where the two differ instead of showing Elo.
  const refine = kind === 'improve';
  const other = new Map(rival.pairs.map((p) => [p.tag, p.w]));
  const diffs = refine ? combo.pairs.filter((p) => other.has(p.tag) && Math.abs(other.get(p.tag) - p.w) > 1e-9).length : 0;
  const tags = masked ? h('div', { class: 'tags' }, h('span', { class: 'tag mask' }, MASK))
    : tagList(combo.pairs, { diff: refine ? other : null });
  const card = h('section', { class: `fighter fade-in ${outcome}`, 'data-side': side, 'aria-label': `${side === 'a' ? '왼쪽' : '오른쪽'} 그림체` },
    frame,
    h('div', { class: 'fighter-info' },
      masked ? h('span', { class: 'tier mask lg' }, MASK)
        : refine ? h('span', { class: `badge ${combo.role === '챔피언' ? 'accent' : 'rose'}`, style: { height: '28px', fontSize: '13px' } },
          combo.role === '챔피언' ? icon('crown') : icon('sliders'), combo.role)
        : evaluating(combo) && !settled(combo, after) ? h('span', { class: 'tier tier-new lg' }, '평가중') : tierChip(after || combo, 'lg'),
      masked
        ? h('div', {}, h('div', { class: 'elo num' }, MASK), h('div', { class: 'meta' }, MASK))
        : refine
          ? h('div', {}, h('div', { class: 'elo' }, diffs ? `다른 가중치 ${diffs}곳` : '가중치 같음'),
            h('div', { class: 'meta' }, '작가는 같고 가중치만 다릅니다 · 강조된 칩이 차이'))
          : h('div', {}, h('div', { class: 'elo num' }, eloText(combo, after)),
            // A combo being evaluated has no record worth showing yet (a newcomer's comes when it is placed).
            h('div', { class: 'meta' }, `${evaluating(combo) && !settled(combo, after) ? '새 조합' : record(after || combo)} · ${capped(combo.generation)}세대`)),
      // Named like the preview's buttons (ui.js comboPreview), side by side so the rating line stays one button tall.
      h('div', { class: 'fighter-actions' },
        h('button', { class: 'btn ghost sm labeled', onclick: () => openOriginal(combo) }, icon('external'), '원본'),
        deletable(kind) ? h('button', { class: 'btn ghost sm labeled danger-text', onclick: () => remove([combo.id]) }, icon('trash'), '삭제') : null)),
    // 복사 copies what this line shows: at its right end, the tags scroll in the room left of it.
    h('div', { class: 'fighter-tags' }, tags,
      h('button', { class: 'btn ghost sm labeled', onclick: () => copyText(combo.style) }, icon('copy'), '복사')),
    h('button', { class: 'btn primary pick', disabled: !ready, onclick: () => vote(side) }, '이쪽이 더 좋습니다', h('kbd', {}, key)));
  // One line that scrolls sideways; the mouse wheel scrolls it too.
  listen(tags, 'wheel', (event) => {
    const el = event.currentTarget;
    if (el.scrollWidth <= el.clientWidth || !event.deltaY) return;
    event.preventDefault();
    el.scrollLeft += event.deltaY;
  });
  return card;
}

function render(match) {
  current = match;
  const next = match.kind ? `${match.kind}:${match.a.id}:${match.b.id}` : 'idle';
  if (next !== stamp) {
    stamp = next;
    ready = false;
  }
  if (!match.kind) {
    const s = app.status?.stage;
    const working = app.status?.job?.running;
    return morph(root, header(match), h('div', { key: 'idle', style: { flex: 1, display: 'grid', placeItems: 'center' } },
      emptyState({ iconName: working ? 'sparkle' : 'check', title: working ? '그림을 만드는 중입니다' : '지금은 할 대결이 없습니다',
        text: match.message,
        action: s && s.route !== 'arena' ? h('button', { class: 'btn primary', onclick: () => app.go(s.route) }, s.action, icon('arrowRight')) : null })));
  }
  // The duel is keyed by the match: a new match is a new duel (it fades in), the same match is updated in place.
  morph(root, header(match),
    h('div', { class: 'duel', key: stamp }, fighter(match.a, 'a', match.b, match.kind),
      h('div', { class: 'versus' }, h('div', { class: 'vs-orb' }, 'VS'),
        h('button', { class: `btn pick-similar ${reveal?.side === 'even' ? 'on' : ''}`, disabled: !ready, onclick: skip }, icon('equal'), '비슷함'),
        deletable(match.kind) ? h('button', { class: 'btn ghost sm danger-text', onclick: () => remove([match.a.id, match.b.id]) }, icon('trash'), '둘 다 삭제') : null),
      fighter(match.b, 'b', match.a, match.kind)));
  const duel = root.querySelector('.duel');
  if (duel.__started) return;
  duel.__started = true;  // a new duel: zoom both pictures together, open voting once both are on screen
  syncZoom([...duel.querySelectorAll('.fighter .art')]);
  const shown = stamp;
  Promise.all([...duel.querySelectorAll('.fighter .art img')].map((img) => img.decode().catch(() => {}))).then(() => {
    if (stamp !== shown) return;  // another match is showing by now (a failed image counts as loaded: its frame says so)
    ready = true;
    render(current);
  });
}

async function load() {
  try {
    render(await get('/api/match'));
  } catch (error) {
    toast(error.message, 'error');
  }
}

// side: 'a' / 'b' for a vote, 'even' for 비슷함 (both sides marked alike), none for undo.
async function act(path, body, side) {
  if (busy) return;
  busy = true;
  try {
    if (side === 'even') {
      root.querySelectorAll('.fighter').forEach((el) => el.classList.add('even'));
      root.querySelector('.pick-similar')?.classList.add('on');
      await new Promise((r) => setTimeout(r, 170));
    } else if (side) {
      root.querySelector(`.fighter[data-side="${side}"]`)?.classList.add('picked');
      root.querySelector(`.fighter[data-side="${side === 'a' ? 'b' : 'a'}"]`)?.classList.add('dimmed');
      await new Promise((r) => setTimeout(r, 170));
    }
    const match = await post(path, body);
    // After every vote, blind or not, the same pause: how the ratings moved (+0 included), and in blind mode what was hidden.
    if (current?.kind && match.last) {
      reveal = { last: match.last, side };
      render(current);
      await new Promise((r) => setTimeout(r, REVEAL_MS));
      reveal = null;
    }
    render(match);
    app.refresh();
  } catch (error) {
    toast(error.message, 'error');
    await load();
  } finally {
    busy = false;
  }
}

const vote = (side) => current?.kind && ready && act('/api/vote', { side }, side);
const skip = () => current?.kind && ready && act('/api/skip', undefined, 'even');
const undo = () => act('/api/undo');
// Deleting takes the pair off the screen: the next match comes (a refusal, e.g. while generating, is shown as a toast).
async function remove(ids) {
  if (busy) return;
  busy = true;
  try {
    if (await removeCombos(app, ids).catch(() => true)) await load();
  } finally {
    busy = false;
  }
}
const blind = () => Boolean(app.settings.arena_blind);
async function toggleBlind() {
  const settings = app.settings;
  settings.arena_blind = !settings.arena_blind;
  if (current) render(current);
  if (!(await saveSetting({ arena_blind: settings.arena_blind }))) {  // not saved: put the toggle back
    settings.arena_blind = !settings.arena_blind;
    if (current) render(current);
  }
}
async function setMode(mode) {
  await saveSetting({ arena_mode: mode });  // either way the reload shows the mode actually saved
  load();
}

// Wheel zoom around the cursor and drag to pan, on both pictures at once: same seed, same composition,
// so the same spot is side by side. Offsets are fractions of the frame size.
function syncZoom(frames) {
  const z = { scale: 1, x: 0, y: 0 };
  let drag = null;
  const apply = () => {
    z.x = Math.min(0, Math.max(1 - z.scale, z.x));
    z.y = Math.min(0, Math.max(1 - z.scale, z.y));
    for (const frame of frames) {
      const img = frame.querySelector('img');
      if (img) img.style.transform = `translate(${z.x * frame.clientWidth}px, ${z.y * frame.clientHeight}px) scale(${z.scale})`;
      frame.classList.toggle('zoomed', z.scale > 1);
    }
  };
  for (const frame of frames) {
    frame.addEventListener('wheel', (event) => {
      event.preventDefault();
      const rect = frame.getBoundingClientRect();
      const px = (event.clientX - rect.left) / rect.width, py = (event.clientY - rect.top) / rect.height;
      const next = Math.min(5, Math.max(1, z.scale * (event.deltaY < 0 ? 1.18 : 1 / 1.18)));
      z.x = px - ((px - z.x) * next) / z.scale;
      z.y = py - ((py - z.y) * next) / z.scale;
      z.scale = next;
      apply();
    }, { passive: false });
    frame.addEventListener('pointerdown', (event) => {
      frame.__panned = false;
      if (z.scale <= 1 || event.button !== 0) return;
      drag = { sx: event.clientX, sy: event.clientY, x: z.x, y: z.y, w: frame.clientWidth, h: frame.clientHeight };
      frame.setPointerCapture(event.pointerId);
    });
    frame.addEventListener('pointermove', (event) => {
      if (!drag) return;
      z.x = drag.x + (event.clientX - drag.sx) / drag.w;
      z.y = drag.y + (event.clientY - drag.sy) / drag.h;
      apply();
    });
    // Judged where the button is let go (a fast drag's moves can arrive after it): moved = a pan, not a click.
    frame.addEventListener('pointerup', (event) => {
      if (drag && Math.abs(event.clientX - drag.sx) + Math.abs(event.clientY - drag.sy) > 3) frame.__panned = true;
      drag = null;
    });
    frame.addEventListener('pointercancel', () => { drag = null; });
  }
}

export default {
  async mount(el, appRef) {
    app = appRef;
    app.setFill(true);
    root = h('div', { style: { display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 } });
    el.append(root);
    stamp = '';
    await load();
  },
  onStatus(status, previous) {
    // While idle, pick up new work (a finished image, a new batch) as soon as it appears.
    if (!current?.kind && !busy) {
      const changed = !previous || previous.counts.newcomers !== status.counts.newcomers
        || previous.job?.done !== status.job?.done || previous.job?.running !== status.job?.running
        || previous.improve !== status.improve || previous.tie !== status.tie;
      if (changed) load();
    }
  },
  onKey(event) {
    // Numpad 4/6 by code: with Num Lock on their key is '4'/'6' (off, it is already ArrowLeft/ArrowRight).
    const map = { ArrowLeft: () => vote('a'), ArrowRight: () => vote('b'), Numpad4: () => vote('a'), Numpad6: () => vote('b'),
      Enter: skip, Backspace: undo };
    const fn = map[event.key] || map[event.code];
    if (fn) {
      event.preventDefault();
      if (!event.repeat) fn();  // a held key must not keep voting on matches not yet looked at
    }
  },
  unmount() {
    current = null;
    reveal = null;
    stamp = '';
  },
};
