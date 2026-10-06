"""Pure logic for NAI Style Lab: NovelAI API, style strings, Elo, tiers,
breeding, and the ladder placement used by evolution selection and newcomer placement.

Most of this is carried over unchanged from v3.6 (recovered from the original by 브랙퍼스트).
"""
import bisect
import http.client
import io
import json
import math
import os
import random
import re
import shutil
import ssl
import time
import zipfile
from datetime import datetime
from pathlib import Path

NAI_GEN_HOST = 'image.novelai.net'
NAI_GEN_PATH = '/ai/generate-image'
MODELS = ['nai-diffusion-5-full', 'nai-diffusion-5-curated', 'nai-diffusion-4-5-full', 'nai-diffusion-4-5-curated', 'nai-diffusion-4-full']

SIZE_PRESETS = {'세로 832x1216': (832, 1216), '정방형 1024x1024': (1024, 1024), '가로 1216x832': (1216, 832)}

# What novelai.net itself sends (captured from the site, V5, no quality tags / UC preset, no Variety+), so an image
# imported there and generated again comes out the same. Variety+ (skip_cfg_above_sigma) alone changes the picture
# completely, and the site does not carry it over on import. Left out: how the site takes the reply (image_format
# webp, stream msgpack) and use_new_shared_trial (account billing), none of which changes the picture.
WEB_PARAMS = {'ucPresetId': 'none', 'qualityPresetId': 'none', 'autoSmea': False, 'dynamic_thresholding': False,
              'controlnet_strength': 1, 'legacy': False, 'add_original_image': True, 'legacy_v3_extend': False,
              'use_coords': False, 'normalize_reference_strength_multiple': True, 'inpaintImg2ImgStrength': 1,
              'straight_alpha': True, 'tag_hint_qt': 0, 'tag_hint_uc_preset': 0, 'deliberate_euler_ancestral_bug': False,
              'prefer_brownian': True, 'noise_schedule': 'karras'}

DEFAULT_NEGATIVE = 'lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, fewer digits, cropped, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark, blurry, artistic error, bad proportions, logo, artist logo, comic, manga panel, panel, speech bubble, cover art, magazine cover, collage, text focus, english text, japanese text, multiple views'

DEFAULT_BASE_PROMPT = '{artist}, 1girl, solo, standing, simple background, upper body'

# Name order as Windows Explorer sorts it (StrCmpLogicalW: case-insensitive, numbers by value).
DEFAULT_ARTIST_TAGS = [
    'artist:chamooi',
    'artist:channel_(caststation)',
    'artist:cura',
    'artist:for-u',
    'artist:healthyman',
    'artist:hwansang',
    'artist:ke-ta',
    'artist:mignon',
    'artist:mikan03_26',
    'artist:momoko_(momopoco)',
    'artist:muchi_maro',
    'artist:necomi',
    'artist:ningen_mame',
    'artist:omutatsu',
    'artist:onineko',
    'artist:quasarcake',
    'artist:rurudo',
    'artist:shigure_ui',
    'artist:shnva',
    'artist:supernew',
    'artist:torino_aqua',
    'artist:wanke',
    'artist:zain',
]


# Ratings are kept on 10x the usual Elo scale (start 10000, 4000 points = 10x odds, K 320). They stay whole
# numbers, yet ten times finer than on the 1000 scale, where rounding made many combos tie.
RATING_SCALE = 10
START_ELO = 1000 * RATING_SCALE
ELO_K = 32 * RATING_SCALE
PLACE_GAP = 48 * RATING_SCALE  # a newcomer placed below the lowest / above the highest rung

TIER_ORDER = ['S', 'A', 'B', 'C', 'D']

# A bell curve: S 10% · A 20% · B 40% · C 20% · D 10%. S and A (the top 30%) are the evolution parents.
TIER_CUTOFFS = {'S': 0.1, 'A': 0.3, 'B': 0.7, 'C': 0.9, 'D': 1.0}

TIER_MATCH_MULTIPLIER = {'S': 1.0, 'A': 0.9, 'B': 0.6, 'C': 0.3, 'D': 0.1}

SAMPLERS = ['k_euler_ancestral', 'k_euler', 'k_dpmpp_2s_ancestral', 'k_dpmpp_2m', 'k_dpmpp_sde']

ARTIST_TAG_RE = re.compile('artist:[^:,]+')

MAX_SEED = 4294967295
AUTH_ERROR_STATUSES = (401, 403)
RETRY_STATUSES = (429, 500, 502, 503, 504)
RETRY_DELAYS = (5, 15, 45)


class NaiHttpError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status

def nai_headers(api_key: str) -> dict:
    return {'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}', 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36', 'Referer': 'https://novelai.net/', 'Origin': 'https://novelai.net', 'Accept': '*/*', 'Accept-Language': 'en-US,en;q=0.9'}

def fetch_user_data(api_key: str):
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(NAI_GEN_HOST, context=ctx, timeout=10)
    try:
        conn.request('GET', '/user/subscription', headers=nai_headers(api_key.strip()))
        resp = conn.getresponse()
        if resp.status != 200:
            reason = {
                400: '요청이 거부되었습니다. API 주소와 키를 확인해 주세요.',
                401: 'API 키가 만료되었거나 올바르지 않습니다.',
                403: '구독 정보에 접근할 권한이 없습니다.',
                429: '요청이 너무 많습니다. 잠시 후 다시 조회해 주세요.',
            }.get(resp.status, 'NovelAI 서버가 요청을 처리하지 못했습니다.')
            raise RuntimeError(f'HTTP {resp.status}: {reason}')
        data = json.loads(resp.read().decode('utf-8'))
        if not isinstance(data, dict) or 'tier' not in data:
            raise ValueError('구독 정보를 읽지 못했습니다.')
        tier_num = data['tier']
        tier_names = {0: 'Free', 1: 'Tablet', 2: 'Scroll', 3: 'Opus'}
        tier_name = tier_names.get(tier_num, f'Tier {tier_num}')
        perks = data.get('perks', {})
        unlimited = perks.get('unlimitedImageGenerations', False) or tier_num == 3
        usage = data.get('usage') or {}
        percent = usage.get('percent') if isinstance(usage, dict) else None
        remaining = None
        if type(percent) in (int, float) and math.isfinite(percent):
            remaining = 0 if usage.get('isNegative') else max(0, percent)
        steps_left = data.get('trainingStepsLeft')
        anlas = None
        if isinstance(steps_left, dict):
            fixed = steps_left.get('fixedTrainingStepsLeft')
            purchased = steps_left.get('purchasedTrainingSteps')
            if type(fixed) is int and type(purchased) is int:
                anlas = fixed + purchased
        return {'tier': tier_name, 'tier_num': tier_num, 'unlimited': unlimited,
                'active': data.get('active', False), 'remaining_percent': remaining,
                'anlas': anlas}
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError('구독 응답 형식을 읽을 수 없습니다.') from exc
    except (OSError, http.client.HTTPException) as exc:
        raise RuntimeError('NovelAI에 연결하지 못했습니다. 네트워크 상태를 확인해 주세요.') from exc
    finally:
        conn.close()

def estimate_v5_images(percent, width, height, steps, model):
    if (percent is None or not model.startswith('nai-diffusion-5')
            or width <= 0 or height <= 0 or width * height > 1024 * 1024
            or not 1 <= steps <= 28):
        return None
    # Measured 2026-10-03 (Opus, V5 Full, 832x1216): a full bar is 1735 images at 23 steps, 1488 at 28.
    # Per-image cost is a fixed part plus a per-step part through those two points, times the pixel count
    # (NovelAI: cost scales with pixels). The API only reports whole percents, so "left" moves in ~15-image steps.
    per_step = (1 / 1488 - 1 / 1735) / (28 - 23)
    fixed = 1 / 1735 - 23 * per_step
    capacity = round(1 / ((fixed + per_step * steps) * (width * height) / (832 * 1216)), 6)  # 1487.99999 is 1488
    return math.floor(capacity * max(0, percent) / 100), math.floor(capacity)


def extract_png_from_zip(data: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                if name.lower().endswith('.png'):
                    return archive.read(name)
    except zipfile.BadZipFile:
        pass
    if data[:4] == b'\x89PNG':
        return data
    raise ValueError('응답에서 PNG를 추출하지 못했습니다.')

def character_prompt_parts(value: list[str]) -> list[str]:
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return [part.strip() for part in value if part.strip()]
    raise ValueError('캐릭터 프롬프트 형식이 올바르지 않습니다.')


def generate_style_image(api_key: str, base_prompt: str, character_prompt: list[str],
                         style_str: str, negative_prompt: str, model: str,
                         width: int, height: int, steps: int, cfg: float,
                         sampler: str, seed: int, cfg_rescale: float) -> bytes:
    base = base_prompt.strip()
    style = style_str.strip()
    if '{artist}' not in base:
        base_caption = ', '.join(p for p in (style, base) if p)
    elif style:
        base_caption = base.replace('{artist}', style)
    else:  # no artist tags (e.g. an empty free prompt): drop the slot without leaving a stray comma
        base_caption = ', '.join(p.strip() for p in base.replace('{artist}', '').split(',') if p.strip())
    if not base_caption:
        raise ValueError('프롬프트가 비어 있습니다.')
    # Every character, each once, in its own slot (as the site sends them: positions left to the AI).
    char_list = character_prompt_parts(character_prompt)
    center = {'x': 0.5, 'y': 0.5}
    parameters = {
        'params_version': 4, 'width': width, 'height': height, 'scale': cfg, 'sampler': sampler, 'steps': steps,
        'seed': seed, 'n_samples': 1, 'cfg_rescale': cfg_rescale, **WEB_PARAMS,
        'v4_prompt': {'caption': {'base_caption': base_caption,
                                  'char_captions': [{'char_caption': c, 'centers': [center]} for c in char_list]},
                      'use_coords': False, 'use_order': True},
        'v4_negative_prompt': {'caption': {'base_caption': negative_prompt,
                                           'char_captions': [{'char_caption': '', 'centers': [center]} for _ in char_list]}},
        'negative_prompt': negative_prompt,
        'characterPrompts': [{'prompt': c, 'uc': '', 'center': center, 'enabled': True} for c in char_list],
    }
    # input is the base prompt alone: the site imports it (the PNG's "prompt") as its base prompt, so with the
    # characters in it too they would come back twice, once there and once in their own slots.
    payload = {'input': base_caption, 'model': model, 'action': 'generate', 'parameters': parameters}
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(NAI_GEN_HOST, context=ctx, timeout=120)
    try:
        conn.request('POST', NAI_GEN_PATH, body=body, headers=nai_headers(api_key))
        resp = conn.getresponse()
        data = resp.read()
        status, reason = resp.status, resp.reason
    finally:
        conn.close()
    if status != 200:
        err_body = data[:500].decode('utf-8', errors='replace')
        raise NaiHttpError(status, f'HTTP {status} {reason}: {err_body}')
    return extract_png_from_zip(data)


def generate_for_settings(settings, style, seed, stop_event, log=None):
    """Generate one image, retrying rate limits and transient server errors."""
    for delay in (*RETRY_DELAYS, None):
        try:
            return generate_style_image(
                settings['api_key'], settings['base_prompt'], settings['character_prompt'],
                style, settings['negative'], settings['model'], settings['width'],
                settings['height'], settings['steps'], settings['cfg'], settings['sampler'],
                seed, cfg_rescale=settings['cfg_rescale'])
        except NaiHttpError as exc:
            if exc.status not in RETRY_STATUSES or delay is None:
                raise
            if log:
                log(f'HTTP {exc.status}: {delay}초 후 다시 시도합니다.')
            if stop_event.wait(delay) is True:
                raise

def default_artists():
    return [{'tag': t, 'count': 0, 'arena_matches': 0, 'arena_wins': 0} for t in DEFAULT_ARTIST_TAGS]

def sanitize_tag(raw: str):
    """``artist:`` + the name as NovelAI takes it: no backslash escapes, underscores for spaces.

    ``absolute (\\queen\\)`` / ``absolute \\(queen\\)`` -> ``artist:absolute_(queen)``.
    """
    name = raw.strip()
    name = name[len('artist:'):] if name.startswith('artist:') else name
    name = '_'.join(name.replace('\\', '').split())
    return f'artist:{name}' if name else None


def _looks_like_number(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False

# A NovelAI weight group, "1.2::tags::" (unclosed, it runs to the end); a negative one pushes its tags away.
WEIGHT_GROUP_RE = re.compile(r'(-?(?:\d+\.?\d*|\.\d+))::(.*?)(?:::|$)', re.S)
PROMPT_ARTIST_RE = re.compile(r'artist:[^:,{}\[\]|\n]+')


def artist_weights_in_prompt(text: str) -> list:
    """(weight, tag) for the artist: tags of a whole prompt and nothing else (no 1girl, no quality tags); the
    weight is its "w::...::" group's, 1 outside one. One in a negative group is left out: it is there to push that
    style away."""
    # ponytail: {} / [] emphasis (x1.05 per level) is not counted, the weight shows as its group's
    found, at = [], 0

    def take(chunk, weight):
        found.extend((weight, t.strip()) for t in PROMPT_ARTIST_RE.findall(chunk) if t.strip() != 'artist:')

    for group in WEIGHT_GROUP_RE.finditer(text):
        take(text[at:group.start()], 1.0)
        if float(group.group(1)) > 0:
            take(group.group(2), float(group.group(1)))
        at = group.end()
    take(text[at:], 1.0)
    return found


def artists_in_prompt(text: str) -> list:
    return [tag for _, tag in artist_weights_in_prompt(text)]


def extract_artist_names(raw: str) -> list:
    names = []
    for line in raw.split('\n'):
        for chunk in line.split(','):
            chunk = chunk.strip()
            if not chunk:
                continue
            for token in chunk.split('::'):
                token = token.strip()
                if not token or _looks_like_number(token):
                    continue
                names.append(token)
    return names

def artists_from_style(style_str: str) -> list:
    return [t.strip() for t in ARTIST_TAG_RE.findall(style_str)]

def calc_elo(rating_a, rating_b, a_won, k=ELO_K):
    ea = 1 / (1 + 10 ** ((rating_b - rating_a) / (400 * RATING_SCALE)))
    eb = 1 - ea
    new_a = round(rating_a + k * ((1 if a_won else 0) - ea))
    new_b = round(rating_b + k * ((0 if a_won else 1) - eb))
    return new_a, new_b

def confirm_matches(pool_size: int) -> int:
    """Matches a combo needs before its tier is settled (no longer shown with "?").

    Placement already finds a spot by binary search in about log2(pool) comparisons; a few more games on top
    settle it: 30 combos -> 8, 100 -> 10, 200 -> 11.
    """
    return round(math.log2(max(pool_size, 2))) + 3


def tier_confirmed(matches: int, pool_size: int) -> bool:
    return matches >= confirm_matches(pool_size)

def rank_among(elo, ascending_elos) -> int:
    """Place of ``elo`` among ``ascending_elos`` counted from the top (1 = best); equal Elo share the lower place.
    What tiers and match odds are computed from."""
    return len(ascending_elos) - bisect.bisect_left(ascending_elos, elo)


def tier_for_percentile(percentile: float) -> str:
    return next((letter for letter in TIER_ORDER if percentile <= TIER_CUTOFFS[letter]), TIER_ORDER[-1])


def tier_grade(percentile: float) -> str:
    """Tier with a +/- step for display: each tier's band split in thirds (S+ top third, S, S- bottom third).
    Only for showing; parents, filters and match odds use the plain tier."""
    tier = tier_for_percentile(percentile)
    index = TIER_ORDER.index(tier)
    low = TIER_CUTOFFS[TIER_ORDER[index - 1]] if index else 0.0
    third = (TIER_CUTOFFS[tier] - low) / 3
    position = percentile - low
    return tier + ('+' if position <= third + 1e-12 else '' if position <= 2 * third + 1e-12 else '-')


def is_rated(combo: dict) -> bool:
    """Has a meaningful Elo: played a regular match or was placed on the ladder."""
    return combo.get('matches', 0) >= 1 or bool(combo.get('placed'))


def rank_order(combos):
    """Best first: higher Elo; on equal Elo the better win rate, then more matches, then the older combo.
    The one order behind every #n the app shows."""
    return sorted(combos, key=lambda c: (-c['elo'], -(c['wins'] / c['matches'] if c['matches'] else 0),
                                         -c['matches'], c.get('created', 0), c['id']))


def top_tier_cut(rated_count: int) -> int:
    """How many rated combos fit in S/A (the top 30%), using the same percentile arithmetic as the tiers."""
    return next((k for k in range(rated_count, -1, -1) if rated_count and k / rated_count <= TIER_CUTOFFS['A']), 0)


def top_tier_ids(combinations, exclude=()):
    """The parents outside a batch: the rated combos currently in S/A, best first."""
    rated = sorted((c for c in combinations if is_rated(c) and c['id'] not in exclude),
                   key=lambda c: (c['elo'], c['id']), reverse=True)
    return [c['id'] for c in rated[:top_tier_cut(len(rated))]]


def boundary_tie_pair(combinations, exclude=(), skipped=()):
    """Two combos whose equal Elo straddles the top-30% boundary, or None.

    ``skipped`` holds ``(frozenset(pair), elo)`` keys the user already called even.
    """
    rated = sorted((c for c in combinations if is_rated(c) and c['id'] not in exclude),
                   key=lambda c: c['elo'], reverse=True)
    cut = top_tier_cut(len(rated))
    if not 0 < cut < len(rated) or rated[cut - 1]['elo'] != rated[cut]['elo']:
        return None
    elo = rated[cut]['elo']
    tied = [c['id'] for c in rated if c['elo'] == elo]
    return next(((x, y) for i, x in enumerate(tied) for y in tied[i + 1:]
                 if (frozenset((x, y)), elo) not in skipped), None)


def top_tie_pair(combinations, exclude=(), skipped=()):
    """Two combos inside the top 30% with the same Elo, or None (the boundary tie is boundary_tie_pair's).

    Same ``skipped`` keys as boundary_tie_pair; the best-ranked tie comes first.
    """
    rated = sorted((c for c in combinations if is_rated(c) and c['id'] not in exclude),
                   key=lambda c: c['elo'], reverse=True)
    top = rated[:top_tier_cut(len(rated))]
    return next(((x['id'], y['id']) for i, x in enumerate(top) for y in top[i + 1:]
                 if x['elo'] == y['elo'] and (frozenset((x['id'], y['id'])), x['elo']) not in skipped), None)


def parse_style_combo(style_str: str) -> list:
    items = []
    for chunk in style_str.split(','):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split('::')
        if len(parts) >= 2:
            try:
                w = float(parts[0])
            except ValueError:
                w = 1.0
            tag = parts[1].strip()
            if tag:
                items.append((w, tag))
        elif chunk.startswith('artist:'):
            items.append((1.0, chunk))
    return items

# NovelAI weighting: ``1.5::red scarf ::`` — the closing ``::`` always follows a space.
def weighted(weight: float, tag: str) -> str:
    return f'{weight:.1f}::{tag} ::'


def rename_artist_in_style(style_str: str, old_tag: str, new_tag: str) -> str:
    """Rename one weighted tag (``w::tag ::``) without touching others."""
    return re.sub(r'(?<=::)' + re.escape(old_tag) + r' ?(?=::)',
                  lambda _: new_tag + ' ', style_str)


def style_from_pairs(pairs):
    return ', '.join(weighted(weight, tag) for weight, tag in pairs)


def clamp_weight(weight: float, min_w: float, max_w: float) -> float:
    """Weights are written with one decimal, so clamp on that grid to never print outside the range."""
    low, high = math.ceil(min_w * 10 - 1e-9) / 10, math.floor(max_w * 10 + 1e-9) / 10
    return min(high, max(low, round(weight, 1)))


BREED_MODES = ('mutate', 'crossover', 'random')
BREED_WEIGHTS = (1, 1, 1)  # a third each: crossover mixes lineages, random keeps looking elsewhere
FAVOURITE_BOOST = 2        # the best-scoring artist is drawn this many times as often as an average one
TOP_PARENT_BOOST = 2       # the best parent is picked this many times as often as the lowest one
MUTATE_WEIGHT_JITTER = 0.3


def artist_preference(scores, artist_tags):
    """{tag: draw weight} from artist_scores' {tag: score}: FAVOURITE_BOOST ** (score / best |score|).

    The best artist gets FAVOURITE_BOOST (2x), one scored as far below average gets its inverse (0.5x),
    an artist with no evidence (score 0) stays at 1, so it keeps being tried.
    """
    top = max((abs(scores.get(tag, 0)) for tag in artist_tags), default=0)
    return {tag: FAVOURITE_BOOST ** (scores.get(tag, 0) / top) if top else 1.0 for tag in artist_tags}


def pick_parents(ranked):
    """Two different parents from ``ranked`` (best first); the best is TOP_PARENT_BOOST times as likely as the
    lowest, falling off in a straight line, so every parent keeps a fair chance."""
    n = len(ranked)
    weights = [TOP_PARENT_BOOST - (TOP_PARENT_BOOST - 1) * i / max(n - 1, 1) for i in range(n)]
    first = random.choices(range(n), weights=weights)[0]
    rest = [i for i in range(n) if i != first]
    second = random.choices(rest, weights=[weights[i] for i in rest])[0]
    return ranked[first], ranked[second]


def breed_weighted_style(style_a, style_b, artist_tags, min_tags, max_tags,
                         min_w, max_w, mode='mutate', parent_counts=None, preference=None):
    """Create a child that always stays inside the configured artist count and weight range.

    mutate: parent A with 1-3 artists replaced/removed/added and inherited weights moved by up to
    ±0.3; crossover: artists (with their weights) drawn from both parents; random: a fresh combo.
    Newly drawn artists favour those that few current parents use (``parent_counts``), so one
    popular artist does not take over every lineage, times the user's taste (``preference``,
    from artist_preference). Inherited weights and artist counts outside
    the current settings are pulled back inside them.
    """
    first, second = parse_style_combo(style_a), parse_style_combo(style_b)
    if not first or not second or len(artist_tags) < min_tags:
        raise ValueError('자손 생성에 필요한 부모 또는 작가가 부족합니다.')
    max_tags = min(max_tags, len(artist_tags))
    counts = parent_counts or {}
    liked = preference or {}

    def clamp(weight):
        return clamp_weight(weight, min_w, max_w)

    def fresh(used):
        pool = [tag for tag in artist_tags if tag not in used]
        return random.choices(pool, weights=[liked.get(tag, 1.0) / (1 + counts.get(tag, 0)) for tag in pool])[0]

    if mode == 'random':
        pairs = []
    elif mode == 'crossover':
        inherited = {tag: weight for weight, tag in first}
        inherited.update({tag: weight for weight, tag in second if tag not in inherited})
        pairs = [(clamp(weight), tag) for tag, weight in inherited.items()]
        random.shuffle(pairs)
        pairs = pairs[:random.randint(min_tags, max_tags)]
    else:
        pairs = [(clamp(weight + random.uniform(-MUTATE_WEIGHT_JITTER, MUTATE_WEIGHT_JITTER)), tag)
                 for weight, tag in first]
        random.shuffle(pairs)
        pairs = pairs[:max_tags]
        for _ in range(random.randint(1, 3)):
            used = {tag for _, tag in pairs}
            can_draw = len(used) < len(artist_tags)
            actions = (['replace'] if can_draw and pairs else []) + (['remove'] if len(pairs) > min_tags else []) \
                + (['add'] if can_draw and len(pairs) < max_tags else [])
            if not actions:
                break
            action = random.choice(actions)
            if action == 'replace':
                pairs[random.randrange(len(pairs))] = (clamp(random.uniform(min_w, max_w)), fresh(used))
            elif action == 'remove':
                pairs.pop(random.randrange(len(pairs)))
            else:
                pairs.append((clamp(random.uniform(min_w, max_w)), fresh(used)))
    target = random.randint(min_tags, max_tags) if mode == 'random' else min_tags
    used = {tag for _, tag in pairs}
    while len(pairs) < target:
        tag = fresh(used)
        used.add(tag)
        pairs.append((clamp(random.uniform(min_w, max_w)), tag))
    return style_from_pairs(pairs)


def _batch_votes(selection, history):
    """This batch's entries only (newest first, like history), so ranking never walks every vote ever cast."""
    batch_id = selection.get('batch_id')
    return [vote for vote in history if batch_id and vote.get('batch_id') == batch_id]


def selection_scores(selection, history):
    """Rebuild this batch's scores from votes; parent scores never change."""
    scores = {key: float(value) for key, value in selection.get('parent_scores', {}).items()}
    if not scores:
        return {}, {}
    cutoff = min(scores.values())
    children = set(selection.get('candidate_ids', []))
    scores.update({child_id: cutoff for child_id in children})
    counts = {child_id: 0 for child_id in children}
    for vote in reversed(history):
        if vote.get('type') != 'selection_vote' or vote.get('batch_id') != selection.get('batch_id'):
            continue
        pair = vote.get('pair') or {}
        a_id, b_id = pair.get('a'), pair.get('b')
        if a_id not in scores or b_id not in scores or vote.get('winner_id') not in (a_id, b_id):
            continue
        new_a, new_b = calc_elo(scores[a_id], scores[b_id], vote['winner_id'] == a_id, k=48 * RATING_SCALE)
        if a_id in children:
            scores[a_id] = new_a
            counts[a_id] += 1
        if b_id in children:
            scores[b_id] = new_b
            counts[b_id] += 1
    return scores, counts


def apply_selection_ratings(combinations, selection, history):
    """Mirror this batch into each child's list Elo/record: Elo becomes its placed ladder score.

    Idempotent: each child remembers what it already received (``selection_applied``),
    so only the difference is applied. Regular-league changes made meanwhile are kept.
    """
    batch_id = selection.get('batch_id')
    children = set(selection.get('candidate_ids', []))
    if not batch_id or not children:
        return
    ranking = selection_ranking(selection, history)
    scores, counts = ranking['scores'], ranking['counts']
    wins = {key: 0 for key in children}
    for vote in _batch_votes(selection, history):
        if vote.get('type') == 'selection_vote' and vote.get('winner_id') in wins:
            wins[vote['winner_id']] += 1
    for combo in combinations:
        key = combo['id']
        if key not in children:
            continue
        voted = counts.get(key, 0)
        judged = bool(ranking['info'][key]['votes'])  # "비슷함" places a child too, it just is not a win or loss
        target = {'elo': round(scores[key]) - START_ELO if judged else 0,
                  'matches': voted, 'wins': wins[key] if voted else 0}
        applied = combo.get('selection_applied') or {}
        if applied.get('batch_id') != batch_id:
            applied = {}
        for field, value in target.items():
            combo[field] += value - applied.get(field, 0)
        combo['selection_applied'] = {'batch_id': batch_id, **target}


SELECTION_EXTRA_VOTES = 4  # verification/ambiguity votes a child may get beyond its bisection
SELECTION_CUT_WINDOW = 3   # children ranked this close to the promotion cut get verified


def selection_votes(selection, history):
    """Return the parent ladder (lowest score first) and each child's votes against it.

    Votes are ``(ladder_index, 'win' | 'loss' | 'tie')`` in the order they were cast;
    a skip ("무승부 / 건너뛰기") counts as a tie: the child sits right next to that parent.
    """
    frozen = selection.get('parent_scores', {})
    ladder = sorted((key for key in selection.get('parent_ids', []) if key in frozen),
                    key=lambda key: (frozen[key], key))
    rung = {key: index for index, key in enumerate(ladder)}
    batch_id = selection.get('batch_id')
    votes = {child: [] for child in selection.get('candidate_ids', [])}
    for vote in reversed(history):  # history is newest-first
        if vote.get('batch_id') != batch_id or vote.get('type') not in ('selection_vote', 'selection_skip'):
            continue
        pair = vote.get('pair') or {}
        a_id, b_id = pair.get('a'), pair.get('b')
        if a_id in votes and b_id in rung:
            child, parent = a_id, b_id
        elif b_id in votes and a_id in rung:
            child, parent = b_id, a_id
        else:
            continue
        if vote['type'] == 'selection_skip':
            result = 'tie'
        elif vote.get('winner_id') in (child, parent):
            result = 'win' if vote['winner_id'] == child else 'loss'
        else:
            continue
        votes[child].append((rung[parent], result))
    return ladder, votes


def _bisect(votes, n, climb_ties=False):
    """Slot bounds from the bisection path; slot = number of parents the child ranks above.

    A tie settles the slot next to that parent, unless ``climb_ties``: then the child is at least
    level with it and goes on to face the parent just above (see ``_climb_rung``).
    """
    lo, hi = 0, n
    for k, result in votes:
        if not lo <= k < hi:
            continue  # off-path votes (verification) are judged by _best_slots instead
        if result == 'win':
            lo = k + 1
        elif result == 'loss':
            hi = k
        elif climb_ties:
            lo = k
        else:
            lo, hi = k, k + 1
    return lo, hi


def _climb_rung(votes, hi, tried):
    """After a tie with parent k, the parent just above (k + 1) is asked next, if still open."""
    if votes and votes[-1][1] == 'tie':
        above = votes[-1][0] + 1
        if above < hi and above not in tried:
            return above
    return None


def _best_slots(votes, n):
    """Slots that contradict the fewest of the child's votes (preferences need not be transitive)."""
    def violations(slot):
        return sum(1 for k, result in votes
                   if (result == 'win' and slot <= k) or (result == 'loss' and slot > k)
                   or (result == 'tie' and slot not in (k, k + 1)))
    costs = [violations(slot) for slot in range(n + 1)]
    return [slot for slot, cost in enumerate(costs) if cost == min(costs)]


def _next_rung(lo, hi, tried):
    """Lowest parent first (most children stop there), then bisect the remaining range."""
    open_rungs = [k for k in range(lo, hi) if k not in tried]
    if not open_rungs:
        return None
    target = lo if lo == 0 else (lo + hi) // 2
    return min(open_rungs, key=lambda k: (abs(k - target), k))


def _check_rung(best, slot, tried, n):
    """A parent to re-ask about: inside an ambiguous range first, then 2-3 rungs above and below."""
    if len(best) > 1:
        open_rungs = [k for k in range(best[0], best[-1]) if k not in tried]
        if open_rungs:
            return open_rungs[len(open_rungs) // 2]
    for window in ([k for k in (slot + 1, slot + 2) if k < n], [k for k in (slot - 2, slot - 3) if k >= 0]):
        if window and not tried.intersection(window):
            return window[0]
    return None


def selection_ranking(selection, history):
    """Rank parents and children together and decide which vote, if any, is still needed.

    Returns a dict with ``ladder``, ``info`` (per child: ``slot``, ``rung`` = parent index it
    should face next or None, ``votes``), ``ascending`` (all ids, lowest first), ``scores``
    (order-preserving ladder scores), ``counts`` (decisive votes per child) and ``boundary_pairs``
    (unplayed child pairs sharing the slot that straddles the promotion cut).
    """
    history = _batch_votes(selection, history)
    ladder, votes = selection_votes(selection, history)
    n = len(ladder)
    elo, counts = selection_scores(selection, history)
    info = {}
    for child, child_votes in votes.items():
        tried = {k for k, _ in child_votes}
        lo, hi = _bisect(child_votes, n, climb_ties=True)
        rung = _climb_rung(child_votes, hi, tried)
        if rung is None:
            rung = _next_rung(lo, hi, tried)
        best = [lo] if rung is not None else _best_slots(child_votes, n)
        info[child] = dict(slot=best[(len(best) - 1) // 2], best=best, rung=rung, bisecting=rung is not None,
                           lo=lo, hi=hi, votes=child_votes, tried=tried)

    batch_id = selection.get('batch_id')
    head_to_head, played = {}, set()
    for vote in history:
        pair = vote.get('pair') or {}
        a_id, b_id = pair.get('a'), pair.get('b')
        if vote.get('batch_id') != batch_id or a_id not in info or b_id not in info:
            continue
        played.add(frozenset((a_id, b_id)))
        if vote.get('type') == 'selection_vote' and vote.get('winner_id') in (a_id, b_id):
            head_to_head[vote['winner_id']] = head_to_head.get(vote['winner_id'], 0) + 1

    def arrange():
        slots = {}
        for child, child_info in info.items():
            slots.setdefault(child_info['slot'], []).append(child)
        ordered = []
        for slot in range(n + 1):
            ordered += sorted(slots.get(slot, []), key=lambda key: (head_to_head.get(key, 0), elo.get(key, 0), key))
            if slot < n:
                ordered.append(ladder[slot])
        return slots, ordered

    slots, ascending = arrange()
    if not any(child_info['bisecting'] for child_info in info.values()):
        # Verify only where it can change who is promoted; children stopped by the lowest parent stay at one vote.
        cap = math.ceil(math.log2(n + 1)) + 1 + SELECTION_EXTRA_VOTES
        rank = {key: index for index, key in enumerate(reversed(ascending), 1)}
        for child, child_info in info.items():
            if (child_info['slot'] > 0 and len(child_info['votes']) < cap
                    and n - SELECTION_CUT_WINDOW < rank[child] <= n + SELECTION_CUT_WINDOW):
                child_info['rung'] = _check_rung(child_info['best'], child_info['slot'], child_info['tried'], n)

    frozen = selection.get('parent_scores', {})
    scores = {key: frozen[key] for key in ladder}
    for slot, mates in slots.items():
        # Children sharing a slot differ only where head-to-head votes separated them;
        # otherwise they share the slot's midpoint instead of an arbitrary spread.
        levels = sorted({head_to_head.get(key, 0) for key in mates})
        if ladder:
            low = frozen[ladder[slot - 1]] if slot else frozen[ladder[0]] - PLACE_GAP
            high = frozen[ladder[slot]] if slot < n else frozen[ladder[-1]] + PLACE_GAP // 2 * (len(levels) + 1)
        else:
            low, high = START_ELO, START_ELO + PLACE_GAP // 2 * (len(levels) + 1)
        for key in mates:
            level = levels.index(head_to_head.get(key, 0)) + 1
            scores[key] = round(low + level * (high - low) / (len(levels) + 1))

    boundary_pairs = []
    cut = len(ascending) - n
    if not any(child_info['rung'] is not None for child_info in info.values()) and 0 < cut < len(ascending):
        below, above = ascending[cut - 1], ascending[cut]
        if below in info and above in info and info[below]['slot'] == info[above]['slot']:
            group = slots[info[below]['slot']]
            # ponytail: full round-robin inside the cut slot; fine for the few children that tie there.
            boundary_pairs = [(x, y) for i, x in enumerate(group) for y in group[i + 1:]
                              if frozenset((x, y)) not in played]
    return dict(ladder=ladder, info=info, ascending=ascending, scores=scores, counts=counts,
                boundary_pairs=boundary_pairs)


def selection_next_pair(selection, combinations, history, ranking=None):
    """Next vote: bisect unsettled children, verify those near the cut, then order cut ties.
    ``ranking``: selection_ranking's result, when the caller already has it."""
    by_id = {combo['id']: combo for combo in combinations}
    ranking = ranking or selection_ranking(selection, history)
    ladder = ranking['ladder']
    waiting = [(len(child_info['votes']), child, child_info['rung'])
               for child, child_info in ranking['info'].items()
               if child_info['rung'] is not None and child in by_id and ladder[child_info['rung']] in by_id]
    if waiting:
        _, child, k = min(waiting)
        return by_id[child], by_id[ladder[k]]
    for a_id, b_id in ranking['boundary_pairs']:
        if a_id in by_id and b_id in by_id:
            return by_id[a_id], by_id[b_id]
    return None


def selection_remaining_matches(selection, combinations, history):
    """Estimate remaining votes: bisection left, up to two checks per child, plus cut ties."""
    ranking = selection_ranking(selection, history)
    if not selection_next_pair(selection, combinations, history, ranking):
        return 0, 0
    lower = upper = len(ranking['boundary_pairs'])
    for child_info in ranking['info'].values():
        if child_info['bisecting']:
            lower += 1
            upper += math.ceil(math.log2(child_info['hi'] - child_info['lo'] + 1)) + (child_info['lo'] == 0) + 2
        elif child_info['rung'] is not None:
            lower += 1
            upper += 2
    return max(1, lower), max(1, lower, upper)


# ---------------------------------------------------------------- newcomer placement
# A new combo is binary-searched into the ladder of rated combos (lowest Elo first).
# Unlike evolution selection there is no "gate" at the bottom: start from the middle.

def placement_state(votes, ladder_size):
    """Where a newcomer stands after its votes against ladder rungs.

    ``votes`` are ``(rung_index, 'win' | 'loss' | 'tie')``. Returns ``(slot, next_rung)``;
    ``next_rung`` is None once the slot is settled. slot = number of rungs it ranks above.
    """
    lo, hi = _bisect(votes, ladder_size, climb_ties=True)
    tried = {k for k, _ in votes}
    climb = _climb_rung(votes, hi, tried)  # "비슷함" with rung k: ask the one just above next
    if climb is not None:
        return lo, climb
    open_rungs = [k for k in range(lo, hi) if k not in tried]
    if open_rungs:
        middle = (lo + hi) // 2
        return lo, min(open_rungs, key=lambda k: (abs(k - middle), k))
    best = _best_slots(votes, ladder_size)
    return best[(len(best) - 1) // 2], None


def placement_elo(slot, ladder_scores):
    """Elo for a newcomer placed at ``slot`` among ascending ``ladder_scores``."""
    if not ladder_scores:
        return START_ELO
    if slot <= 0:
        return round(ladder_scores[0] - PLACE_GAP)
    if slot >= len(ladder_scores):
        return round(ladder_scores[-1] + PLACE_GAP)
    return round((ladder_scores[slot - 1] + ladder_scores[slot]) / 2)


# ---------------------------------------------------------------- artist scores

ARTIST_SCORE_PRIOR = 2.0  # combos' worth of evidence (weight 1 each) an artist needs before its score moves far from 0


def artist_scores(combos, sweeps=60):
    """Each artist's share of the combo ratings: {tag: (score, combos it is in)}.

    Fits rating - average ≈ Σ weight × artist score over the rated combos (ridge regression by coordinate
    descent). So artists that often appear together are told apart, a weight of 1.8 counts for more than 0.5,
    and every kind of vote counts (it is all in the combo ratings). An artist with little evidence stays near
    0, the average; the prior pulls it there like ARTIST_SCORE_PRIOR average combos would.
    """
    rows = [(c['elo'], parse_style_combo(c['style'])) for c in combos if is_rated(c)]
    if not rows:
        return {}
    mean = sum(elo for elo, _ in rows) / len(rows)
    residual = [elo - mean for elo, _ in rows]
    uses = {}  # tag -> [(row, weight)]
    for row, (_, pairs) in enumerate(rows):
        for weight, tag in pairs:
            uses.setdefault(tag, []).append((row, weight))
    score = dict.fromkeys(uses, 0.0)
    for _ in range(sweeps):
        for tag, hits in uses.items():
            old = score[tag]
            new = sum(w * (residual[row] + w * old) for row, w in hits) / (sum(w * w for _, w in hits) + ARTIST_SCORE_PRIOR)
            for row, w in hits:
                residual[row] -= w * (new - old)
            score[tag] = new
    return {tag: (round(score[tag]), len(hits)) for tag, hits in uses.items()}


# ---------------------------------------------------------------- improve workbench

def jitter_weights(pairs, jitter, min_w, max_w, tries=50):
    """Same artists, new weights: every weight moves by up to ±jitter (at least one changes)."""
    for _ in range(tries):
        moved = [(clamp_weight(weight + random.uniform(-jitter, jitter), min_w, max_w), tag)
                 for weight, tag in pairs]
        if any(new != old for (new, _), (old, _) in zip(moved, pairs)):
            return moved
    index = random.randrange(len(pairs))  # tiny jitter / narrow range: force one visible step
    weight, tag = pairs[index]
    step = 0.1 if clamp_weight(weight + 0.1, min_w, max_w) != weight else -0.1
    return pairs[:index] + [(clamp_weight(weight + step, min_w, max_w), tag)] + pairs[index + 1:]


# ---------------------------------------------------------------- storage

# The state.json format. Whenever a release changes it: raise SCHEMA and add UPGRADES[old], which turns a format-old
# state into format old + 1 in place. Data from any older release goes through every step in order.
SCHEMA = 1
UPGRADES = {}


class NewerDataError(Exception):
    """The data was saved by a newer release (a newer format): this one must not open it, or it would lose
    what it does not know."""


def upgrade_state(data: dict) -> int:
    """Bring a loaded state up to SCHEMA, in place; returns the format it had. Data without a format number is
    format 1 (saved before formats were numbered)."""
    schema = data.get('schema', 1)
    if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
        raise ValueError(f'unknown data format: {schema!r}')
    if schema > SCHEMA:
        raise NewerDataError(schema)
    for step in range(schema, SCHEMA):
        UPGRADES[step](data)
    data['schema'] = SCHEMA
    return schema


def empty_state():
    return {'schema': SCHEMA, 'artists': default_artists(), 'combinations': [], 'history': [], 'ui_state': {},
            'selection': {'generation': 1, 'candidate_ids': [], 'batch_id': None, 'retired': [],
                          'parent_ids': [], 'parent_scores': {}, 'log': []},
            'placement': None, 'improve': None, 'free_results': []}


def load_state(state_file: Path) -> dict:
    """Read state.json; an unreadable file is set aside (never overwritten) and reported.

    Data in an older format is kept as it was (state.schemaN-date.json), then upgraded and saved. Data in a newer
    format raises NewerDataError and is left untouched."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    if not state_file.exists():
        return empty_state()
    try:
        data = json.loads(state_file.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('not a state')
        schema = data.get('schema', 1)
        if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
            raise ValueError(f'unknown data format: {schema!r}')
        if schema > SCHEMA:
            raise NewerDataError(schema)
    except NewerDataError:
        raise
    except Exception:
        corrupt = state_file.with_name(f"state.corrupt-{datetime.now():%Y%m%d_%H%M%S}.json")
        state_file.replace(corrupt)
        return {**empty_state(), 'load_error': str(corrupt)}
    # One copy per launch of the last state that loaded fine: a way back if something goes wrong this run.
    shutil.copyfile(state_file, backup_file(state_file))
    if schema < SCHEMA:
        before = state_file.with_name(f'state.schema{schema}-{datetime.now():%Y%m%d_%H%M%S}.json')
        shutil.copyfile(state_file, before)  # kept for good: the way back to the release that wrote it
        upgrade_state(data)
        write_state(state_file, data)
    data['schema'] = SCHEMA
    return data


def backup_file(state_file: Path) -> Path:
    return state_file.with_name(state_file.name + '.bak')


def write_state(state_file: Path, data: dict):
    """Atomically replace the state file so a crash never leaves half-written JSON."""
    text = json.dumps(data, ensure_ascii=False)
    tmp = state_file.with_name(state_file.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())  # on disk before the swap, so a power cut cannot leave an empty state.json
    # Antivirus or indexers can hold a just-written file for a moment on Windows; retry briefly.
    for attempt in range(20):
        try:
            os.replace(tmp, state_file)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05 * (attempt + 1))
