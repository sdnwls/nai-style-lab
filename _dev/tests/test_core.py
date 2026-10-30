"""Pure-logic checks: breeding ranges, placement, selection ladder, ties, weights, storage."""
import collections
import json
import math
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'app'))

import core  # noqa: E402


def main():
    random.seed(5)
    artists = [f'artist:{i}' for i in range(30)]

    # ---- breeding never leaves the configured ranges, even from parents made under older settings
    wide = core.style_from_pairs([(2.5 if i % 2 else 0.2, f'artist:{i}') for i in range(10)])
    narrow = core.style_from_pairs([(1.0, f'artist:{i}') for i in range(20, 24)])
    for _ in range(300):
        for mode in core.BREED_MODES:
            pairs = core.parse_style_combo(core.breed_weighted_style(wide, narrow, artists, 4, 8, 0.5, 2.0, mode))
            assert 4 <= len(pairs) <= 8 and all(0.5 <= w <= 2.0 for w, _ in pairs), (mode, pairs)
            assert len({t for _, t in pairs}) == len(pairs)
            odd = core.parse_style_combo(core.breed_weighted_style(wide, narrow, artists, 4, 8, 0.55, 1.95, mode))
            assert all(0.55 <= w <= 1.95 for w, _ in odd), (mode, odd)
    drawn = [t for _ in range(400) for _, t in core.parse_style_combo(core.breed_weighted_style(
        narrow, narrow, artists, 4, 4, 0.5, 1.5, 'random', {'artist:0': 19}))]
    assert drawn.count('artist:0') * 5 < drawn.count('artist:1'), 'popular parent artists are drawn less often'

    # ---- weight-only variants keep the artists and stay within ±jitter and the range
    base = [(1.0, 'artist:a'), (1.4, 'artist:b'), (0.6, 'artist:c')]
    for jitter in (0.3, 0.2, 0.1):
        for _ in range(200):
            moved = core.jitter_weights(base, jitter, 0.5, 1.5)
            assert [t for _, t in moved] == [t for _, t in base]
            assert any(w != bw for (w, _), (bw, _) in zip(moved, base)), 'a variant always differs'
            assert all(abs(w - bw) <= jitter + 1e-9 and 0.5 <= w <= 1.5 for (w, _), (bw, _) in zip(moved, base))
    assert core.jitter_weights([(1.5, 'artist:a')], 0.01, 0.5, 1.5) == [(1.4, 'artist:a')], 'tiny jitter still moves a visible step'

    # ---- an artist's own weight range replaces the common one, wider or narrower, in breeding and jitter
    own = {'artist:0': (0.2, 0.4), 'artist:1': (2.0, 2.5)}
    for _ in range(300):
        for mode in core.BREED_MODES:
            pairs = core.parse_style_combo(core.breed_weighted_style(wide, narrow, artists, 4, 8, 0.5, 1.5, mode, ranges=own))
            assert all(own.get(t, (0.5, 1.5))[0] <= w <= own.get(t, (0.5, 1.5))[1] for w, t in pairs), (mode, pairs)
        moved = core.jitter_weights([(1.0, 'artist:0'), (1.0, 'artist:1'), (1.0, 'artist:2')], 0.3, 0.5, 1.5, ranges=own)
        assert moved[0][0] <= 0.4 and moved[1][0] >= 2.0 and 0.7 <= moved[2][0] <= 1.3, moved

    # ---- newcomer placement: exact slot on a 100-rung ladder in at most ceil(log2(101)) votes
    ladder = list(range(100))
    for truth in (0, 1, 37, 50, 99, 100):
        votes = []
        while True:
            slot, rung = core.placement_state(votes, len(ladder))
            if rung is None:
                break
            votes.append((rung, 'win' if truth > rung else 'loss'))
        assert slot == truth and len(votes) <= math.ceil(math.log2(101)), (truth, slot, len(votes))
    slot, rung = core.placement_state([(50, 'tie')], 100)
    assert rung == 51, '"비슷함" with a rung asks the rung just above next'
    slot, rung = core.placement_state([(50, 'tie'), (51, 'loss')], 100)
    assert rung is None and slot in (50, 51), 'level with 50, below 51: settles right there'
    slot, rung = core.placement_state([(50, 'tie'), (51, 'win')], 100)
    assert rung is not None and rung > 51, 'beating the rung above keeps searching upward'
    slot, rung = core.placement_state([(99, 'tie')], 100)
    assert rung is None and slot in (99, 100), 'a tie with the top rung has nothing above to ask'
    assert core.placement_elo(0, [10000, 11000]) == 9520 and core.placement_elo(1, [10000, 11000]) == 10500
    assert core.placement_elo(2, [10000, 11000]) == 11480 and core.placement_elo(0, []) == core.START_ELO == 10000

    # ---- evolution selection: children land exactly between parents; failures stop at the gate
    parents = {f'p{i}': 1000 + 10 * i for i in range(20)}
    children = [f'c{i}' for i in range(12)]
    taste = dict(parents)
    taste.update({c: random.uniform(990, 1200) for c in children})
    selection = {'parent_ids': list(parents), 'parent_scores': parents, 'candidate_ids': children, 'batch_id': 'b'}
    combos = [{'id': k, 'elo': v} for k, v in taste.items()]
    history = []
    while (pair := core.selection_next_pair(selection, combos, history)):
        a, b = pair[0]['id'], pair[1]['id']
        history.insert(0, {'type': 'selection_vote', 'batch_id': 'b', 'pair': {'a': a, 'b': b},
                           'winner_id': a if taste[a] > taste[b] else b})
        assert len(history) < 400
    ranking = core.selection_ranking(selection, history)
    ladder_ids = ranking['ladder']
    for child in children:
        assert ranking['info'][child]['slot'] == sum(taste[p] < taste[child] for p in ladder_ids), child
    gate_only = [c for c in children if taste[c] < parents['p0']]
    for child in gate_only:
        assert len(ranking['info'][child]['votes']) == 1, 'a child below the lowest parent costs one vote'

    # ---- "비슷함" with a parent climbs to the parent just above instead of ending the child at the bottom
    parents = {f'q{i}': 10000 + 1000 * i for i in range(6)}  # q0 10000 … q5 15000
    selection = {'parent_ids': list(parents), 'parent_scores': parents, 'candidate_ids': ['kid'], 'batch_id': 't'}
    combos = [{'id': k, 'elo': v, 'matches': 0, 'wins': 0} for k, v in {**parents, 'kid': 10000}.items()]
    history, faced = [], []
    for answer in ('tie', 'tie', 'loss'):
        kid, parent = core.selection_next_pair(selection, combos, history)
        faced.append(parent['id'])
        entry = {'type': 'selection_skip' if answer == 'tie' else 'selection_vote', 'batch_id': 't',
                 'pair': {'a': kid['id'], 'b': parent['id']}}
        if answer == 'loss':
            entry['winner_id'] = parent['id']
        history.insert(0, entry)
    assert faced == ['q0', 'q1', 'q2'], faced
    assert core.selection_next_pair(selection, combos, history) is None
    kid_info = core.selection_ranking(selection, history)['info']['kid']
    assert kid_info['slot'] in (1, 2), kid_info  # level with q1, below q2
    core.apply_selection_ratings(combos, selection, history)
    kid = next(c for c in combos if c['id'] == 'kid')
    assert 10000 < kid['elo'] < 12000, 'a child placed by ties gets its ladder score, not the untouched start'
    only_ties = {**selection, 'candidate_ids': ['solo']}
    top_tie = [{'type': 'selection_skip', 'batch_id': 't', 'pair': {'a': 'solo', 'b': f'q{i}'}} for i in reversed(range(6))]
    assert core.selection_next_pair(only_ties, combos + [{'id': 'solo', 'elo': 10000}], top_tie) is None, \
        'tied with every parent up to the top: settled'

    # ---- contradictory taste lands where the fewest votes disagree
    fickle = [(0, 'win'), (10, 'win'), (15, 'loss'), (12, 'win'), (13, 'loss'), (14, 'win'), (11, 'loss')]
    assert core._best_slots(fickle, 20) == [11, 13, 15]

    # ---- top 30% (S·A) and its boundary tie
    assert [core.top_tier_cut(n) for n in (4, 5, 10, 76, 100)] == [1, 1, 3, 22, 30]
    assert [core.tier_for_percentile(p) for p in (0.1, 0.11, 0.3, 0.31, 0.7, 0.71, 0.9, 0.91, 1.0)] ==         ['S', 'A', 'A', 'B', 'B', 'C', 'C', 'D', 'D'], 'S 10 · A 20 · B 40 · C 20 · D 10'
    assert [core.tier_grade(p) for p in (0.03, 0.05, 0.1, 0.12, 0.2, 0.3, 0.5, 0.95, 1.0)] ==         ['S+', 'S', 'S-', 'A+', 'A', 'A-', 'B', 'D', 'D-'], 'each tier split in thirds'
    pool = [{'id': f'x{i}', 'elo': e, 'matches': 3} for i, e in enumerate((1500, 1450, 1400, 1400, 1200, 1100, 1000, 900, 800, 700))]
    assert core.boundary_tie_pair(pool) == ('x2', 'x3')
    assert core.boundary_tie_pair(pool, skipped={(frozenset(('x2', 'x3')), 1400)}) is None
    pool[3]['elo'] = 1399
    assert core.boundary_tie_pair(pool) is None and core.top_tier_ids(pool) == ['x0', 'x1', 'x2']
    # ---- a tier is settled after about log2(pool) + 3 matches (placement already takes ~log2(pool))
    assert [core.confirm_matches(n) for n in (30, 100, 200)] == [8, 10, 11]
    assert core.tier_confirmed(10, 100) and not core.tier_confirmed(9, 100)

    # ---- artist tags as NovelAI takes them: no backslash escapes, underscores for spaces
    bs = chr(92)
    assert core.sanitize_tag(f'artist:21yc {bs}(september breeze{bs})') == 'artist:21yc_(september_breeze)'
    assert core.sanitize_tag(f'absolute ({bs}queen{bs})') == 'artist:absolute_(queen)'
    assert core.sanitize_tag('  torino   aqua ') == 'artist:torino_aqua' and core.sanitize_tag('artist: ') is None

    # ---- a tie inside the top 30% (the boundary one is not its business)
    inner = [{'id': f'y{i}', 'elo': e, 'matches': 3} for i, e in enumerate((1500, 1450, 1450, 1400, 1300, 1200, 1100, 1000, 900, 800))]
    assert core.top_tie_pair(inner) == ('y1', 'y2')
    assert core.top_tie_pair(inner, skipped={(frozenset(('y1', 'y2')), 1450)}) is None
    assert core.top_tie_pair(inner, exclude={'y2'}) is None
    assert core.top_tie_pair(pool) is None and core.top_tie_pair([{**c, 'elo': 1400} if c['id'] == 'x3' else c for c in pool]) is None
    assert core.is_rated({'matches': 0, 'placed': True}) and not core.is_rated({'matches': 0})

    # ---- storage: atomic write, and an unreadable file is set aside instead of overwritten
    with tempfile.TemporaryDirectory() as tmp:
        state_file = Path(tmp) / 'state.json'
        core.write_state(state_file, {'artists': [], 'combinations': [{'id': 'x'}], 'history': []})
        assert core.load_state(state_file)['combinations'] == [{'id': 'x'}]
        assert not state_file.with_name('state.json.tmp').exists()
        state_file.write_text('{"broken', encoding='utf-8')
        loaded = core.load_state(state_file)
        assert Path(loaded['load_error']).read_text(encoding='utf-8') == '{"broken' and not state_file.exists()

    # ---- style helpers
    assert core.parse_style_combo('artist:plain, 1.2::artist:a::, 1girl') == [(1.0, 'artist:plain'), (1.2, 'artist:a')]
    assert core.rename_artist_in_style('1.2::artist:foo ::, 1.0::artist:foo bar ::', 'artist:foo', 'artist:x9') == \
        '1.2::artist:x9 ::, 1.0::artist:foo bar ::'
    assert core.style_from_pairs([(1.5, 'red scarf'), (0.8, 'artist:a')]) == '1.5::red scarf ::, 0.8::artist:a ::', \
        'NAI form: a space before the closing ::'
    assert core.parse_style_combo('1.5::red scarf ::') == [(1.5, 'red scarf')]
    assert core.clamp_weight(0.849, 0.85, 1.95) == 0.9 and core.clamp_weight(2.5, 0.5, 2.0) == 2.0

    # ---- #n: Elo first; equal Elo -> better win rate, then more matches, then the older combo
    same = lambda key, wins, matches, created: {'id': key, 'elo': 1200, 'wins': wins, 'matches': matches, 'created': created}
    ranked = core.rank_order([same('late', 2, 4, 9), same('few', 1, 2, 1), same('rate', 3, 4, 5),
                              same('early', 2, 4, 3), {'id': 'top', 'elo': 1300, 'wins': 0, 'matches': 5, 'created': 0}])
    assert [c['id'] for c in ranked] == ['top', 'rate', 'early', 'late', 'few'], [c['id'] for c in ranked]
    print('PASS: breeding ranges, weight variants, placement, selection ladder, ties, storage, style helpers.')



def artist_score_check():
    """artist_scores finds each artist's own share, even for artists that always come together."""
    random.seed(3)
    truth = {f'artist:t{i}': (i - 10) * 150 for i in range(20)}  # t0 -1500 … t19 +1350
    tags = list(truth)
    combos = []
    for n in range(300):
        picked = random.sample(tags, 5)
        if 'artist:t19' in picked and 'artist:t0' not in picked:  # the best artist nearly always brings the worst along
            picked[picked.index('artist:t19') - 1] = 'artist:t0'
        pairs = [(round(random.uniform(0.5, 1.8), 1), t) for t in picked]
        elo = core.START_ELO + sum(w * truth[t] for w, t in pairs) + random.gauss(0, 300)
        combos.append({'id': str(n), 'style': core.style_from_pairs(pairs), 'elo': round(elo), 'matches': 5})
    combos.append({'id': 'rare', 'style': core.style_from_pairs([(1.0, 'artist:rare')]), 'elo': 99999, 'matches': 1})
    combos.append({'id': 'unrated', 'style': core.style_from_pairs([(1.0, 'artist:ghost')]), 'elo': 99999, 'matches': 0})
    scores = core.artist_scores(combos)
    fitted = sorted(tags, key=lambda t: scores[t][0])
    assert fitted[0] == 'artist:t0' and fitted[-1] == 'artist:t19', fitted
    assert all(abs(scores[t][0] - truth[t]) < 300 for t in tags), {t: (scores[t][0], truth[t]) for t in tags}
    assert 'artist:ghost' not in scores, 'an unrated combo is no evidence'
    assert scores['artist:rare'][1] == 1 and scores['artist:rare'][0] < 99999 - core.START_ELO, 'one combo only moves it part way'
    print('PASS: artist scores.')



def taste_check():
    """The best artist is drawn about twice as often as an unknown one, the worst half as often;
    the best parent is picked about twice as often as the lowest."""
    random.seed(8)
    tags = ['artist:best', 'artist:worst'] + [f'artist:n{i}' for i in range(8)]
    liked = core.artist_preference({'artist:best': 300, 'artist:worst': -300, 'artist:n0': 0}, tags)
    assert liked['artist:best'] == 2 and liked['artist:worst'] == 0.5 and liked['artist:n5'] == 1
    assert core.artist_preference({}, tags)['artist:best'] == 1, 'no scores yet: every artist alike'
    drawn = collections.Counter(core.parse_style_combo(
        core.breed_weighted_style('1.0::artist:n0 ::', '1.0::artist:n1 ::', tags, 1, 1, 1, 1, 'random', None, liked))[0][1]
        for _ in range(30000))
    unknown = sum(drawn[f'artist:n{i}'] for i in range(8)) / 8
    assert 1.8 < drawn['artist:best'] / unknown < 2.2 and 0.4 < drawn['artist:worst'] / unknown < 0.6, drawn
    ranked = [f'p{i}' for i in range(10)]
    picks = collections.Counter(p for _ in range(30000) for p in core.pick_parents(ranked))
    assert 1.6 < picks['p0'] / picks['p9'] < 2.4, picks
    assert all(a != b for a, b in (core.pick_parents(ranked) for _ in range(500))), 'two different parents'
    print('PASS: taste-weighted artists and parents.')


def usage_estimate_check():
    """The V5 image estimate goes through the two measured points and scales with pixels and percent."""
    v5 = 'nai-diffusion-5-full'
    assert core.estimate_v5_images(100, 832, 1216, 23, v5) == (1735, 1735)
    assert core.estimate_v5_images(100, 832, 1216, 28, v5) == (1488, 1488)
    assert core.estimate_v5_images(50, 832, 1216, 28, v5) == (744, 1488)
    assert core.estimate_v5_images(100, 1024, 1024, 28, v5)[1] < 1488, 'more pixels, fewer images'
    assert core.estimate_v5_images(100, 832, 1216, 20, v5)[1] > 1735, 'fewer steps, more images'
    assert core.estimate_v5_images(100, 832, 1216, 29, v5) is None, 'past 28 steps it is not free'
    assert core.estimate_v5_images(100, 832, 1216, 28, 'nai-diffusion-4-5-full') is None, 'only V5 has the bar'
    print('PASS: V5 usage estimate.')


if __name__ == '__main__':
    main()
    artist_score_check()
    taste_check()
    usage_estimate_check()
