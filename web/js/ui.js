// Small UI toolkit: DOM builder, icons, toasts, dialogs, lightbox, tooltips and shared pieces.
import { post } from './api.js';

// ---------------------------------------------------------------- DOM
// h() builds an element. Its handlers go through listen() and the properties it sets are remembered
// (__props), so morph() can bring an element already on screen up to date with a freshly built one.
export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'key') el.dataset.key = value;
    else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
    else if (key.startsWith('on') && typeof value === 'function') listen(el, key.slice(2).toLowerCase(), value);
    else if (key === 'html') el.innerHTML = value;
    else if (key in el && key !== 'list') {
      el[key] = value;
      (el.__props ??= {})[key] = value;
    } else el.setAttribute(key, value === true ? '' : value);
  }
  append(el, children);
  return el;
}

// One real listener per event type, calling whatever handler the element holds now (morph swaps it).
// Handlers that need their element use event.currentTarget: the element on screen, not the one built.
export function listen(el, type, fn) {
  el.__on ??= {};
  if (!(type in el.__on)) el.addEventListener(type, (event) => el.__on[type]?.(event));
  el.__on[type] = fn;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

// Start over: for a new page or an overlay. Updates of what is already on screen use morph().
export function clear(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

// The one way a page updates what is on screen: build the whole view again, then morph() it in.
// Only what changed is touched, so scroll positions, focus, typed text, hover and loaded pictures stay,
// and only elements that are really new play their entry animation (.fade-in). List items carry a ``key``
// so they are matched by identity, not position; an element whose data-sig is unchanged is left alone.
export function morph(parent, ...children) {
  const next = document.createDocumentFragment();
  append(next, children);
  morphChildren(parent, next);
  return parent;
}

const keyOf = (node) => (node.nodeType === 1 ? node.dataset.key : undefined);

function morphChildren(target, source) {
  const keyed = new Map();
  for (const node of target.childNodes) if (keyOf(node)) keyed.set(keyOf(node), node);
  let cursor = target.firstChild;
  for (const fresh of [...source.childNodes]) {
    const key = keyOf(fresh);
    let old = null;
    if (key) {
      old = keyed.get(key) || null;
      keyed.delete(key);
    } else if (cursor && !keyOf(cursor)) {
      old = cursor;
    }
    if (old && old.nodeName === fresh.nodeName) {
      morphNode(old, fresh);
      if (old === cursor) cursor = cursor.nextSibling;
      else target.insertBefore(old, cursor);
    } else {
      target.insertBefore(fresh, cursor);
    }
  }
  while (cursor) {  // whatever is left was not in the new view
    const next = cursor.nextSibling;
    cursor.remove();
    cursor = next;
  }
  for (const node of keyed.values()) node.remove();
}

function morphNode(old, fresh) {
  if (old.nodeType !== 1) {
    if (old.nodeValue !== fresh.nodeValue) old.nodeValue = fresh.nodeValue;
    return;
  }
  if (old.dataset.sig && old.dataset.sig === fresh.dataset.sig) return;
  for (const { name } of [...old.attributes]) if (!fresh.hasAttribute(name)) old.removeAttribute(name);
  for (const { name, value } of fresh.attributes) if (old.getAttribute(name) !== value) old.setAttribute(name, value);
  for (const [key, value] of Object.entries(fresh.__props || {})) {
    if (key === 'value' && old === document.activeElement) continue;  // never under the user's cursor
    if (old[key] !== value) old[key] = value;
  }
  old.__props = fresh.__props;
  for (const type of new Set([...Object.keys(old.__on || {}), ...Object.keys(fresh.__on || {})])) {
    if (fresh.__on?.[type]) listen(old, type, fresh.__on[type]);
    else old.__on[type] = null;
  }
  morphChildren(old, fresh);
}

// From the server's /api/meta, the one source for both: input limits (every field for a setting uses the
// same numbers) and the tier letters, best first, as core.py rates them.
let ranges = {};
let TIERS = [];
export const setMeta = (meta) => { ranges = meta?.ranges || {}; TIERS = meta?.tiers || []; };
export function limits(key) {
  const range = ranges[key];
  return range ? { min: range[0], max: range[1] } : {};
}

export const fmt = (n) => (n == null ? '–' : Number(n).toLocaleString('ko-KR'));
// A combo's numbers as shown stop at a cap (Elo 99,999, wins / losses / generation 99), so they fit the narrowest
// preview; the numbers themselves have no limit.
export const capped = (n, max = 99) => fmt(n == null ? n : Math.min(n, max));
export const eloShown = (elo) => capped(elo, 99_999);
// Display only: drop the "artist:" prefix and prompt escapes like "\(" (Korean fonts draw "\" as "₩").
// Only the "artist:" prefix goes: the rest is shown exactly as stored, so an odd tag is easy to spot.
export const shortTag = (tag) => tag.replace(/^artist:/, '');

// ---------------------------------------------------------------- icons (24px line icons, 1.8 stroke)
const PATHS = {
  home: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V20a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V9.5"/>',
  swords: '<path d="M14.5 17.5 3 6V3h3l11.5 11.5"/><path d="m13 19 6-6"/><path d="m16 16 4 4"/><path d="m19 21 2-2"/><path d="M14.5 6.5 18 3h3v3l-3.5 3.5"/><path d="m5 14 4 4"/><path d="m7 17-3 3"/><path d="m3 19 2 2"/>',
  dna: '<path d="M4 2c0 6 16 6 16 12s-16 6-16 8"/><path d="M20 2c0 6-16 6-16 12s16 6 16 8"/><path d="M6 6h12"/><path d="M6 18h12"/><path d="M8 10h8"/><path d="M8 14h8"/>',
  wand: '<path d="m15 4-11 11 5 5L20 9z"/><path d="m13 6 5 5"/><path d="M19 2v2"/><path d="M18 3h2"/><path d="M5 2v4"/><path d="M3 4h4"/><path d="M20 15v4"/><path d="M18 17h4"/>',
  grid: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  brush: '<path d="M9.06 11.9 17.07 3.9a2.85 2.85 0 1 1 4.03 4.03l-8.01 8.01"/><path d="M7.07 14.94c-1.66 0-3 1.35-3 3.02 0 1.33-2.5 1.52-2 2.02 1.08 1.1 2.49 2.02 4 2.02 2.2 0 4-1.8 4-4.04a3.01 3.01 0 0 0-3-3.02z"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  sparkle: '<path d="M12 3l1.9 5.6L19.5 10.5 13.9 12.4 12 18l-1.9-5.6L4.5 10.5l5.6-1.9z"/><path d="M19 3v4"/><path d="M17 5h4"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
  equal: '<path d="M5 9h14"/><path d="M5 15h14"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  external: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  trash: '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.64-6.36L21 8"/><path d="M21 3v5h-5"/>',
  crown: '<path d="m2 7 4.5 4L12 4l5.5 7L22 7l-2 12H4z"/>',
  star: '<path d="m12 2 3.1 6.3 6.9 1-5 4.9 1.2 6.8-6.2-3.3-6.2 3.3 1.2-6.8-5-4.9 6.9-1z"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  eyeOff: '<path d="M9.9 4.24A9.1 9.1 0 0 1 12 4c6.4 0 10 8 10 8a18.5 18.5 0 0 1-2.16 3.19"/><path d="M6.61 6.61A13.5 13.5 0 0 0 2 12s3.6 8 10 8a9.7 9.7 0 0 0 5.39-1.61"/><path d="m2 2 20 20"/><path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/>',
  folder: '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2z"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
  zap: '<path d="M13 2 3 14h9l-1 8 10-12h-9z"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
  alert: '<path d="m10.29 3.86-8.47 14.14A2 2 0 0 0 3.53 21h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  arrowRight: '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
  target: '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
  scale: '<path d="m16 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1z"/><path d="m2 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1z"/><path d="M7 21h10"/><path d="M12 3v18"/><path d="M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2"/>',
  sliders: '<path d="M4 21v-7"/><path d="M4 10V3"/><path d="M12 21v-9"/><path d="M12 8V3"/><path d="M20 21v-5"/><path d="M20 12V3"/><path d="M1 14h6"/><path d="M9 8h6"/><path d="M17 16h6"/>',
  key: '<circle cx="7.5" cy="15.5" r="5.5"/><path d="m21 2-9.6 9.6"/><path d="m15.5 7.5 3 3L22 7l-3-3"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.09-3.09a2 2 0 0 0-2.82 0L6 21"/>',
  flag: '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><path d="M4 22v-7"/>',
  keyboard: '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="M6 8h.01"/><path d="M10 8h.01"/><path d="M14 8h.01"/><path d="M18 8h.01"/><path d="M8 12h.01"/><path d="M12 12h.01"/><path d="M16 12h.01"/><path d="M7 16h10"/>',
  play: '<path d="m6 3 14 9-14 9z"/>',
  pen: '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/><path d="m15 5 4 4"/>',
};

export function icon(name, extra = '') {
  const span = document.createElement('span');
  span.style.display = 'contents';
  span.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ${extra}>${PATHS[name] || ''}</svg>`;
  return span.firstElementChild;
}

// ---------------------------------------------------------------- toasts
const TOAST_ICON = { ok: 'check', info: 'info', warn: 'alert', error: 'alert' };
export function toast(text, level = 'info', ms) {
  const root = document.getElementById('toasts');
  const close = () => {
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 250);
  };
  const el = h('div', { class: `toast ${level}` },
    h('div', { class: 'ico' }, icon(TOAST_ICON[level] || 'info')),
    h('div', { class: 'body' }, text),
    h('button', { class: 'x', 'aria-label': '닫기', onclick: close }, icon('x', 'width="14" height="14"')));
  root.append(el);
  while (root.children.length > 5) root.firstElementChild.remove();
  setTimeout(close, ms ?? (level === 'error' ? 9000 : level === 'warn' ? 7000 : 4200));
}

export async function run(promise, okText) {
  try {
    const result = await promise;
    if (okText) toast(okText, 'ok');
    return result;
  } catch (error) {
    toast(error.message, 'error');
    error.shown = true;  // the global unhandled-rejection guard must not toast it twice
    throw error;
  }
}

// ---------------------------------------------------------------- dialogs
function modal(build) {
  return new Promise((resolve) => {
    const root = document.getElementById('overlay-root');
    const done = (value) => { backdrop.remove(); document.removeEventListener('keydown', onKey, true); resolve(value); };
    const onKey = (event) => {
      if (event.key === 'Escape') { event.stopPropagation(); done(null); }
    };
    const dialog = h('div', { class: 'dialog', role: 'dialog', 'aria-modal': 'true' });
    const backdrop = h('div', { class: 'backdrop', onmousedown: (e) => { if (e.target === backdrop) done(null); } }, dialog);
    build(dialog, done);
    document.addEventListener('keydown', onKey, true);
    root.append(backdrop);
    (dialog.querySelector('input, textarea') || dialog.querySelector('.btn.primary, .btn.danger'))?.focus();
  });
}

export function confirmDialog({ title, text, ok = '확인', danger = false }) {
  return modal((dialog, done) => {
    dialog.append(h('h3', {}, title), h('p', {}, text),
      h('div', { class: 'actions' },
        h('button', { class: 'btn ghost', onclick: () => done(false) }, '취소'),
        h('button', { class: `btn ${danger ? 'danger' : 'primary'}`, onclick: () => done(true) }, ok)));
  }).then(Boolean);
}

export function promptDialog({ title, text, value = '', ok = '저장', type = 'text', placeholder = '' }) {
  return modal((dialog, done) => {
    const input = h('input', { class: 'input', type, value: String(value), placeholder,
      onkeydown: (e) => { if (e.key === 'Enter') done(input.value); } });
    dialog.append(h('h3', {}, title), text ? h('p', {}, text) : null, input,
      h('div', { class: 'actions' },
        h('button', { class: 'btn ghost', onclick: () => done(null) }, '취소'),
        h('button', { class: 'btn primary', onclick: () => done(input.value) }, ok)));
    setTimeout(() => input.select(), 0);
  });
}

// ---------------------------------------------------------------- prompt viewer
// What NovelAI writes into its PNGs: tEXt chunks, "Comment" holding the request as JSON and "Source" the model.
// Null for anything else (another kind of file, or a picture whose metadata a site stripped).
export function novelaiMeta(bytes) {
  const PNG = [137, 80, 78, 71, 13, 10, 26, 10];
  if (bytes.length < 8 || PNG.some((b, i) => bytes[i] !== b)) return null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const utf8 = new TextDecoder();
  const texts = {};
  for (let at = 8; at + 8 <= bytes.length;) {
    const size = view.getUint32(at), type = utf8.decode(bytes.subarray(at + 4, at + 8));
    if (type === 'IEND') break;
    if (type === 'tEXt') {
      const body = bytes.subarray(at + 8, at + 8 + size), zero = body.indexOf(0);
      texts[utf8.decode(body.subarray(0, zero))] = utf8.decode(body.subarray(zero + 1));
    }
    at += 12 + size;
  }
  return metaFromTexts(texts);
}

// NovelAI also hides the same texts in the alpha channel's lowest bits, column by column: "stealth_pngcomp", a 32-bit
// length in bits, then the texts as gzipped JSON. A program that re-saves the PNG drops its tEXt chunks but keeps these
// (novelai.net reads them too). Null when they are not there.
export async function stealthMeta(file) {
  const MAGIC = 'stealth_pngcomp';
  const bitmap = await createImageBitmap(file, { premultiplyAlpha: 'none', colorSpaceConversion: 'none' });
  const { width, height } = bitmap;
  const context = new OffscreenCanvas(width, height).getContext('2d');
  context.drawImage(bitmap, 0, 0);
  const pixels = context.getImageData(0, 0, width, height).data;  // alpha comes back exact
  let bit = 0;
  const read = (count) => {
    if (bit + count * 8 > width * height) return null;
    const out = new Uint8Array(count);
    for (let i = 0; i < count * 8; i++, bit++) {
      const x = Math.floor(bit / height), y = bit % height;
      out[i >> 3] = (out[i >> 3] << 1) | (pixels[(y * width + x) * 4 + 3] & 1);
    }
    return out;
  };
  const magic = read(MAGIC.length);
  if (!magic || new TextDecoder().decode(magic) !== MAGIC) return null;
  const length = read(4);
  const body = length && read(new DataView(length.buffer).getUint32(0) >> 3);
  if (!body) return null;
  const json = await new Response(new Blob([body]).stream().pipeThrough(new DecompressionStream('gzip'))).text();
  return metaFromTexts(JSON.parse(json));
}

function metaFromTexts(texts) {
  try {
    const meta = JSON.parse(texts.Comment);
    return typeof meta === 'object' && meta ? { ...meta, source: texts.Source || '' } : null;
  } catch {
    return null;
  }
}

// A NovelAI picture dropped on the window: its prompts and settings, each prompt with a copy button, and its
// artists (of the prompt and character prompts; the undesired content names artists to avoid) to register at once.
export async function promptViewer(app, file) {
  const meta = novelaiMeta(new Uint8Array(await file.arrayBuffer())) || await stealthMeta(file).catch(() => null);
  if (!meta) return toast('NovelAI 프롬프트 정보가 없는 그림입니다. NovelAI에서 받은 PNG 원본을 놓아 주세요 (SNS에 올렸던 그림·캡처는 정보가 지워져 있습니다).', 'warn');
  const caption = meta.v4_prompt?.caption;
  const base = caption?.base_caption ?? meta.prompt ?? '';
  const characters = (caption?.char_captions || []).map((c) => c.char_caption).filter(Boolean);
  const negative = meta.v4_negative_prompt?.caption?.base_caption ?? meta.uc ?? '';
  const scan = await run(post('/api/artists/scan', { text: [base, ...characters].join('\n') }));
  const settings = [meta.source.replace(/\s+[0-9A-F]{8}$/, ''), meta.width && `${meta.width}×${meta.height}`,
    meta.steps && `${meta.steps} steps`, meta.scale != null && `CFG ${meta.scale}`, meta.cfg_rescale && `Rescale ${meta.cfg_rescale}`,
    meta.sampler, meta.seed != null && `시드 ${meta.seed}`, meta.skip_cfg_above_sigma && 'Variety+'].filter(Boolean).join(' · ');
  const url = URL.createObjectURL(file);
  const section = (title, text) => h('div', { class: 'viewer-section' },
    h('div', { class: 'row' }, h('h4', {}, title), h('span', { class: 'spacer' }),
      h('button', { class: 'btn ghost sm labeled', onclick: () => copyText(text, `${title}: 복사했습니다.`) }, icon('copy'), '복사')),
    h('div', { class: 'viewer-text' }, text));
  // Weight and name, as in the arena; the ones not registered yet are highlighted.
  const fresh = new Set(scan.new);
  const artistChips = tagList(scan.pairs, { open: true });
  [...artistChips.children].forEach((chip, i) => chip.classList.toggle('diff', fresh.has(scan.pairs[i].tag)));
  await modal((dialog, done) => {
    dialog.classList.add('viewer');
    const register = async () => {
      const result = await app.act(post('/api/artists/add', { text: scan.new.join(', ') }));
      toast(`작가 ${result.added}명을 등록했습니다.`, 'ok');
      done(null);
      if (app.route === 'library') app.go('library');  // its table shows the new names
    };
    dialog.append(h('h3', {}, '프롬프트 뷰어'), h('p', {}, file.name),
      h('div', { class: 'viewer-body' }, h('img', { src: url, alt: '' }),
        h('div', { class: 'viewer-info' },
          h('div', { class: 'viewer-section' }, h('h4', {}, `작가 ${scan.pairs.length}명 · 미등록 작가 ${scan.new.length}명`),
            scan.pairs.length ? artistChips : h('p', { class: 'muted' }, '프롬프트에 artist: 태그가 없습니다.')),
          section('프롬프트', base),
          characters.map((c, i) => section(`캐릭터 ${i + 1}`, c)),
          negative ? section('네거티브', negative) : null,
          settings ? h('p', { class: 'note' }, settings) : null)),
      h('div', { class: 'actions' },
        h('button', { class: 'btn ghost', onclick: () => done(null) }, '닫기'),
        h('button', { class: 'btn primary', disabled: !scan.new.length, onclick: register }, icon('plus'),
          scan.new.length ? `새 작가 ${scan.new.length}명 일괄 등록` : scan.pairs.length ? '모두 등록되어 있음' : '등록할 작가 없음')));
  });
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------- lightbox & originals
export function lightbox(src) {
  const root = document.getElementById('overlay-root');
  const close = () => { box.remove(); document.removeEventListener('keydown', onKey, true); };
  const onKey = (e) => { if (e.key === 'Escape' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); close(); } };
  // Dragged out of the app (to Explorer, a chat, an editor), the picture goes as its PNG file: the window starts
  // a real file drag (main.py) in place of the page's own.
  const img = h('img', { src, alt: '', ondragstart: (e) => {
    e.preventDefault();
    post('/api/drag', { file: src.split('/').pop() }).catch(() => {});
  } });
  const box = h('div', { class: 'lightbox', onclick: close }, img);
  document.addEventListener('keydown', onKey, true);
  root.append(box);
}

export function openOriginal(combo) {
  return run(post('/api/open', combo.id ? { id: combo.id } : { file: combo.file }));
}

// ---------------------------------------------------------------- multi-select (작가 표, 그림체 목록)
// One way to pick items everywhere: click = just this one (again = none), Ctrl = toggle, Shift = the range
// from the last click. ``state`` holds {selected: Set, anchor}; ``order`` is the ids in the order shown.
export function pickSelect(state, id, event, order) {
  if (event.shiftKey && state.anchor) {
    const [a, b] = [order.indexOf(state.anchor), order.indexOf(id)].sort((x, y) => x - y);
    if (a >= 0) for (const key of order.slice(a, b + 1)) state.selected.add(key);
  } else if (event.ctrlKey || event.metaKey) {
    state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
    state.anchor = id;
  } else if (state.selected.size === 1 && state.selected.has(id)) {
    state.selected.clear();
  } else {
    state.selected = new Set([id]);
    state.anchor = id;
  }
}

// The keys that go with it: Delete removes the selection, Esc clears it, Ctrl+A picks everything shown.
export function selectionKeys(event, state, { order, remove, changed }) {
  if (event.key === 'Delete' && state.selected.size) {
    event.preventDefault();
    remove([...state.selected]);
  } else if (event.key === 'Escape' && state.selected.size) {
    state.selected.clear();
    changed();
  } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a') {
    event.preventDefault();
    state.selected = new Set(order());
    changed();
  }
}

// A keyboard button whose tooltip lists the page's shortcuts: rows of [keys, what they do] (no keys: a plain line).
export function keysButton(rows) {
  const button = h('button', { class: 'btn icon-btn', 'aria-label': `단축키: ${rows.map(([keys, what]) => [...keys, what].join(' ')).join(', ')}` },
    icon('keyboard'));
  tooltip(button, () => h('div', {}, h('div', { class: 't' }, '단축키'),
    rows.map(([keys, what]) => h('div', { class: 'r' }, keys.map((key) => h('kbd', {}, key)), what))), { anchored: true });
  return button;
}

// ---------------------------------------------------------------- tooltip
let tip;
// Follows the mouse; anchored: always in one place, under the target, its right edge on the target's.
export function tooltip(target, render, { anchored = false } = {}) {
  listen(target, 'mousemove', (event) => {
    if (!tip) { tip = h('div', { class: 'tooltip' }); document.body.append(tip); }
    clear(tip, render());
    tip.style.display = 'block';  // before measuring it: a hidden one measures 0 wide
    // The element on screen: after a re-render, morph() moves this handler onto the old element, so `target` is gone.
    const box = event.currentTarget.getBoundingClientRect();
    const x = Math.max(8, Math.min(anchored ? box.right - tip.offsetWidth : event.clientX + 14, innerWidth - tip.offsetWidth - 8));
    const y = Math.min(anchored ? box.bottom + 8 : event.clientY + 14, innerHeight - tip.offsetHeight - 8);
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  });
  listen(target, 'mouseleave', () => { if (tip) tip.style.display = 'none'; });
}

// ---------------------------------------------------------------- shared pieces
const tierVar = (tier) => `var(--t-${tier.toLowerCase()})`;  // each tier's color in app.css

export function tierChip(combo, size = '') {
  if (combo.excluded) return h('span', { class: 'badge red' }, combo.excluded);
  // A child of the running evolution is still being evaluated, like a newcomer being placed: the same chip.
  if (combo.child) return h('span', { class: `tier tier-new ${size}` }, '평가중');
  if (!combo.tier) return h('span', { class: `tier tier-new ${size}` }, combo.rated ? '–' : '평가중');
  const grade = combo.grade || combo.tier;  // S+ / S / S-: the tier's band in thirds (color stays the tier's)
  return h('span', { class: `tier tier-${combo.tier} ${size} ${combo.provisional ? 'prov' : ''}` }, grade);
}

// The one way a record is written everywhere: "3승 2패", or "대결 전" before any match.
export function record(combo) {
  return combo.matches ? `${capped(combo.wins)}승 ${capped(combo.matches - combo.wins)}패` : '대결 전';
}

// The one "최종" mark: translucent, bottom-left corner of the picture (``art(..., { corners: [['bl', finalBadge(c)]] })``).
export function finalBadge(combo) {
  return combo.final ? h('span', { class: 'badge glass' }, icon('star'), '최종') : null;
}

export function copyText(text, label = '태그를 복사했습니다.') {
  return navigator.clipboard.writeText(text).then(() => toast(label, 'ok', 2200),
    () => toast('클립보드에 복사하지 못했습니다.', 'error'));
}

// ``diff``: a Map tag -> weight of the other side; chips whose weight differs are highlighted with ↑/↓.
export function tagList(pairs, { open = false, diff = null } = {}) {
  const sorted = diff ? [...pairs].sort((a, b) => Number(isDiff(b)) - Number(isDiff(a))) : pairs;
  function isDiff(p) { return diff && diff.has(p.tag) && Math.abs(diff.get(p.tag) - p.w) > 1e-9; }
  return h('div', { class: `tags ${open ? 'open' : ''}` },
    sorted.map((p) => {
      const changed = isDiff(p);
      const up = changed && p.w > diff.get(p.tag);
      return h('span', { class: `tag ${changed ? 'diff' : ''}` },
        h('span', { class: `w ${p.w >= 1.3 ? 'hi' : p.w <= 0.8 ? 'lo' : ''}` }, p.w.toFixed(1)), shortTag(p.tag),
        changed ? h('span', { class: `arrow ${up ? 'up' : 'down'}` }, up ? '↑' : '↓') : null);
    }));
}

export function weightBars(pairs, { min = 0.5, max = 2, compare } = {}) {
  const span = max - min || 1;
  const before = new Map((compare || []).map((p) => [p.tag, p.w]));
  return h('div', { class: 'weights' }, pairs.map((p) => {
    const old = before.get(p.tag);
    const diff = old == null ? null : Math.round((p.w - old) * 10) / 10;
    return h('div', { class: 'weight-row' },
      h('span', { class: 'name' }, shortTag(p.tag)),
      h('span', { class: 'track' },
        h('span', { class: 'fill', style: { width: `${Math.max(4, ((p.w - min) / span) * 100)}%` } }),
        old != null && diff !== 0 ? h('span', { class: 'ghost', style: { left: `${((old - min) / span) * 100}%` } }) : null),
      h('span', { class: `val ${diff > 0 ? 'up' : diff < 0 ? 'down' : ''}` }, p.w.toFixed(1)));
  }));
}

// A picture in its frame. It shimmers until loaded (.art:has(> img:not(.ready)) in app.css); the picture's
// data-sig keeps a loaded image untouched when morph() updates the frame's badges around it.
const pictureReady = (event) => event.currentTarget.classList.add('ready');
export function art(combo, { thumb = true, cls = '', corners = [] } = {}) {
  const src = thumb ? combo.thumb : combo.image;
  return h('div', { class: `art ${cls}` },
    src ? h('img', { src, alt: '', loading: 'lazy', decoding: 'async', draggable: false, 'data-sig': src,
      onload: pictureReady, onerror: pictureReady }) : h('div', { class: 'empty' }, '이미지 없음'),
    corners.map(([where, ...content]) => h('div', { class: `corner ${where}` }, content)));
}

// ---------------------------------------------------------------- page frames (one of each, for every page)
// The title and its description, alone on their lines; a page's controls go below them. keys: the page's
// shortcuts (keysButton rows), as a button at the right end of the title's line.
export function pageHead({ title, desc, eyebrow, below, keys }) {
  return h('div', { class: 'page-head' },
    eyebrow ? h('div', { class: 'eyebrow' }, eyebrow) : null,
    h('div', { class: 'title-line' }, h('h1', {}, title), keys ? keysButton(keys) : null), desc ? h('p', {}, desc) : null, below);
}

// A card with a head: icon badge, title and a line under it, then the card's content.
export function card({ icon: iconName, tone = '', title, desc, cls = '', style }, ...body) {
  return h('section', { class: `card ${cls}`, style },
    h('div', { class: 'card-head' }, h('div', { class: `icon-badge ${tone}` }, icon(iconName)),
      h('div', { class: 'card-head-text' }, h('h3', {}, title), desc ? h('p', { class: 'desc' }, desc) : null)),
    body);
}

// The tile of a combo, the same on every page: its picture with 평가중 or the tier (top left), #rank (top
// right) and 최종 (bottom left), and Elo · record under it. ``badge`` replaces the top-left badge (진화 shows
// 생존/탈락 there), ``extra`` adds to the caption, ``cls`` marks the tile (selected, out).
// check: { on, toggle } adds a checkbox at the bottom right, so the mouse alone can pick several (그림체).
export function comboTile(combo, { onclick, cls = '', badge, extra, check } = {}) {
  return h('div', { class: `tile ${combo.excluded ? 'out' : ''} ${cls}`, key: combo.id, onclick },  // set aside (탈락 / 제외): dimmed
    art(combo, { corners: [['tl', badge ?? tierChip(combo)],
      ['tr', combo.rank ? h('span', { class: 'badge glass num' }, `#${combo.rank}`) : null], ['bl', finalBadge(combo)],
      ['br', check ? h('button', { class: `tile-check ${check.on ? 'on' : ''}`, 'aria-label': '선택', 'aria-pressed': String(check.on),
        onclick: (e) => { e.stopPropagation(); check.toggle(); } }, icon('check', 'width="14" height="14"')) : null]] }),
    h('div', { class: 'caption' }, h('span', { class: 'elo num' }, eloShown(combo.elo)), h('span', { class: 'meta num' }, record(combo)), extra));
}

// A page with the preview column (그림체 · 진화 · 다듬기). The title and its line span the page and the preview
// starts right under them on every tab; the left column keeps its top (toolbar, steps) in place and scrolls the list.
export function previewPage(head, top, list, preview) {
  return [head, h('div', { style: { flex: 1, minHeight: 0 } }, h('div', { class: 'split fill' },
    h('div', { class: 'split-main' }, top, h('div', { class: 'split-list' }, list)), preview))];
}

// Arrow keys move the selection between the tiles of a preview page: ←/→ to the previous/next tile, ↑/↓ to the
// nearest tile in the row above/below (across galleries too). Returns the tile's id, or null if the key is not for it.
export function arrowTarget(event, currentId) {
  const dirs = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -1, ArrowDown: 1 };
  if (!(event.key in dirs) || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return null;
  const tiles = [...document.querySelectorAll('.split-list .tile[data-key]')];
  const i = tiles.findIndex((t) => t.dataset.key === currentId);
  if (i < 0) return null;
  event.preventDefault();
  const step = dirs[event.key];
  let next = tiles[i + step];
  if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
    const rect = (t) => t.getBoundingClientRect();
    const here = rect(tiles[i]);
    const center = (r) => r.left + r.width / 2;
    // The nearest row in that direction, then the tile in it closest to this one's column.
    const rows = tiles.map((t) => [t, rect(t)]).filter(([, r]) => (r.top - here.top) * step > 1);
    const rowTop = rows.reduce((best, [, r]) => (best == null || Math.abs(r.top - here.top) < Math.abs(best - here.top) ? r.top : best), null);
    next = rows.filter(([, r]) => Math.abs(r.top - rowTop) <= 1)
      .sort(([, a], [, b]) => Math.abs(center(a) - center(here)) - Math.abs(center(b) - center(here)))[0]?.[0];
  }
  if (!next) return null;
  next.scrollIntoView({ block: 'nearest' });
  return next.dataset.key;
}

// Sort and tier filters for the 그림체 gallery. view holds { tiers: Set, sort }.
const SORTS = [['elo-desc', 'Elo 높은 순'], ['elo-asc', 'Elo 낮은 순'], ['created-desc', '최근 생성 순'], ['created-asc', '오래된 생성 순']];

// What the gallery shows: the active combos and the archive (탈락 / 제외), sorted and filtered.
export function galleryList(active, excluded, view) {
  let list = active.concat(excluded);
  // Tier filters add up (S + A shows both); none picked = 전체.
  if (view.tiers.size) {
    list = list.filter((c) => !c.excluded && (view.tiers.has(c.tier) || (view.tiers.has('new') && (!c.rated || c.child))));
  }
  const [key, dir] = view.sort.split('-');
  const value = (c) => c[key];
  // By Elo the archive goes last (its Elo no longer counts); by creation time it falls where it was made.
  const archiveLast = (a, b) => (key === 'elo' ? Boolean(a.excluded) - Boolean(b.excluded) : 0);
  // Equal Elo: the server's ranking decides (#n), so the order matches the numbers on the tiles.
  const byRank = (a, b) => (a.rank ?? Infinity) - (b.rank ?? Infinity);
  return list.slice().sort((a, b) => archiveLast(a, b) || (dir === 'desc' ? value(b) - value(a) || byRank(a, b) : value(a) - value(b) || byRank(b, a)));
}

// One row, all on the left: sort, then the filters (several at once). onChange re-renders.
export function galleryToolbar(view, onChange) {
  const filters = [...TIERS.map((t) => [t, t]), ['new', '평가중']];
  const toggle = (key) => { view.tiers.has(key) ? view.tiers.delete(key) : view.tiers.add(key); onChange(); };
  const chip = (on, onclick, label, tier) => h('button', { class: `chip ${on ? 'on' : ''} ${tier ? `tier-fill tier-${tier}` : ''}`,
    style: { whiteSpace: 'nowrap', background: tier ? tierVar(tier) : null }, onclick }, label);
  return h('div', { class: 'toolbar', style: { marginBottom: '14px' } },  // wraps to a second row on a narrow window
    h('select', { class: 'select', style: { width: '120px', flex: 'none' }, 'aria-label': '정렬', onchange: (e) => { view.sort = e.currentTarget.value; onChange(); } },
      SORTS.map(([key, label]) => h('option', { value: key, selected: view.sort === key }, label))),
    h('div', { class: 'row', style: { gap: '6px' } },
      chip(!view.tiers.size, () => { view.tiers.clear(); onChange(); }, '전체'),
      filters.map(([key, label]) => chip(view.tiers.has(key), () => toggle(key), label, TIERS.includes(key) ? key : null))));
}

// The preview column beside a gallery: the combo large, its actions, record and weights.
export function comboPreview(c, app) {
  if (!c) {
    return h('aside', { class: 'detail', key: 'none' }, h('div', { class: 'card' },
      emptyState({ iconName: 'image', title: '그림체를 선택하세요' })));
  }
  const action = (name, label, onclick, disabled = false, cls = '') =>
    h('button', { class: `btn ghost sm ${cls}`, disabled, onclick }, icon(name), label);
  // Status badges ride on the image, so the tier and every action fit on one row below it.
  const frame = art(c, { thumb: false, corners: [['bl', finalBadge(c)]] });
  listen(frame, 'click', () => lightbox(c.image));
  frame.style.cursor = 'zoom-in';
  const running = Boolean(app.status?.job?.running);
  // Keyed by the combo: showing another one brings a new panel (it fades in); the same one is updated in place.
  return h('aside', { class: 'detail fade-in', key: c.id },
    frame,
    // One line, 다듬기 the main one. Deleting is in the selection bar and the Delete key. 다듬기 is off until the
    // combo is ranked and while images are being made.
    h('div', { class: 'preview-actions' },
      c.excluded
        ? h('button', { class: 'btn sm', onclick: () => reviveCombos(app, [c.id]) }, icon('refresh'), '부활')
        : h('button', { class: 'btn primary sm', disabled: !c.rated || running, onclick: () => startRefine(app, c) }, icon('wand'), '다듬기'),
      h('button', { class: 'btn sm', onclick: () => editElo(app, [c.id], c.elo) }, icon('pen'), 'Elo 수정'),
      h('button', { class: 'btn sm', onclick: () => openOriginal(c) }, icon('external'), '원본')),
    // Tier and record on one line (the Elo is changed with Elo 수정 above).
    h('div', { class: 'kv' }, h('div', { class: 'tier-cell' }, tierChip(c)),
      h('div', {}, h('div', { class: 'k' }, 'Elo'), h('div', { class: 'v num' }, eloShown(c.elo))),
      // No labels here: "18승 10패" and "3세대" say what they are; their units are small, like "Elo".
      h('div', {}, h('div', { class: 'v num' }, c.matches
        ? [capped(c.wins), h('span', { class: 'k' }, '승'), ' ', capped(c.matches - c.wins), h('span', { class: 'k' }, '패')] : '대결 전')),
      h('div', {}, h('div', { class: 'v num' }, capped(c.generation), h('span', { class: 'k' }, '세대')))),
    h('div', { class: 'card pad-sm' },
      h('div', { class: 'row', style: { marginBottom: '8px', height: '20px' } }, h('span', { class: 'field-label' }, `작가 ${c.pairs.length}명`),
        h('span', { class: 'spacer' }), action('copy', '복사', () => copyText(c.style), false, 'header-action')),
      weightBars(c.pairs, { min: app.settings.global_min_w, max: app.settings.global_max_w })));
}

export async function reviveCombos(app, ids) {
  await app.act(post('/api/combos/revive', { ids }), `${ids.length}개를 되살렸습니다. 평가 기록이 없던 조합은 대결에서 자리를 찾습니다.`);
}

export async function editElo(app, ids, current) {
  const value = await promptDialog({ title: 'Elo 수정', text: `선택한 ${ids.length}개 조합의 Elo를 이 값으로 바꿉니다.`, value: current ?? 10000, type: 'number' });
  if (value == null || value === '') return;
  await app.act(post('/api/combos/elo', { ids, elo: Number(value) }), 'Elo를 수정했습니다.');
}

// True once confirmed and sent.
export async function removeCombos(app, ids) {
  const ok = await confirmDialog({ title: `${ids.length}개 영구 삭제`, text: '기록과 이미지 파일이 함께 삭제되고 되살릴 수 없습니다.', ok: '삭제', danger: true });
  if (!ok) return false;
  await app.act(post('/api/combos/delete', { ids }), `${ids.length}개를 삭제했습니다.`);
  return true;
}

async function startRefine(app, c) {
  const ok = await confirmDialog({ title: '이 그림체를 다듬으시겠습니까?',
    text: '작가는 그대로 두고 가중치만 바꾼 변형을 만들어, 원본과 1:1로 비교합니다.\n변형은 원본과 같은 시드로 그립니다.', ok: '다듬기 시작' });
  if (!ok) return;
  await app.act(post('/api/improve/start', { id: c.id }), '다듬기를 시작했습니다. 변형이 만들어지는 대로 대결이 이어집니다.');
  app.go('refine');
}

export function emptyState({ iconName = 'sparkle', title, text, action }) {
  return h('div', { class: 'empty-state fade-in' },
    h('div', { class: 'icon-badge' }, icon(iconName)), h('h3', {}, title), text ? h('p', {}, text) : null, action || null);
}

export function notice({ tone = 'accent', iconName = 'info', title, text, action }) {
  return h('div', { class: `notice ${tone}` },
    h('div', { class: `icon-badge ${tone === 'amber' ? 'amber' : ''}` }, icon(iconName)),
    h('div', { class: 'body' }, title ? h('strong', {}, title) : null, text ? h('span', {}, text) : null),
    action || null);
}

// How a switch, toggle or tab outside 설정 saves its setting: silently (the control itself shows the change).
// On failure the error is shown and false comes back, so the caller puts the control back as it was.
export async function saveSetting(changes) {
  try {
    await post('/api/settings', { changes });
    return true;
  } catch (error) {
    toast(error.message, 'error');
    return false;
  }
}

export function stat(label, value, sub) {
  return h('div', { class: 'card stat' }, h('div', { class: 'label' }, label),
    h('div', { class: 'value' }, value), sub ? h('div', { class: 'sub' }, sub) : null);
}

// Tier distribution: a single stacked bar, 2px surface gaps, legend with counts, per-segment tooltip.
// ``compact``: a small one-line legend, to sit beside a number (the home screen's 조합 card).
export function tierDistribution(combos, { compact = false } = {}) {
  const counts = Object.fromEntries(TIERS.map((t) => [t, 0]));
  let unrated = 0;
  for (const c of combos) {
    if (c.tier) counts[c.tier] += 1;
    else unrated += 1;
  }
  const total = TIERS.reduce((sum, t) => sum + counts[t], 0) || 1;
  const bar = h('div', { class: 'dist', role: 'img', 'aria-label': 'tier distribution' });
  for (const t of TIERS) {
    if (!counts[t]) continue;
    const seg = h('span', { style: { flex: `${counts[t]} 1 0`, background: tierVar(t) } });
    tooltip(seg, () => h('div', {}, h('div', { class: 't' }, `${t} 티어`),
      h('div', { class: 'r' }, h('i', { style: { background: tierVar(t) } }), '조합', h('b', {}, `${counts[t]}개`)),
      h('div', { class: 'r' }, h('i', { style: { background: 'transparent' } }), '비율', h('b', {}, `${Math.round((counts[t] / total) * 100)}%`))));
    bar.append(seg);
  }
  const legend = h('div', { class: `legend ${compact ? 'compact' : ''}` },
    // compact: the letter in its tier's color instead of a swatch, so all five fit on one line beside a number
    TIERS.map((t) => (compact ? h('span', { style: { color: tierVar(t) } }, t, h('b', {}, counts[t]))
      : h('span', {}, h('i', { style: { background: tierVar(t) } }), t, h('b', {}, counts[t])))),
    unrated ? h('span', {}, h('i', { style: { background: 'var(--surface-4)' } }), '평가중', h('b', {}, unrated)) : null);
  return h('div', {}, bar, legend);
}
