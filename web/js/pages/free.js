// 자유 생성 — try any prompt with the current settings and seed; results stay in a small gallery.
import { get, post } from '../api.js';
import { h, morph, icon, toast, art, lightbox, emptyState, openOriginal, pageHead } from '../ui.js';

let root, app, results = [], draft = '';

function render() {
  const working = app.status?.job?.running;
  morph(root,
    pageHead({ title: '자유 생성', desc: '원하는 태그를 넣어 한 장씩 그려 봅니다. 설정의 기본·캐릭터·네거티브 프롬프트와 고정 시드를 그대로 씁니다.' }),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'minmax(0, 380px) minmax(0, 1fr)', alignItems: 'start' } },
      h('section', { class: 'card', style: { position: 'sticky', top: 0 } },
        h('div', { class: 'field' }, h('label', {}, '프롬프트 (작가 태그 자리)'),
          h('textarea', { class: 'textarea', rows: 6, value: draft, oninput: (e) => { draft = e.currentTarget.value; },
            placeholder: '1.2::artist:rurudo ::, 0.9::artist:shigure ui ::\n기본 프롬프트의 {artist} 자리에 들어갑니다.' })),
        h('button', { class: 'btn primary lg', style: { width: '100%', marginTop: '14px' }, disabled: working, onclick: () => generate(draft) },
          icon('brush'), working ? '생성 중…' : '그리기')),
      h('section', {},
        results.length ? h('div', { class: 'gallery lg' }, results.map((r) =>
          h('div', { class: 'tile', key: r.image, onclick: () => lightbox(r.url) },
            art({ thumb: `/thumb/${r.image}`, image: r.url }),
            h('div', { class: 'caption', style: { flexDirection: 'column', alignItems: 'stretch', gap: '2px' } },
              h('span', { class: 'mono', style: { color: 'var(--text-2)', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' } }, r.prompt || '(빈 프롬프트)'),
              h('div', { class: 'row', style: { gap: '4px' } },
                h('span', { class: 'note' }, `${r.time} · 시드 ${r.seed}`), h('span', { class: 'spacer' }),
                h('button', { class: 'btn ghost icon-btn sm', 'aria-label': '원본 열기',
                  onclick: (e) => { e.stopPropagation(); openOriginal({ file: r.image }); } }, icon('external')))))))
          : h('div', { class: 'card' }, emptyState({ iconName: 'brush', title: '아직 그린 그림이 없습니다', text: '왼쪽에 태그를 넣고 그려 보세요. 그린 그림은 모두 여기에 모입니다.' })))));
}

async function load() {
  try {
    results = (await get('/api/free')).results;
    render();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function generate(text) {
  await app.act(post('/api/generate/free', { prompt: text }), '그리는 중입니다.');
}

export default {
  async mount(el, appRef) {
    app = appRef;
    root = h('div');
    el.append(root);
    await load();
  },
  onStatus(status, previous, force) {
    if (force || (previous && previous.job?.running !== status.job?.running)) load();
  },
};
