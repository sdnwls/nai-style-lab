"""State, workflow and background jobs for NAI Style Lab.

Everything the UI can do goes through :class:`Engine`. All state changes happen under one
re-entrant lock and are saved atomically, so the HTTP threads and the generation thread never
race. Pure calculations live in :mod:`core`.
"""
import collections
import copy
import io
import json
import math
import random
import shutil
import threading
import time
import uuid
import zipfile
import zlib
from datetime import datetime
from pathlib import Path

import core

STAGES = ('준비', '순위 결정', '진화', '다듬기', '완성')
ARENA_MODES = ('auto', 'league', 'top')
IMPROVE_JITTER_START = 0.3
IMPROVE_JITTER_MIN = 0.1
IMPROVE_JITTER_STEP = 0.1  # the adjustment narrows by this much each round
FINAL_CHECK_SEEDS = 3
CONVERGED_GENERATIONS = 2  # generations in a row where no child survived
MIN_RATED_FOR_EVOLUTION = 10
COMBO_KEYS = {'id', 'style', 'elo', 'matches', 'wins'}  # what an imported combo must have
MAX_FAILURE_STREAK = 3  # images failing in a row (each already retried) stop the job
DRAW_TRIES = 30  # draws to find a style not made before, per image (random combos, evolution, refine variants)

SETTING_DEFAULTS = {
    'model': core.MODELS[0], 'size': list(core.SIZE_PRESETS)[0], 'sampler': core.SAMPLERS[0],
    'steps': 28, 'cfg': 5.0, 'cfg_rescale': 0.0, 'delay': 1.0, 'seed': '',
    'global_min_w': 0.8, 'global_max_w': 1.8, 'gen_min': 4, 'gen_max': 8,
    'gen_count': 50, 'evo_count': 20, 'improve_variants': 4,
    'base_prompt': core.DEFAULT_BASE_PROMPT, 'character_prompt': [], 'negative': core.DEFAULT_NEGATIVE,
    'arena_mode': 'auto', 'arena_blind': False,
    'prompt_checked': False, 'artists_checked': False,
}
PROMPT_KEYS = ('base_prompt', 'character_prompt', 'negative')
SETTING_TYPES = {key: type(value) for key, value in SETTING_DEFAULTS.items()}
# Same limits as the settings screen: (label, low, high).
SETTING_RANGES = {
    'steps': ('Steps', 1, 50), 'cfg': ('CFG', 0, 20), 'cfg_rescale': ('CFG Rescale', 0, 1),
    'delay': ('생성 간 대기', 0, 30), 'global_min_w': ('최소 가중치', 0.1, 3), 'global_max_w': ('최대 가중치', 0.1, 3),
    'gen_min': ('조합당 최소 작가 수', 1, 20), 'gen_max': ('조합당 최대 작가 수', 1, 20),
    'gen_count': ('무작위 조합 기본 개수', 1, 200), 'evo_count': ('진화 조합 수', 1, 60),
    'improve_variants': ('다듬기 라운드당 변형', 2, 12),
}
SETTING_CHOICES = {'model': core.MODELS, 'size': list(core.SIZE_PRESETS), 'sampler': core.SAMPLERS, 'arena_mode': ARENA_MODES}


class UserError(Exception):
    """A request the user can fix (shown as a message, not a crash)."""


def _now():
    return datetime.now().strftime('%H:%M:%S')


def _new_id():
    return uuid.uuid4().hex[:10]


def _next_jitter(jitter):
    return max(IMPROVE_JITTER_MIN, round(jitter - IMPROVE_JITTER_STEP, 1))


def _jitter_schedule():
    """Every round's adjustment, first to last: [0.3, 0.2, 0.1]."""
    steps = round((IMPROVE_JITTER_START - IMPROVE_JITTER_MIN) / IMPROVE_JITTER_STEP)
    return [round(IMPROVE_JITTER_START - i * IMPROVE_JITTER_STEP, 1) for i in range(steps + 1)]


class Engine:
    def __init__(self, data_dir: Path, generate=None, fetch_subscription=None, auto_subscription=False):
        self.data_dir = Path(data_dir)
        self.img_dir = self.data_dir / 'images'
        self.thumb_dir = self.data_dir / 'thumbs'
        self.state_file = self.data_dir / 'state.json'
        self.key_file = self.data_dir / 'api_key.txt'
        self.img_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        # Injected for tests; the real ones call NovelAI.
        self._generate = generate or core.generate_for_settings
        self._fetch_subscription = fetch_subscription or core.fetch_user_data
        self.state = core.load_state(self.state_file)
        self.api_key = self.key_file.read_text(encoding='utf-8').strip() if self.key_file.exists() else ''
        self.events = collections.deque(maxlen=60)
        self._event_id = 0
        self.job = None
        self.stop_event = threading.Event()
        self.current = None
        self._last_pair = ()
        self.undo_stack = collections.deque(maxlen=30)
        self.skipped_ties = set()
        self.subscription = None
        self.auto_subscription = auto_subscription  # the app checks it around every run; tests never call NovelAI
        self.generating_children = None  # (made, total) while an evolution batch is being generated
        if self.state.get('load_error'):
            backup = core.backup_file(self.state_file)
            hint = f' 직전 실행 때의 백업은 {backup.name}입니다.' if backup.exists() else ''
            self._event('warn', f"state.json을 읽지 못해 새로 시작했습니다. 원본은 {self.state.pop('load_error')}에 보존했습니다.{hint}")

    # ------------------------------------------------------------------ basics
    def save(self):
        with self.lock:
            core.write_state(self.state_file, self.state)

    def _event(self, level, text):
        self._event_id += 1
        self.events.append({'id': self._event_id, 'level': level, 'text': text, 'time': _now()})

    def _event_locked(self, level, text):
        with self.lock:
            self._event(level, text)

    def events_since(self, since):
        return [event for event in self.events if event['id'] > since]

    @property
    def combos(self):
        return self.state['combinations']

    @property
    def selection(self):
        return self.state['selection']

    @property
    def retired(self):
        return self.selection['retired']

    def settings(self):
        ui = self.state['ui_state']
        s = {key: ui.get(key, default) for key, default in SETTING_DEFAULTS.items()}
        for key, choices in SETTING_CHOICES.items():
            if s[key] not in choices:  # a choice a later release dropped (the 960x1088 size): back to the default
                s[key] = SETTING_DEFAULTS[key]
        return s

    def update_settings(self, changes: dict):
        with self.lock:
            ui = self.state['ui_state']
            before = dict(ui)
            try:
                api_key = self._apply_settings(ui, changes)
            except UserError:
                ui.clear()
                ui.update(before)  # a refused change leaves nothing behind
                raise
            if api_key is not None:
                self.api_key = api_key
                self.key_file.write_text(api_key, encoding='utf-8')
            if 'arena_mode' in changes:
                self.current = None
            self.save()
            return self.settings()

    def _apply_settings(self, ui, changes):
        """Write checked ``changes`` into ``ui``; returns a new API key or None. Raises UserError."""
        api_key = None
        for key, value in changes.items():
            if key == 'api_key':
                api_key = str(value).strip()
                continue
            if key not in SETTING_TYPES:
                raise UserError(f'알 수 없는 설정입니다: {key}')
            kind = SETTING_TYPES[key]
            try:
                if kind is bool and not isinstance(value, bool):
                    raise TypeError
                value = [str(v) for v in value if str(v).strip()] if kind is list else kind(value)
            except (TypeError, ValueError):
                raise UserError(f'{key} 값이 올바르지 않습니다.')
            if key == 'seed':
                value = value.strip()
                if value and (not value.isdigit() or int(value) > core.MAX_SEED):
                    raise UserError(f'시드는 0 ~ {core.MAX_SEED} 사이의 정수로 입력하거나 비워 두세요.')
            if key in SETTING_CHOICES and value not in SETTING_CHOICES[key]:
                raise UserError(f'{key} 값이 올바르지 않습니다.')
            if key in SETTING_RANGES:
                label, low, high = SETTING_RANGES[key]
                if not low <= value <= high:  # also refuses NaN
                    raise UserError(f'{label}은(는) {low} ~ {high} 사이로 입력해 주세요.')
            ui[key] = value
            if key in PROMPT_KEYS:
                ui['prompt_checked'] = True  # editing the prompt is checking it
        s = self.settings()
        if s['gen_max'] < s['gen_min']:
            raise UserError('조합당 작가 수 범위를 확인해 주세요 (최소 ≤ 최대).')
        if not s['global_min_w'] < s['global_max_w']:
            raise UserError('가중치 범위를 확인해 주세요 (최소 < 최대).')
        return api_key

    def _start_count(self, key, value):
        """The count a start request asks for (or the saved one), checked like the settings field and
        kept as the next default, so the pages need no separate settings save."""
        if value is None:
            return self.settings()[key]
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise UserError('개수가 올바르지 않습니다.')
        self.update_settings({key: value})
        return value

    def _gen_settings(self):
        """Settings for one generation run; an empty seed gets a random one that is kept."""
        s = self.settings()
        if not self.api_key:
            raise UserError('설정에서 NovelAI API 키를 먼저 입력해 주세요.')
        if not s['seed']:
            s['seed'] = str(random.randint(0, core.MAX_SEED))
            self.state['ui_state']['seed'] = s['seed']
            self._event('info', f"시드를 {s['seed']}(으)로 정했습니다. 앞으로 모든 그림이 이 시드로 생성됩니다.")
        width, height = core.SIZE_PRESETS[s['size']]
        return dict(api_key=self.api_key, model=s['model'], width=width, height=height, steps=s['steps'],
                    cfg=s['cfg'], cfg_rescale=s['cfg_rescale'], sampler=s['sampler'], delay=s['delay'],
                    base_prompt=s['base_prompt'], character_prompt=s['character_prompt'],
                    negative=s['negative'], seed=int(s['seed']))

    # ------------------------------------------------------------------ lookups
    def find(self, combo_id):
        for combo in self.combos:
            if combo['id'] == combo_id:
                return combo
        return None

    def find_any(self, combo_id):
        found = self.find(combo_id)
        if found:
            return found
        for combo in self.retired:
            if combo['id'] == combo_id:
                return combo
        improve = self.state.get('improve') or {}
        return (improve.get('items') or {}).get(combo_id)

    def rated(self):
        return [c for c in self.combos if core.is_rated(c)]

    def newcomers(self):
        children = set(self.selection['candidate_ids'])
        return [c for c in self.combos if not core.is_rated(c) and c['id'] not in children]

    def ranking_counts(self, rated=None, waiting=None):
        """(ranked, waiting) as the user sees them. The first combo becomes the anchor without a vote,
        so until a second one is placed there is no ranking yet and it still counts as waiting.
        ``rated`` / ``waiting``: the raw counts, when the caller already has them."""
        rated = len(self.rated()) if rated is None else rated
        waiting = len(self.newcomers()) if waiting is None else waiting
        return (rated, waiting) if rated >= 2 else (0, waiting + rated)

    def top_ids(self):
        return core.top_tier_ids(self.combos, set(self.selection['candidate_ids']))

    def boundary_tie(self):
        return core.boundary_tie_pair(self.combos, set(self.selection['candidate_ids']), self.skipped_ties)

    def top_tie(self):
        return core.top_tie_pair(self.combos, set(self.selection['candidate_ids']), self.skipped_ties)

    # ------------------------------------------------------------------ views
    def _tiers(self):
        """(tier, provisional, grade, #n, child of the running batch) for every active combo, computed once.

        #n is the place in the whole ranking (core.rank_order): every page shows this same number.
        """
        rated = self.rated()
        elos = sorted(c['elo'] for c in rated)
        places = {c['id']: n for n, c in enumerate(core.rank_order(rated), 1)} if len(rated) >= 2 else {}
        children = set(self.selection['candidate_ids'])
        out = {}
        for combo in self.combos:
            child = combo['id'] in children
            if not core.is_rated(combo) or len(rated) < 2:
                out[combo['id']] = (None, False, None, None, child)
                continue
            rank = core.rank_among(combo['elo'], elos)
            out[combo['id']] = (core.tier_for_percentile(rank / len(elos)),
                                not core.tier_confirmed(combo['matches'], len(elos)),
                                core.tier_grade(rank / len(elos)), places[combo['id']], child)
        return out

    def combo_view(self, combo, tiers=None, role=None):
        tiers = tiers if tiers is not None else self._tiers()
        tier, provisional, grade, rank, child = tiers.get(combo['id'], (None, False, None, None, False))
        excluded = (combo.get('excluded') or {}).get('reason')
        image = combo.get('image_file')
        return {
            'id': combo['id'], 'style': combo['style'],
            'pairs': [{'w': w, 'tag': t} for w, t in core.parse_style_combo(combo['style'])],
            'elo': round(combo['elo']), 'tier': tier, 'grade': grade, 'provisional': provisional, 'rank': rank,
            # An active combo without a tier while rated is the lone first anchor: not ranked against anything yet.
            'rated': core.is_rated(combo) and (tier is not None or combo['id'] not in tiers), 'matches': combo['matches'], 'wins': combo['wins'],
            'generation': combo.get('generation', 1),
            'image': f'/img/{image}' if image else None, 'thumb': f'/thumb/{image}' if image else None,
            'excluded': excluded,
            'child': child, 'role': role,
            'seed': combo.get('seed'), 'final': bool(combo.get('final')), 'created': combo.get('created', 0),
        }

    def list_combos(self):
        with self.lock:
            tiers = self._tiers()
            active = [self.combo_view(c, tiers) for c in self.combos]
            excluded = [self.combo_view(c, tiers) for c in self.retired]
            return {'active': active, 'excluded': excluded}

    # ------------------------------------------------------------------ jobs
    def job_view(self):
        job = self.job
        if not job:
            return None
        return {k: job[k] for k in ('label', 'done', 'total', 'failures', 'running')}

    def _require_idle(self, action, kinds=None):
        """Refuse a change while images are being made (or only while a job of one of ``kinds`` runs)."""
        job = self.job
        if job and job['running'] and (kinds is None or job['kind'] in kinds):
            raise UserError(f'생성 작업이 끝난 뒤 {action} 주세요.')

    def _start_job(self, kind, label, total, work):
        # label: short enough for the sidebar's job card (135px) even at 99세대; the count shows there as done / total.
        self._require_idle('다시 시도해')
        self.stop_event.clear()
        self.job = {'kind': kind, 'label': label, 'done': 0, 'total': total, 'failures': 0,
                    'running': True, 'streak': 0, 'renders': 0}
        job = self.job

        def run():
            try:
                work(job)
            except Exception as exc:  # a crashed job must still end cleanly
                with self.lock:
                    job['failures'] += 1
                    self._event('error', f'{label} 중 오류가 났습니다: {exc}')
            finally:
                with self.lock:
                    job['running'] = False
                    self.generating_children = None if kind == 'evolution' else self.generating_children
                    self.save()
                    self._after_job(kind)
                if self.auto_subscription:
                    self.refresh_subscription()  # what the run used

        threading.Thread(target=run, daemon=True).start()
        if self.auto_subscription:
            self.refresh_subscription()

    def stop_job(self):
        self.stop_event.set()

    def _render(self, job, settings, style, seed):
        """Generate one image outside the lock; returns the saved file name or None on failure (or a stop).

        The set delay goes between a job's images: never before its first one or after its last.
        """
        if job['renders']:
            self._pause(settings)
            if self.stop_event.is_set():
                return None
        job['renders'] += 1
        try:
            png = self._generate(settings, style, seed, self.stop_event,
                                 lambda text: self._event_locked('warn', text))
        except Exception as exc:
            with self.lock:
                job['failures'] += 1
                self._event('error', f'이미지를 만들지 못했습니다: {exc}')
                job['streak'] += 1
                if getattr(exc, 'status', None) in core.AUTH_ERROR_STATUSES:
                    self._event('error', 'API 키 인증에 실패해 작업을 멈췄습니다. 설정에서 키를 확인해 주세요.')
                    self.stop_event.set()
                elif job['streak'] >= MAX_FAILURE_STREAK and not self.stop_event.is_set():
                    self._event('error', f'이미지가 {MAX_FAILURE_STREAK}번 연속 실패해 작업을 멈췄습니다. '
                                         '설정(프롬프트·모델·크기)이나 네트워크를 확인해 주세요.')
                    self.stop_event.set()
            return None
        name = f'{_new_id()}.png'
        (self.img_dir / name).write_bytes(png)
        job['streak'] = 0
        return name

    def _draw_and_render(self, job, settings, count, draw, keep, seen):
        """The loop shared by random combos, evolution and refine variants: ``count`` images, each from
        ``draw()`` -> (style, info), drawn again (up to DRAW_TRIES) while that style was made before.
        ``keep(style, name, info)`` runs under the lock for every image made. Returns how many were made."""
        made = 0
        for _ in range(count):
            if self.stop_event.is_set():
                break
            for _ in range(DRAW_TRIES):
                style, info = draw()
                if style not in seen:
                    break
            else:
                with self.lock:
                    self._event('warn', '더 이상 새로운 조합을 만들 수 없어 만든 만큼으로 마칩니다.')
                break
            seen.add(style)
            name = self._render(job, settings, style, settings['seed'])
            with self.lock:
                job['done'] += 1
                if name:
                    made += 1
                    keep(style, name, info)
                    self.save()
        return made

    def _new_combo(self, style, name, settings, **fields):
        """A combo just drawn: random ones, evolution children and refine variants start from the same fields."""
        return {'id': _new_id(), 'style': style, 'elo': core.START_ELO, 'matches': 0, 'wins': 0, 'placed': False,
                'image_file': name, 'seed': settings['seed'], 'created': time.time(),
                'generation': self.selection['generation'], **fields}

    def _pause(self, settings):
        if settings['delay'] > 0:
            self.stop_event.wait(settings['delay'])

    def _after_job(self, kind):
        if kind == 'improve':
            self._improve_round_check()

    # ------------------------------------------------------------------ generation: random / free
    def start_random(self, count=None):
        with self.lock:
            s = self.settings()
            count = self._start_count('gen_count', count)
            tags = [a['tag'] for a in self.state['artists']]
            if len(tags) < s['gen_min']:
                raise UserError(f"작가를 최소 {s['gen_min']}명 이상 등록해 주세요.")
            settings = self._gen_settings()
            seen = {c['style'] for c in self.combos + self.retired}
            self.save()

        def draw():
            size = random.randint(s['gen_min'], min(s['gen_max'], len(tags)))
            picked = random.sample(tags, size)
            weights = [core.clamp_weight(random.uniform(s['global_min_w'], s['global_max_w']),
                                         s['global_min_w'], s['global_max_w']) for _ in picked]
            return core.style_from_pairs(list(zip(weights, picked))), set(picked)

        def keep(style, name, used):
            self.combos.append(self._new_combo(style, name, settings, generation=self.selection['generation'] - 1))
            for artist in self.state['artists']:
                if artist['tag'] in used:
                    artist['count'] += 1

        def work(job):
            made = self._draw_and_render(job, settings, count, draw, keep, seen)
            with self.lock:
                self._event('ok', f'새 조합 {made}개를 만들었습니다. 대결에서 자리를 찾아 주세요.')

        with self.lock:
            self._start_job('random', '새 조합 생성', count, work)

    def start_custom(self, text):
        """One combo exactly as typed (its artist: tags and weights), drawn and added like a random one.
        Artists it names that are not registered yet are registered too."""
        scan = self.scan_artists(text)
        if not scan['pairs']:
            raise UserError('artist: 태그를 하나 이상 넣어 주세요.')
        style = core.style_from_pairs([(p['w'], p['tag']) for p in scan['pairs']])
        with self.lock:
            if any(c['style'] == style for c in self.combos + self.retired):
                raise UserError('이미 그림체 목록에 있는 조합입니다.')
            settings = self._gen_settings()
            self.save()

        def work(job):
            name = self._render(job, settings, style, settings['seed'])
            with self.lock:
                job['done'] = 1
                if not name:
                    return
                used = {p['tag'] for p in scan['pairs']}
                for tag in scan['new']:
                    self.state['artists'].append({'tag': tag, 'count': 0, 'arena_matches': 0, 'arena_wins': 0})
                for artist in self.state['artists']:
                    if artist['tag'] in used:
                        artist['count'] += 1
                self.combos.append(self._new_combo(style, name, settings, generation=self.selection['generation'] - 1))
                self.save()
                self._event('ok', '조합을 만들었습니다. 대결에서 자리를 찾아 주세요.')

        with self.lock:
            self._start_job('custom', '조합 생성', 1, work)

    def start_free(self, prompt):
        with self.lock:
            settings = self._gen_settings()
            self.save()

        def work(job):
            name = self._render(job, settings, prompt.strip(), settings['seed'])
            with self.lock:
                job['done'] = 1
                if name:
                    self.state['free_results'].insert(0, {'image': name, 'prompt': prompt, 'time': _now(),
                                                          'seed': settings['seed']})
                    self._event('ok', '자유 생성 이미지를 만들었습니다.')

        with self.lock:
            self._start_job('free', '자유 생성', 1, work)

    # ------------------------------------------------------------------ artists
    def list_artists(self):
        with self.lock:
            scores = core.artist_scores(self.combos + self.retired)
            return [{**a, 'winrate': a['arena_wins'] / a['arena_matches'] if a['arena_matches'] else 0,
                     'score': scores.get(a['tag'], (0, 0))[0], 'combos': scores.get(a['tag'], (0, 0))[1]}
                    for a in self.state['artists']]

    def add_artists(self, text):
        with self.lock:
            existing = {a['tag'] for a in self.state['artists']}
            added = 0
            # With any artist: tag it is a prompt (pasted, or from a dropped picture): those tags only. Without, a
            # list of bare names: every comma/line-separated entry.
            names = core.artists_in_prompt(text) if 'artist:' in text else core.extract_artist_names(text)
            for name in names:
                tag = core.sanitize_tag(name)
                if tag and tag not in existing:
                    self.state['artists'].append({'tag': tag, 'count': 0, 'arena_matches': 0,
                                                  'arena_wins': 0})
                    existing.add(tag)
                    added += 1
            self._artists_checked()
            self.save()
            return added

    def scan_artists(self, text):
        """The artists a prompt names with their weights, as the combos' pairs (tags as add_artists would
        register them, each once), and which are not registered yet."""
        weights = {}
        for w, raw in core.artist_weights_in_prompt(text):
            tag = core.sanitize_tag(raw)
            if tag:
                weights.setdefault(tag, w)
        with self.lock:
            existing = {a['tag'] for a in self.state['artists']}
        return {'pairs': [{'w': w, 'tag': t} for t, w in weights.items()], 'new': [t for t in weights if t not in existing]}

    def rename_artist(self, old, new):
        with self.lock:
            new = core.sanitize_tag(new or '')
            if not new or new == old:
                return
            if any(a['tag'] == new for a in self.state['artists']):
                raise UserError('이미 있는 작가 이름입니다.')
            self._require_idle('이름을 바꿔')  # a running job still draws with the old name
            for artist in self.state['artists']:
                if artist['tag'] == old:
                    artist['tag'] = new
            improving = list(((self.state.get('improve') or {}).get('items') or {}).values())
            for combo in self.combos + self.retired + improving:
                combo['style'] = core.rename_artist_in_style(combo['style'], old, new)
            self._artists_checked()
            self.save()

    def _artists_checked(self):
        """Editing the artist list is checking it (the setup step starts from the default list)."""
        self.state['ui_state']['artists_checked'] = True

    def delete_artists(self, tags):
        with self.lock:
            tags = set(tags)
            self.state['artists'] = [a for a in self.state['artists'] if a['tag'] not in tags]
            self._artists_checked()
            self.save()

    # ------------------------------------------------------------------ arena: choosing the next match
    def match(self, last=None):
        """The current match (kept until voted/skipped), choosing a new one if needed.
        ``last``: the match just judged, handed back as it is now (blind mode reveals it)."""
        with self.lock:
            desired = self._priority_pair()
            if desired:
                kind, a_id, b_id, meta = desired
                if not (self.current and self.current['kind'] == kind
                        and {self.current['a'], self.current['b']} == {a_id, b_id}):
                    if (hash(frozenset((a_id, b_id))) & 1):  # stable, unbiased side
                        a_id, b_id = b_id, a_id
                    self.current = {'kind': kind, 'a': a_id, 'b': b_id, 'meta': meta}
                else:
                    self.current['meta'] = meta
            elif not (self.current and self.current['kind'] in ('league', 'top')
                      and self.find(self.current['a']) and self.find(self.current['b'])):
                self.current = self._league_pair()
            return self._match_view(last)

    def _priority_pair(self):
        mode = self.settings()['arena_mode']
        tie = self.boundary_tie()
        if tie:
            return 'tie', tie[0], tie[1], {}
        tie = self.top_tie()
        if tie:
            return 'tie', tie[0], tie[1], {'inner': True}
        if mode != 'auto':
            return None
        for chooser in (self._placement_pair, self._selection_pair, self._improve_pair):
            pair = chooser()
            if pair:
                return pair
        return None

    def _league_pair(self):
        mode = self.settings()['arena_mode']
        children = set(self.selection['candidate_ids'])
        pool = [c for c in self.combos if core.is_rated(c) and c['id'] not in children]
        if mode == 'top':
            top = set(self.top_ids())
            leaders = [c for c in pool if c['id'] in top]
            pool = leaders if len(leaders) >= 2 else sorted(pool, key=lambda c: -c['elo'])[:2]
        if len(pool) < 2:
            return None
        weights = self._match_weights(pool, pool)
        last = self._last_pair
        for _ in range(10):
            a = random.choices(pool, weights=weights)[0]
            rest = [c for c in pool if c['id'] != a['id']]
            b = random.choices(rest, weights=self._match_weights(rest, pool))[0]
            if {a['id'], b['id']} != set(last or ()) or len(pool) == 2:
                break
        return {'kind': 'top' if mode == 'top' else 'league', 'a': a['id'], 'b': b['id'], 'meta': {}}

    @staticmethod
    def _match_weights(combos, pool):
        rated = sorted(c['elo'] for c in pool if core.is_rated(c))

        def weight(combo):
            m = combo['matches']
            value = 1.0 / (m + 1)
            if m >= 1 and len(rated) >= 2 and core.tier_confirmed(m, len(rated)):
                rank = core.rank_among(combo['elo'], rated)
                value *= core.TIER_MATCH_MULTIPLIER.get(core.tier_for_percentile(rank / len(rated)), 1.0)
            return value
        return [weight(c) for c in combos]

    def _match_view(self, last=None):
        cur = self.current
        base = {'stage': self.stage(), 'mode': self.settings()['arena_mode'], 'undo': bool(self.undo_stack)}
        tiers = self._tiers() if cur or last else None
        if last:
            base['last'] = {side: self.combo_view(combo, tiers) for side in ('a', 'b') if (combo := self.find_any(last[side]))}
        if not cur:
            return {**base, 'kind': None, 'message': self._idle_message()}
        roles = self._roles(cur)
        a, b = self.find_any(cur['a']), self.find_any(cur['b'])
        if not a or not b:
            self.current = None
            return {**base, 'kind': None, 'message': self._idle_message()}
        return {**base, 'kind': cur['kind'], 'context': self._context(cur),
                'a': self.combo_view(a, tiers, roles.get(a['id'])),
                'b': self.combo_view(b, tiers, roles.get(b['id']))}

    def _roles(self, cur):
        kind = cur['kind']
        # A newcomer being placed and an evolution child look alike (평가중 in the tier chip): neither gets a role.
        if kind == 'improve':
            imp = self.state['improve']
            return {imp['champion_id']: '챔피언', **{k: '변형' for k in imp['pending']}}
        return {}

    def _context(self, cur):
        kind = cur['kind']
        if kind == 'tie' and cur['meta'].get('inner'):
            return {'title': '상위 30% 동점', 'detail': '상위권에서 점수가 같은 두 조합입니다. 순위를 가리도록 더 마음에 드는 쪽을 골라 주세요.'}
        if kind == 'tie':
            return {'title': '상위 30% 경계 동점', 'detail': '상위 30%에 들 마지막 자리를 두고 점수가 같습니다. 더 마음에 드는 쪽을 골라 주세요.'}
        if kind == 'place':
            left = len(self.newcomers())
            p = self.state['placement']
            done = sum(1 for v in self._placement_votes(p))
            return {'title': '새 조합 자리 찾기', 'detail': f'기존 순위표 사이에서 위치를 찾는 중 · {done + 1}번째 비교 · 남은 새 조합 {left}개'}
        if kind == 'select':
            low, high = core.selection_remaining_matches(self.selection, self.combos, self.state['history'])
            remain = f'{low}' if low == high else f'{low}~{high}'
            return {'title': f"{self.selection['generation']}세대 진화 조합 평가",
                    'detail': f'새 조합이 상위 30% 사이 어디쯤인지 찾는 중 · 남은 대결 약 {remain}회'}
        if kind == 'improve':
            imp = self.state['improve']
            return {'title': f"다듬기 {imp['round']}라운드", 'detail': f"챔피언 vs 가중치 변형 · 조정 폭 ±{imp['jitter']:.1f} · 남은 변형 {len(imp['pending'])}개"}
        if kind == 'top':
            return {'title': '상위 30% 리그', 'detail': '상위권끼리 순위를 다듬습니다.'}
        return {'title': '순위 결정', 'detail': '할 일이 없을 때는 일반 대결로 순위를 더 정확하게 만듭니다.'}

    def _idle_message(self):
        if self.generating_children:
            made, total = self.generating_children
            return f'진화 조합을 만드는 중입니다 ({made}/{total}). 하나가 완성되면 바로 대결이 이어집니다.'
        if self.job and self.job['running']:
            return f"{self.job['label']} 중입니다. 완성되면 바로 대결이 이어집니다."
        if len(self.rated()) < 2 and not self.newcomers():
            return '대결할 조합이 없습니다. 먼저 새 조합을 만들어 주세요.'
        return '지금은 할 대결이 없습니다.'

    # ------------------------------------------------------------------ arena: voting
    def vote(self, side):
        with self.lock:
            cur = self.current
            if not cur or side not in ('a', 'b'):
                raise UserError('지금 투표할 대결이 없습니다.')
            winner, loser = (cur['a'], cur['b']) if side == 'a' else (cur['b'], cur['a'])
            handler = {'tie': self._vote_regular, 'league': self._vote_regular, 'top': self._vote_regular,
                       'place': self._vote_place, 'select': self._vote_select, 'improve': self._vote_improve}
            before = self.undo_stack[-1] if self.undo_stack else None
            handler[cur['kind']](cur, winner, loser)
            self._remember_match(cur, before)
            self._last_pair = (cur['a'], cur['b'])
            self.current = None
            self.save()
            return self.match(last=cur)

    def skip(self):
        with self.lock:
            cur = self.current
            if not cur:
                return self.match()
            kind = cur['kind']
            entry = None
            before = self.undo_stack[-1] if self.undo_stack else None
            if kind == 'tie':
                elo = self.find(cur['a'])['elo']
                self.skipped_ties.add((frozenset((cur['a'], cur['b'])), elo))
                self.undo_stack.append({'kind': 'tie_skip', 'key': (frozenset((cur['a'], cur['b'])), elo)})
            elif kind == 'place':
                self._record_place(cur, None)
            elif kind == 'select':
                entry = {'type': 'selection_skip', 'batch_id': self.selection['batch_id'],
                         'pair': {'a': cur['a'], 'b': cur['b']}}
            elif kind == 'improve':
                self._improve_step(None)  # "similar": shown again at the end of the round, then the champion keeps it
            else:  # league / top: nothing to change, but the user may change their mind: an undo step all the same
                self.undo_stack.append({'kind': 'skip'})
            if entry:
                self.state['history'].insert(0, entry)
                self.undo_stack.append({'kind': 'history', 'entry': entry,
                                        'placement': copy.deepcopy(self.state['placement'])})
                core.apply_selection_ratings(self.combos, self.selection, self.state['history'])
            self._remember_match(cur, before)
            self._last_pair = (cur['a'], cur['b'])
            self.current = None
            self.save()
            return self.match(last=cur)

    def _remember_match(self, cur, before):
        """The undo step this vote just added keeps the match itself, sides included, so undo shows it again."""
        if self.undo_stack and self.undo_stack[-1] is not before:
            self.undo_stack[-1]['match'] = copy.deepcopy(cur)

    def _vote_regular(self, cur, winner_id, loser_id):
        a, b = self.find(cur['a']), self.find(cur['b'])
        a_won = winner_id == a['id']
        tags_a, tags_b = set(core.artists_from_style(a['style'])), set(core.artists_from_style(b['style']))
        snap = {'kind': 'regular',
                'combos': {c['id']: {k: c[k] for k in ('elo', 'matches', 'wins')} for c in (a, b)},
                'artists': {x['tag']: {k: x[k] for k in ('arena_matches', 'arena_wins')}
                            for x in self.state['artists'] if x['tag'] in tags_a | tags_b}}
        a['elo'], b['elo'] = core.calc_elo(a['elo'], b['elo'], a_won)
        a['matches'] += 1
        b['matches'] += 1
        (a if a_won else b)['wins'] += 1
        for artist in self.state['artists']:
            in_a, in_b = artist['tag'] in tags_a, artist['tag'] in tags_b
            if in_a != in_b:
                artist['arena_matches'] += 1
                won = a_won if in_a else not a_won
                artist['arena_wins'] += 1 if won else 0
        entry = {'type': 'arena', 'pair': {'a': a['id'], 'b': b['id']}, 'winner_id': winner_id}
        self.state['history'].insert(0, entry)
        snap['entry'] = entry
        self.undo_stack.append(snap)

    def undo(self):
        with self.lock:
            if not self.undo_stack:
                raise UserError('되돌릴 대결이 없습니다.')
            snap = self.undo_stack.pop()
            kind = snap['kind']
            if kind in ('regular', 'history') or (kind == 'improve' and snap['entry']):
                history = self.state['history']
                index = next((i for i, e in enumerate(history) if e is snap['entry']), None)
                if index is None:
                    self.undo_stack.clear()
                    raise UserError('이 대결은 더 이상 되돌릴 수 없습니다.')
                history.pop(index)
            if kind == 'regular':
                for combo in self.combos:
                    combo.update(snap['combos'].get(combo['id'], {}))
                for artist in self.state['artists']:
                    artist.update(snap['artists'].get(artist['tag'], {}))
            elif kind == 'history':
                if 'combo' in snap:  # the vote had settled a placement: restore the newcomer
                    combo = self.find(snap['combo']['id'])
                    if combo:
                        combo.clear()
                        combo.update(snap['combo'])
                self.state['placement'] = snap['placement']
                if snap['entry']['type'].startswith('selection'):
                    core.apply_selection_ratings(self.combos, self.selection, self.state['history'])
            elif kind == 'improve':
                imp = self.state['improve']
                if snap.get('requeued'):
                    imp['pending'].remove(snap['challenger'])
                    imp['items'].get(snap['challenger'], {}).pop('similar', None)
                imp['pending'].insert(0, snap['challenger'])  # variants made since then stay queued behind it
                imp['items'].get(snap['challenger'], {}).pop('result', None)
                imp.update(snap['before'])
            elif kind == 'tie_skip':
                self.skipped_ties.discard(snap['key'])
            # Back to the very match that was undone (as it was shown), not whatever the arena would pick next.
            restored = snap.get('match')
            self.current = restored if restored and self.find_any(restored['a']) and self.find_any(restored['b']) else None
            self.save()
            return self._match_view() if self.current else self.match()

    # ------------------------------------------------------------------ newcomer placement
    def _placement_votes(self, placement):
        """(rung, result) for the placed newcomer, oldest first."""
        rung = {key: i for i, key in enumerate(placement['ladder'])}
        out = []
        for vote in reversed(self.state['history']):
            if vote.get('batch_id') != placement['batch'] or vote.get('type') not in ('place_vote', 'place_skip'):
                continue
            pair = vote['pair']
            other = pair['b'] if pair['a'] == placement['id'] else pair['a']
            if other not in rung:
                continue
            if vote['type'] == 'place_skip':
                out.append((rung[other], 'tie'))
            else:
                out.append((rung[other], 'win' if vote['winner_id'] == placement['id'] else 'loss'))
        return out

    def _ensure_placement(self):
        """Start placing the next newcomer; the very first combo simply becomes the START_ELO anchor."""
        p = self.state.get('placement')
        if p:
            by_id = {c['id']: c for c in self.combos}
            if p['id'] in by_id and not core.is_rated(by_id[p['id']]) and all(k in by_id for k in p['ladder']):
                return p
        self.state['placement'] = None
        children = set(self.selection['candidate_ids'])
        while True:
            queue = self.newcomers()
            if not queue:
                return None
            newcomer = queue[0]
            ladder = sorted((c for c in self.rated() if c['id'] not in children),
                            key=lambda c: (c['elo'], c['id']))
            if not ladder:
                newcomer['placed'] = True
                newcomer['elo'] = core.START_ELO
                continue
            self.state['placement'] = {'id': newcomer['id'], 'batch': _new_id(),
                                       'ladder': [c['id'] for c in ladder], 'scores': [c['elo'] for c in ladder]}
            return self.state['placement']

    def _placement_pair(self):
        p = self._ensure_placement()
        if not p:
            return None
        _, rung = core.placement_state(self._placement_votes(p), len(p['ladder']))
        if rung is None:
            self._settle_placement()
            return self._placement_pair()
        return 'place', p['id'], p['ladder'][rung], {}

    def _vote_place(self, cur, winner_id, loser_id):
        self._record_place(cur, winner_id)

    def _record_place(self, cur, winner_id):
        """A placement vote (or skip = tie when ``winner_id`` is None); settles the slot when decided."""
        p = self.state['placement']
        entry = {'type': 'place_vote' if winner_id else 'place_skip', 'batch_id': p['batch'],
                 'pair': {'a': cur['a'], 'b': cur['b']}}
        if winner_id:
            entry['winner_id'] = winner_id
        self.state['history'].insert(0, entry)
        snap = {'kind': 'history', 'entry': entry, 'placement': copy.deepcopy(p)}
        before = copy.deepcopy(self.find(p['id']))
        if self._settle_placement():
            snap['combo'] = before
        self.undo_stack.append(snap)

    def _settle_placement(self):
        """If the current newcomer's slot is settled, give it its Elo and move on. Returns True if settled."""
        p = self.state.get('placement')
        if not p:
            return False
        votes = self._placement_votes(p)
        slot, rung = core.placement_state(votes, len(p['ladder']))
        if rung is not None:
            return False
        combo = self.find(p['id'])
        if combo:
            combo['elo'] = core.placement_elo(slot, p['scores'])
            played = [v for v in votes if v[1] != 'tie']
            combo['matches'] += len(played)
            combo['wins'] += sum(1 for v in played if v[1] == 'win')
            combo['placed'] = True
            self._event('ok', f"새 조합이 {len(p['ladder']) - slot + 1}위 자리({combo['elo']}점)에 들어갔습니다.")
        self.state['placement'] = None
        return True

    # ------------------------------------------------------------------ evolution
    def evolution_view(self):
        with self.lock:
            sel = self.selection
            children = list(sel['candidate_ids'])
            tiers = self._tiers()
            parents = sel['parent_ids'] if children else self.top_ids()
            view = {'generation': sel['generation'], 'batch_active': bool(children or sel['batch_id']),
                    'log': sel['log'][-12:], 'parents': [], 'children': [], 'summary': None,
                    'blocker': self._evolution_blocker(),
                    'converged': self._converged(), 'converged_after': CONVERGED_GENERATIONS}
            view['can_start'] = view['blocker'] is None
            by_id = {c['id']: c for c in self.combos}
            view['parents'] = [self.combo_view(by_id[k], tiers) for k in parents if k in by_id]
            view['parents'].sort(key=lambda c: c['rank'] or float('inf'))
            if children:
                plan = self._generation_plan()
                info = plan['ranking']['info']
                for key in children:
                    if key not in by_id:
                        continue
                    child = self.combo_view(by_id[key], tiers)
                    pending = key in info and info[key]['rung'] is not None
                    child['status'] = ('평가중' if pending else '탈락' if key in plan['failed']
                                       else '상위 30% 진입' if key in plan['next_parents'] else '생존')
                    view['children'].append(child)
                pending = [c for c in view['children'] if c['status'] == '평가중']
                view['summary'] = {
                    'children': len(children), 'pending': len(pending),
                    'entered': sum(1 for c in view['children'] if c['status'] in ('생존', '상위 30% 진입')),
                    'failed': sum(1 for c in view['children'] if c['status'] == '탈락'),
                    'ready': self._selection_done(plan['ranking']),
                }
            return view

    def _evolution_blocker(self):
        s = self.settings()
        rated, waiting = len(self.rated()), len(self.newcomers())
        if rated < MIN_RATED_FOR_EVOLUTION:
            return f'평가된 조합이 {MIN_RATED_FOR_EVOLUTION}개 이상 필요합니다 (지금 {rated}개).'
        if waiting:
            return f'자리를 찾지 못한 새 조합 {waiting}개를 먼저 배치해 주세요.'
        if self.selection['candidate_ids']:
            return '진행 중인 진화 조합 평가를 먼저 마무리해 주세요.'
        if len(self.state['artists']) < s['gen_min']:
            return f"작가가 {s['gen_min']}명 이상 필요합니다."
        if self.boundary_tie():
            return '상위 30% 경계 동점을 먼저 해소해 주세요 (대결 화면에 나옵니다).'
        if self.top_tie():
            return '상위 30% 안의 동점을 먼저 해소해 주세요 (대결 화면에 나옵니다).'
        if len(self.top_ids()) < 2:
            return '진화에 쓸 상위 30% 조합이 2개 이상 필요합니다.'
        return None

    def start_evolution(self, count=None):
        with self.lock:
            self._require_idle('진화를 시작해')  # before anything changes: a refused start must leave no batch behind
            blocker = self._evolution_blocker()
            if blocker:
                raise UserError(blocker)
            s = self.settings()
            count = self._start_count('evo_count', count)
            settings = self._gen_settings()
            top = set(self.top_ids())
            parents = [c for c in self.combos if c['id'] in top]
            sel = self.selection
            sel['parent_ids'] = [c['id'] for c in parents]
            sel['parent_scores'] = {c['id']: c['elo'] for c in parents}
            sel['batch_id'] = _new_id()
            sel['candidate_ids'] = []
            self.generating_children = (0, count)
            self.undo_stack.clear()
            self.save()
            artist_tags = [a['tag'] for a in self.state['artists']]
            parent_counts = collections.Counter(t for c in parents for t in core.artists_from_style(c['style']))
            scores = {tag: score for tag, (score, _) in core.artist_scores(self.combos + self.retired).items()}
            preference = core.artist_preference(scores, artist_tags)
            ranked = core.rank_order(parents)
            seen = {c['style'] for c in self.combos + self.retired}

        def draw():
            p_a, p_b = core.pick_parents(ranked)
            mode = random.choices(core.BREED_MODES, weights=core.BREED_WEIGHTS)[0]
            return core.breed_weighted_style(p_a['style'], p_b['style'], artist_tags, s['gen_min'], s['gen_max'],
                                             s['global_min_w'], s['global_max_w'], mode, parent_counts,
                                             preference), None

        def keep(style, name, _):
            combo = self._new_combo(style, name, settings)  # generation: the evolution tab's
            self.combos.insert(0, combo)
            self.selection['candidate_ids'].append(combo['id'])
            self.generating_children = (len(self.selection['candidate_ids']), count)

        def work(job):
            self._draw_and_render(job, settings, count, draw, keep, seen)

        with self.lock:
            self._start_job('evolution', f"{self.selection['generation']}세대 진화 조합 생성", count, work)

    def _selection_pool(self):
        """The running batch's parents and children."""
        ids = set(self.selection['parent_ids']) | set(self.selection['candidate_ids'])
        return [c for c in self.combos if c['id'] in ids]

    def _selection_pair(self):
        sel = self.selection
        if not sel['candidate_ids']:
            return None
        ranking = core.selection_ranking(sel, self.state['history'])
        pair = core.selection_next_pair(sel, self._selection_pool(), self.state['history'], ranking)
        if pair and self.generating_children:
            # More children are coming and will move the cut: only place children for now.
            if not any(child['bisecting'] for child in ranking['info'].values()):
                return None
        return ('select', pair[0]['id'], pair[1]['id'], {}) if pair else None

    def _vote_select(self, cur, winner_id, loser_id):
        entry = {'type': 'selection_vote', 'pair': {'a': cur['a'], 'b': cur['b']}, 'winner_id': winner_id,
                 'batch_id': self.selection['batch_id']}
        self.state['history'].insert(0, entry)
        core.apply_selection_ratings(self.combos, self.selection, self.state['history'])
        self.undo_stack.append({'kind': 'history', 'entry': entry,
                                'placement': copy.deepcopy(self.state['placement'])})

    def _selection_done(self, ranking=None):
        sel = self.selection
        if not sel['candidate_ids'] or self.generating_children:
            return False
        return core.selection_next_pair(sel, self._selection_pool(), self.state['history'], ranking) is None

    def _generation_plan(self):
        """Failed children leave; as many existing combos as children got in drop from the bottom of
        the list Elo; the top 30% of what remains are the new parents."""
        sel = self.selection
        ranking = core.selection_ranking(sel, self.state['history'])
        parents = set(sel['parent_ids'])
        children = set(sel['candidate_ids'])
        active = {c['id']: c for c in self.combos}
        failed = {k for k in children if k in active and ranking['info'][k]['slot'] == 0}
        entered = [k for k in children if k in active and k not in failed]
        order = sorted((k for k in active if k not in failed),
                       key=lambda k: (active[k]['elo'], k in parents, k), reverse=True)
        removable = [k for k in reversed(order) if k not in children]
        dropped = removable[:len(entered)]
        survivors = [k for k in order if k not in dropped]
        next_parents = core.top_tier_ids([active[k] for k in survivors])
        return dict(ranking=ranking, order=order, next_parents=next_parents, failed=failed,
                    entered=entered, dropped=dropped)

    def finish_generation(self):
        with self.lock:
            self._require_idle('세대를 확정해', kinds=('evolution',))
            if not self.selection['candidate_ids']:
                raise UserError('확정할 진화 세대가 없습니다.')
            if not self._selection_done():
                raise UserError('남은 평가 대결을 먼저 마쳐 주세요.')
            core.apply_selection_ratings(self.combos, self.selection, self.state['history'])
            plan = self._generation_plan()
            sel = self.selection
            by_id = {c['id']: c for c in self.combos}
            out = [(k, '탈락') for k in plan['failed']] + [(k, '제외') for k in plan['dropped']]
            for key, reason in out:
                by_id[key]['excluded'] = {'reason': reason, 'generation': sel['generation']}
            self.retired.extend(by_id[k] for k, _ in out)
            gone = {k for k, _ in out}
            self.state['combinations'] = [c for c in self.combos if c['id'] not in gone]
            promoted = len(set(sel['candidate_ids']) & set(plan['next_parents']))
            sel['log'].append({'generation': sel['generation'], 'children': len(sel['candidate_ids']),
                               'entered': len(plan['entered']), 'promoted': promoted,
                               'failed': len(plan['failed']), 'dropped': len(plan['dropped'])})
            sel['parent_ids'] = plan['next_parents']
            sel['parent_scores'] = {k: by_id[k]['elo'] for k in plan['next_parents']}
            sel['candidate_ids'] = []
            sel['batch_id'] = None
            sel['generation'] += 1
            self.current = None
            self.undo_stack.clear()
            self._event('ok', f"{sel['generation'] - 1}세대 확정 · 생존 {len(plan['entered'])} · "
                              f"탈락 {len(plan['failed'])} · 기존 최하위 제외 {len(plan['dropped'])}")
            self.save()
            return sel['log'][-1]

    def _converged(self):
        log = self.selection['log']
        recent = log[-CONVERGED_GENERATIONS:]
        return len(recent) == CONVERGED_GENERATIONS and sum(g['entered'] for g in recent) == 0

    # ------------------------------------------------------------------ improve workbench
    def improve_view(self):
        with self.lock:
            imp = self.state.get('improve')
            tiers = self._tiers()
            candidates = core.rank_order(c for c in self.combos if core.is_rated(c))
            view = {'session': None, 'candidates': [self.combo_view(c, tiers) for c in candidates],
                    'jitters': _jitter_schedule(), 'final_seeds': FINAL_CHECK_SEEDS,
                    'finals': [self.combo_view(c, tiers) for c in self.combos if c.get('final')],
                    'top': self.top_ids()}  # the live top 30%, shown first as on 진화
            if not imp:
                return view
            items = imp['items']

            def item_view(key, role=None):
                combo = self.find_any(key)
                return self.combo_view(combo, tiers, role) if combo else None
            base = self.find_any(imp['base_id'])
            champion_combo = self.find_any(imp['champion_id']) if imp.get('champion_id') else None
            view['session'] = {
                'status': imp['status'], 'round': imp['round'], 'jitter': imp['jitter'], 'next_jitter': _next_jitter(imp['jitter']),
                'base': item_view(imp['base_id']), 'champion': item_view(imp['champion_id'], '챔피언'),
                'pending': [item_view(k, '변형') for k in imp['pending'] if k in items],
                'seed': imp['seed'], 'changed': imp['changed'],
                'variants': [{**item_view(k), 'result': items[k].get('result')}
                             for k in imp['order'] if k in items],
                'finals': [{'seed': f['seed'], 'champion': f'/img/{f["champion"]}' if f.get('champion') else None,
                            'base': f'/img/{f["base"]}' if f.get('base') else None} for f in imp.get('finals', [])],
                'same_as_base': bool(base and champion_combo and champion_combo['style'] == base['style']),
                'suggest_final': imp['status'] == 'round_done' and not imp['changed'] and imp['jitter'] <= IMPROVE_JITTER_MIN,
            }
            return view

    def start_improve(self, combo_id, variants=None):
        with self.lock:
            if self.state.get('improve'):
                raise UserError('진행 중인 다듬기를 먼저 마무리해 주세요.')
            self._require_idle('다듬기를 시작해')
            base = self.find(combo_id)
            if not base:
                raise UserError('다듬을 조합을 찾을 수 없습니다.')
            settings = self._gen_settings()
            count = self._start_count('improve_variants', variants)
            # The original is the first champion as drawn; variants use its seed, so only the weights differ.
            # ponytail: a prompt/model changed since the original was drawn also differs; re-render it if that matters.
            seed = base['seed'] if base.get('seed') is not None else settings['seed']
            self.state['improve'] = {
                'base_id': base['id'], 'champion_id': base['id'], 'items': {},
                'round': 1, 'jitter': IMPROVE_JITTER_START, 'pending': [], 'order': [], 'changed': False,
                'status': 'generating', 'seed': seed, 'variants': count,
                'finals': [],
            }
            self.save()
        self._improve_generate(settings, count)

    def _improve_generate(self, settings, count):
        s = self.settings()
        settings = {**settings, 'seed': self.state['improve']['seed']}

        def work(job):
            imp = self.state['improve']
            seen = {self.find_any(k)['style'] for k in imp['items']} | {self.find_any(imp['champion_id'])['style']}

            def draw():
                # The champion as it is now: a variant that wins mid-round is what the rest of the round varies.
                with self.lock:
                    pairs = core.parse_style_combo(self.find_any(imp['champion_id'])['style'])
                return core.style_from_pairs(core.jitter_weights(pairs, imp['jitter'], s['global_min_w'], s['global_max_w'])), None

            def keep(style, name, _):
                champion = self.find_any(imp['champion_id'])
                item = self._new_combo(style, name, settings, elo=champion['elo'], generation=champion.get('generation', 1))
                imp['items'][item['id']] = item
                # Ahead of the variants called "similar": those come back only at the end of the round.
                again = [n for n, k in enumerate(imp['pending']) if imp['items'].get(k, {}).get('similar')]
                imp['pending'].insert(again[0] if again else len(imp['pending']), item['id'])
                imp['order'].append(item['id'])
                imp['status'] = 'voting'

            self._draw_and_render(job, settings, count, draw, keep, seen)

        with self.lock:
            self._start_job('improve', f"다듬기 {self.state['improve']['round']}라운드 생성", count, work)

    def _improve_pair(self):
        imp = self.state.get('improve')
        if not imp or not imp['pending'] or not imp.get('champion_id'):
            return None
        return 'improve', imp['champion_id'], imp['pending'][0], {}

    def _vote_improve(self, cur, winner_id, loser_id):
        self._improve_step(winner_id, {'a': cur['a'], 'b': cur['b']})

    def _improve_step(self, winner_id, pair=None):
        """The first waiting variant meets the champion, undoably. ``winner_id`` None = "similar": the first time
        the variant goes to the end of the round to be shown again; the second time the champion keeps its title."""
        imp = self.state['improve']
        snap = {'kind': 'improve', 'challenger': imp['pending'].pop(0), 'entry': None,
                'before': {k: imp[k] for k in ('champion_id', 'changed', 'status')}}
        challenger = snap['challenger']
        item = imp['items'][challenger]
        if winner_id:
            item['result'] = 'win' if winner_id == challenger else 'loss'
            if winner_id == challenger:
                imp['champion_id'] = challenger
                imp['changed'] = True
            snap['entry'] = {'type': 'improve_vote', 'pair': pair, 'winner_id': winner_id}
            self.state['history'].insert(0, snap['entry'])
        elif not item.get('similar'):
            item['similar'] = True
            imp['pending'].append(challenger)
            snap['requeued'] = True
        else:
            item['result'] = 'loss'  # similar twice: it did not take the title (no vote in the history)
        self._improve_round_check()
        self.undo_stack.append(snap)

    def _improve_round_check(self):
        imp = self.state.get('improve')
        generating = self.job and self.job['running'] and self.job['kind'] == 'improve'
        if imp and not imp['pending'] and not generating and imp['status'] in ('voting', 'generating'):
            imp['status'] = 'round_done'

    def improve_next_round(self):
        with self.lock:
            imp = self.state.get('improve')
            if not imp or imp['status'] != 'round_done':
                raise UserError('지금은 다음 라운드를 시작할 수 없습니다.')
            self._require_idle('다음 라운드를 시작해')
            imp['round'] += 1
            imp['jitter'] = _next_jitter(imp['jitter'])
            imp['changed'] = False
            imp['status'] = 'generating'
            settings = self._gen_settings()
            count = imp['variants']
            self.undo_stack.clear()  # last round's votes are settled
            self.save()
        self._improve_generate(settings, count)

    def improve_final_check(self):
        with self.lock:
            imp = self.state.get('improve')
            if not imp or imp['status'] not in ('round_done', 'final_ready'):
                raise UserError('라운드를 마친 뒤 최종 확인을 할 수 있습니다.')
            self._require_idle('최종 확인을 시작해')  # before the old final images are dropped
            settings = self._gen_settings()
            champion = self.find_any(imp['champion_id'])
            base = self.find_any(imp['base_id'])
            # The champion never beat the original (same weights): draw it once per seed, not twice.
            same = champion['style'] == base['style']
            roles = (('base', base),) if same else (('champion', champion), ('base', base))
            seeds = [random.randint(0, core.MAX_SEED) for _ in range(FINAL_CHECK_SEEDS)]
            self._drop_final_images(imp)
            imp['finals'] = [{'seed': seed} for seed in seeds]
            imp['status'] = 'final_check'
            self.undo_stack.clear()
            self.save()

        def work(job):
            for final in imp['finals']:
                for role, combo in roles:
                    if self.stop_event.is_set():
                        break
                    name = self._render(job, settings, combo['style'], final['seed'])
                    with self.lock:
                        job['done'] += 1
                        final[role] = name
                        if same:
                            final['champion'] = name
                        self.save()
            with self.lock:
                if all(final.get('champion') and final.get('base') for final in imp['finals']):
                    imp['status'] = 'final_ready'
                else:  # stopped or failed part-way: back to the round instead of half-drawn previews
                    self._drop_final_images(imp)
                    imp['finals'] = []
                    imp['status'] = 'round_done'
                    self._event('warn', '최종 확인 이미지를 모두 그리지 못해 라운드 결과로 돌아갑니다. 다시 시도할 수 있습니다.')

        with self.lock:
            self._start_job('final', '다른 시드로 최종 확인', FINAL_CHECK_SEEDS * len(roles), work)

    def finish_improve(self, choice='champion'):
        """End the session: 'champion' keeps the winner, 'base' keeps the original, 'discard' keeps neither.

        A champion that differs from the original joins the ranking as a newcomer and is marked final;
        the other variants stay visible as ✕ 탈락.
        """
        with self.lock:
            imp = self.state.get('improve')
            if not imp:
                raise UserError('진행 중인 다듬기가 없습니다.')
            if choice not in ('champion', 'base', 'discard'):
                raise UserError('마무리 방법이 올바르지 않습니다.')
            self._require_idle('마무리해', kinds=('improve', 'final'))
            base = self.find(imp['base_id'])
            champion = imp['items'].get(imp['champion_id'])
            improved = choice == 'champion' and champion is not None
            result = None
            if improved:
                champion.update({'placed': False, 'final': True})
                champion.pop('result', None)
                champion.pop('similar', None)
                self.combos.append(champion)
                result = champion['id']
            elif choice != 'discard' and base:
                base['final'] = True
                result = base['id']
            for key, item in imp['items'].items():
                if key == result:
                    continue
                item.pop('result', None)
                item.pop('similar', None)
                item['excluded'] = {'reason': '탈락', 'generation': None, 'improve': imp['base_id']}
                self.retired.append(item)
            self._drop_final_images(imp)
            self.state['improve'] = None
            self.current = None
            self.undo_stack.clear()
            if improved:
                self._event('ok', '다듬은 조합을 최종 그림체로 저장했습니다. 대결에서 전체 순위 안의 자리를 찾습니다.')
            elif result:
                self._event('ok', '원본을 최종 그림체로 표시했습니다.')
            else:
                self._event('info', '다듬기를 끝냈습니다.')
            self.save()
            return result

    def improve_resume(self):
        """Back from the final check to keep refining."""
        with self.lock:
            imp = self.state.get('improve')
            if not imp or imp['status'] != 'final_ready':
                raise UserError('최종 확인 결과가 있을 때만 다듬기를 이어갈 수 있습니다.')
            self._drop_final_images(imp)
            imp['finals'] = []
            imp['status'] = 'round_done'
            self.save()

    def _drop_final_images(self, imp):
        """Final-check previews are only ever shown in the app's own window: removed as far as possible."""
        for final in imp.get('finals', []):
            for role in ('champion', 'base'):
                try:
                    self._delete_images([final.get(role)])
                except UserError:
                    pass

    # ------------------------------------------------------------------ combos: edit / delete / revive
    def _delete_images(self, names):
        """Delete images and their thumbnails, before the records go. Windows refuses a file another program
        holds open (a viewer): that fails the request and the records stay, so trying again finishes it."""
        for name in names:
            if not name or (self.img_dir / name).resolve().parent != self.img_dir.resolve():
                continue
            try:
                (self.img_dir / name).unlink(missing_ok=True)
                (self.thumb_dir / (Path(name).stem + '.webp')).unlink(missing_ok=True)
            except OSError:
                raise UserError('그림 파일이 다른 프로그램에서 열려 있어 삭제에 실패했습니다.')

    def _protected(self):
        sel = self.selection
        if not sel['candidate_ids']:
            return set()
        return set(sel['parent_ids']) | set(sel['candidate_ids'])

    def delete_combos(self, ids):
        with self.lock:
            self._require_idle('삭제해')
            ids = set(ids)
            if ids & self._protected():
                raise UserError('진행 중인 진화에 쓰이는 조합은 세대를 확정한 뒤 삭제할 수 있습니다.')
            imp = self.state.get('improve')
            if imp and imp['base_id'] in ids:
                raise UserError('다듬기 중인 원본은 다듬기를 마친 뒤 삭제할 수 있습니다.')
            targets = [c for c in self.combos + self.retired if c['id'] in ids]
            kept = {c.get('image_file') for c in self.combos + self.retired if c['id'] not in ids}
            self._delete_images(c.get('image_file') for c in targets if c.get('image_file') not in kept)  # before the records: a refusal keeps them
            self.state['combinations'] = [c for c in self.combos if c['id'] not in ids]
            self.selection['retired'] = [c for c in self.retired if c['id'] not in ids]
            if self.state.get('placement') and (self.state['placement']['id'] in ids
                                                or ids & set(self.state['placement']['ladder'])):
                self.state['placement'] = None
            self.current = None
            self.undo_stack.clear()
            self.save()
            return {'deleted': len(targets)}

    def purge(self, reason):
        with self.lock:
            ids = [c['id'] for c in self.retired if (c.get('excluded') or {}).get('reason', '제외') == reason]
            return self.delete_combos(ids)

    def revive(self, ids):
        with self.lock:
            self._require_idle('부활시켜')
            ids = set(ids)
            targets = [c for c in self.retired if c['id'] in ids]
            self.selection['retired'] = [c for c in self.retired if c['id'] not in ids]
            for combo in targets:
                combo.pop('excluded', None)
                if not core.is_rated(combo):
                    combo['placed'] = False  # never judged: let it find its place
                self.combos.append(combo)
            self.current = None
            self.undo_stack.clear()
            self.save()
            return len(targets)

    def set_elo(self, ids, elo):
        with self.lock:
            elo = int(elo)
            if elo < 0:
                raise UserError('Elo는 0 이상으로 입력해 주세요.')
            ids = set(ids)
            for combo in self.combos + self.retired:
                if combo['id'] in ids:
                    combo['elo'] = elo
                    if not core.is_rated(combo):
                        combo['placed'] = True
            # Ladders frozen for a running selection / placement take the new score at once.
            sel = self.selection
            if sel['candidate_ids'] and ids & set(sel['parent_scores']):
                sel['parent_scores'].update({key: elo for key in ids if key in sel['parent_scores']})
                core.apply_selection_ratings(self.combos, sel, self.state['history'])
            p = self.state.get('placement')
            if p and ids & set(p['ladder']):
                by_id = {c['id']: c for c in self.combos}
                if all(key in by_id for key in p['ladder']):
                    p['ladder'] = sorted(p['ladder'], key=lambda key: (by_id[key]['elo'], key))
                    p['scores'] = [by_id[key]['elo'] for key in p['ladder']]
                else:
                    self.state['placement'] = None  # a rung left the list: start this placement afresh
            self.current = None
            self.undo_stack.clear()
            self.save()

    def image_path(self, combo_id):
        with self.lock:
            combo = self.find_any(combo_id)
            return self.img_dir / combo['image_file'] if combo and combo.get('image_file') else None

    # ------------------------------------------------------------------ reset
    RESET_SCOPES = ('ratings', 'evolution', 'settings', 'artists', 'combos', 'all')

    def reset(self, scope):
        """Reset one part of the data. Nothing is backed up."""
        with self.lock:
            if scope not in self.RESET_SCOPES:
                raise UserError('초기화 범위가 올바르지 않습니다.')
            self._require_idle('초기화해')
            if scope in ('ratings', 'evolution') and self.selection['candidate_ids']:
                raise UserError('진행 중인 진화 세대를 확정한 뒤 초기화해 주세요.')
            if scope == 'ratings' and self.state.get('improve'):
                raise UserError('진행 중인 다듬기를 마친 뒤 초기화해 주세요.')
            s = self.state
            if scope == 'ratings':
                for combo in self.combos + self.retired:
                    combo.update(elo=core.START_ELO, matches=0, wins=0, placed=False)
                for artist in s['artists']:
                    artist.update(arena_matches=0, arena_wins=0)
                s['history'], s['placement'] = [], None
            elif scope == 'evolution':
                sel = self.selection
                sel.update(generation=1, log=[], parent_ids=[], parent_scores={}, candidate_ids=[], batch_id=None)
                for combo in self.combos + self.retired:
                    combo['generation'] = 0  # the next batch is 1세대 again
            elif scope == 'settings':
                seed = s['ui_state'].get('seed', '')
                s['ui_state'] = {'seed': seed}  # the seed stays so new images still compare with old ones
            elif scope == 'artists':
                s['artists'] = core.default_artists()
                s['ui_state']['artists_checked'] = False  # back to the default list: check it again
            else:
                self._delete_images(p.name for p in self.img_dir.iterdir() if p.is_file())  # before the records: a refusal keeps them
                shutil.rmtree(self.thumb_dir, ignore_errors=True)
                keep = {'artists': s['artists'], 'ui_state': s['ui_state']} if scope == 'combos' else {}
                self.state = {**core.empty_state(), **keep}
            if scope == 'all':
                self.api_key = ''
                self.key_file.unlink(missing_ok=True)
                self.subscription = None
            self.current = None
            self.undo_stack.clear()
            self.skipped_ties.clear()
            self.save()
            return {}

    # ------------------------------------------------------------------ export / import
    def export_data(self, target):
        """Write one zip with state.json, every image and the API key into the binary file ``target``
        (so another PC is ready to go: keep the zip private)."""
        with self.lock:
            self.save()
            # PNGs are already compressed: store them as is, which keeps big exports fast.
            with zipfile.ZipFile(target, 'w', zipfile.ZIP_STORED) as zf:
                zf.write(self.state_file, 'state.json', compress_type=zipfile.ZIP_DEFLATED)
                for image in sorted(self.img_dir.glob('*.png')):
                    zf.write(image, f'images/{image.name}')
                if self.api_key:
                    zf.writestr('api_key.txt', self.api_key, compress_type=zipfile.ZIP_DEFLATED)

    def import_data(self, blob: bytes):
        """Replace everything with an exported zip, its API key too if it has one. Nothing is backed up."""
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
            state = json.loads(zf.read('state.json').decode('utf-8'))
        except (zipfile.BadZipFile, KeyError, ValueError):
            raise UserError('NAI Style Lab에서 내보낸 zip 파일이 아닙니다.')
        try:  # an export from an older release is upgraded like its data would be at launch
            if isinstance(state, dict):
                core.upgrade_state(state)
        except core.NewerDataError:
            raise UserError('더 새 버전의 NAI Style Lab에서 내보낸 데이터입니다. 앱을 업데이트한 뒤 불러오세요.')
        except (ValueError, KeyError, TypeError, AttributeError):
            raise UserError('데이터 파일의 내용이 올바르지 않습니다.')
        combos = state.get('combinations') if isinstance(state, dict) else None
        if not isinstance(combos, list) or not all(isinstance(c, dict) and COMBO_KEYS <= c.keys() for c in combos):
            raise UserError('데이터 파일의 내용이 올바르지 않습니다.')
        try:  # older exports have no key: then the current one stays
            api_key = zf.read('api_key.txt').decode('utf-8').strip()
        except (KeyError, UnicodeDecodeError):
            api_key = ''
        # Only flat image names: nothing in the zip may write outside the images folder.
        images = [(info, Path(info.filename).name) for info in zf.infolist()
                  if info.filename.startswith('images/') and Path(info.filename).name.lower().endswith('.png')
                  and info.filename == f'images/{Path(info.filename).name}']
        # Nothing current is touched until the zip has fully unpacked and its state checks out.
        staging, retired_dir = self.data_dir / 'images.importing', self.data_dir / 'images.replaced'
        with self.lock:
            self._require_idle('불러와')
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(retired_dir, ignore_errors=True)
            staging.mkdir()
            try:
                for info, name in images:
                    (staging / name).write_bytes(zf.read(info))
            except (zipfile.BadZipFile, OSError) as exc:
                shutil.rmtree(staging, ignore_errors=True)
                raise UserError(f'zip 파일의 이미지를 풀지 못했습니다: {exc}')
            try:
                self.img_dir.rename(retired_dir)
                staging.rename(self.img_dir)
            except OSError as exc:
                if retired_dir.exists() and not self.img_dir.exists():
                    retired_dir.rename(self.img_dir)
                shutil.rmtree(staging, ignore_errors=True)
                raise UserError(f'이미지 폴더를 바꾸지 못했습니다. 잠시 뒤 다시 시도해 주세요: {exc}')
            self.state = {**core.empty_state(), **state}
            shutil.rmtree(retired_dir, ignore_errors=True)
            shutil.rmtree(self.thumb_dir, ignore_errors=True)
            if api_key and api_key != self.api_key:
                self.api_key = api_key
                self.key_file.write_text(api_key, encoding='utf-8')
                self.subscription = None  # it was the old key's
                if self.auto_subscription:
                    self.refresh_subscription()
            self.current = None
            self.undo_stack.clear()
            self.skipped_ties.clear()
            self.save()
            return {'combos': len(self.combos), 'images': len(images)}

    # ------------------------------------------------------------------ subscription
    def refresh_subscription(self):
        """Check the subscription in the background (at launch and when a run starts or ends), so the sidebar
        always shows it. Without an API key there is nothing to check."""
        if self.api_key:
            threading.Thread(target=self.check_subscription, daemon=True).start()

    def check_subscription(self):
        key = self.api_key
        if not key:
            raise UserError('API 키를 먼저 입력해 주세요.')
        try:
            data = self._fetch_subscription(key)
            error = None
        except Exception as exc:
            data, error = None, str(exc)
        with self.lock:
            self.subscription = {'data': data, 'error': error, 'checked': _now()}
            return self.subscription_view()

    def settings_payload(self):
        """Everything /api/settings returns; the pages keep one copy and refetch it when settings_rev changes."""
        return {'settings': self.settings(), 'api_key': self.api_key, 'subscription': self.subscription_view()}

    def subscription_view(self):
        sub = self.subscription
        if not sub:
            return None
        if sub['error']:
            return {'error': sub['error'], 'checked': sub['checked']}
        data = sub['data']
        s = self.settings()
        width, height = core.SIZE_PRESETS[s['size']]
        estimate = None
        if data['active'] and (data['tier_num'] == 3 or data['unlimited']):
            estimate = core.estimate_v5_images(data['remaining_percent'], width, height, s['steps'], s['model'])
        return {'tier': data['tier'], 'active': data['active'], 'anlas': data['anlas'],
                'remaining_percent': data['remaining_percent'],
                'estimate': {'left': estimate[0], 'total': estimate[1]} if estimate else None,
                'checked': sub['checked']}

    # ------------------------------------------------------------------ workflow stage
    def stage(self):
        """Where the user is in 준비 → 순위 결정 → 진화 → 다듬기 → 완성, and what to do now."""
        s = self.settings()
        checklist = {
            'api_key': bool(self.api_key),
            # Enough artists, and looked at: the default list alone is not a choice yet.
            'artists': len(self.state['artists']) >= s['gen_min'] and (s['artists_checked'] or bool(self.combos)),
            'prompt': s['prompt_checked'] or bool(self.combos),  # combos made before this step existed count as checked
            'combos': bool(self.combos),
        }
        imp = self.state.get('improve')
        rated = len(self.rated())
        newcomers = len(self.newcomers())
        running = self.job and self.job['running']
        shown_rated, waiting = self.ranking_counts(rated, newcomers)
        finals = any(c.get('final') for c in self.combos)  # a refine was finished: the journey is done
        if not all(checklist.values()):
            step = 0
            if not checklist['api_key']:
                todo = ('설정에서 NovelAI API 키를 입력하세요.', 'settings', '설정 열기')
            elif len(self.state['artists']) < s['gen_min']:
                todo = (f"좋아하는 작가를 {s['gen_min']}명 이상 등록하세요.", 'library', '작가 등록')
            elif not checklist['artists']:
                todo = ('작가 목록을 확인하세요. 좋아하는 작가를 더하거나 기본 목록을 그대로 써도 됩니다.', 'library', '작가 확인')
            elif not checklist['prompt']:
                todo = ('모든 그림에 쓰일 프롬프트를 확인하세요. 기본값 그대로 써도 됩니다.', 'settings', '프롬프트 확인')
            else:
                todo = ('첫 무작위 조합을 만들어 보세요.', 'library', '조합 만들기')
        elif imp:
            step = 4 if imp['status'] in ('final_check', 'final_ready') else 3
            if imp['status'] == 'final_ready':
                todo = ('다른 시드에서도 좋은지 확인하고 확정하세요.', 'refine', '최종 확인')
            elif imp['status'] == 'final_check':
                todo = ('다른 시드로 최종 확인 이미지를 그리는 중입니다.', 'refine', '진행 보기')
            elif imp['status'] == 'round_done':
                todo = ('라운드가 끝났습니다. 다음 라운드로 더 다듬거나 최종 확인하세요.', 'refine', '다듬기 보기')
            else:
                todo = ('챔피언과 변형을 비교하세요. 마음에 드는 쪽을 고르면 됩니다.', 'arena', '대결하기')
        elif newcomers or rated < MIN_RATED_FOR_EVOLUTION:
            step = 4 if finals else 1  # the refined style finding its place in the ranking is still 완성
            if newcomers:
                todo = (f'새 조합 {waiting}개의 자리를 찾아 주세요. 한 조합당 약 {max(1, math.ceil(math.log2(rated + 1)))}표면 됩니다.', 'arena', '대결하기')
            elif running:
                todo = ('새 조합을 만드는 중입니다. 완성되는 대로 대결이 시작됩니다.', 'arena', '대결하기')
            else:
                todo = (f'평가된 조합이 {shown_rated}개입니다. 진화를 시작하려면 {MIN_RATED_FOR_EVOLUTION}개 이상 필요합니다.', 'library', '조합 더 만들기')
        elif finals:
            step = 4
            todo = ('최종 그림체를 정했습니다. 다른 그림체를 더 다듬거나 진화를 이어 갈 수도 있습니다.', 'refine', '완성한 그림체 보기')
        else:
            step = 2
            sel = self.selection
            if sel['candidate_ids']:
                if self._selection_done():
                    todo = ('진화 조합 평가가 끝났습니다. 세대를 확정하세요.', 'evolution', '세대 확정')
                else:
                    todo = (f"{sel['generation']}세대 진화 조합을 평가하세요.", 'arena', '대결하기')
            elif self.boundary_tie():
                todo = ('상위 30% 경계에서 점수가 같은 조합이 생겼습니다. 대결에서 먼저 가려 주세요.', 'arena', '대결하기')
            elif self.top_tie():
                todo = ('상위 30% 안에서 점수가 같은 조합이 생겼습니다. 대결에서 먼저 가려 주세요.', 'arena', '대결하기')
            elif self._converged():
                step = 3  # evolution has run its course: the stepper and home move on to refine
                todo =(f'{CONVERGED_GENERATIONS}세대 연속으로 살아남은 진화 조합이 없습니다. 이제 다듬기로 넘어가 보세요.', 'refine', '다듬기 시작')
            else:
                todo = (f"상위 30% 조합을 섞어 {sel['generation']}세대 진화 조합을 만드세요.", 'evolution', '진화 시작')
        params = {'focus': 'connection' if not checklist['api_key'] else 'prompts'} if todo[1] == 'settings' else {}
        return {'step': step, 'names': STAGES, 'todo': todo[0], 'route': todo[1], 'action': todo[2], 'params': params,
                'checklist': checklist}

    def status(self, since=0):
        with self.lock:
            tie = self.boundary_tie() or self.top_tie()
            rated, waiting = self.ranking_counts()
            return {
                'stage': self.stage(), 'job': self.job_view(), 'events': self.events_since(since),
                'counts': {'active': len(self.combos), 'rated': rated, 'newcomers': waiting,
                           'excluded': len(self.retired), 'artists': len(self.state['artists']),
                           'generation': self.selection['generation'],
                           # ponytail: walks the whole history each poll (~0.2 ms per 4,000 votes); a counter if it ever shows
                           'votes': sum(1 for h in self.state['history'] if h.get('type') in
                                        ('arena', 'selection_vote', 'place_vote', 'improve_vote'))},
                'tie': bool(tie), 'subscription': self.subscription_view(),
                # Changes whenever anything /api/settings returns changes (a save here, a seed picked by a
                # generation, a count remembered by a start, a reset or import): the page refetches then.
                'settings_rev': zlib.crc32(json.dumps(self.settings_payload(), sort_keys=True).encode()),
                'improve': (self.state.get('improve') or {}).get('status'),
            }

