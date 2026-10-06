"""End-to-end workflow checks on a throwaway data folder with a fake image generator (never calls NovelAI)."""
import io
import json
import random
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'app'))

import core  # noqa: E402
from engine import CONVERGED_GENERATIONS, Engine, UserError  # noqa: E402

PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 64


class FakeNai:
    """Records every request; returns a tiny PNG. Can be made to fail."""
    def __init__(self):
        self.calls = []
        self.fail_status = None

    def __call__(self, settings, style, seed, stop_event, log=None):
        self.calls.append({'style': style, 'seed': seed})
        if self.fail_status:
            raise core.NaiHttpError(self.fail_status, f'HTTP {self.fail_status}')
        return PNG


def wait_job(engine, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if not engine.job or not engine.job['running']:
            return
        time.sleep(0.01)
    raise AssertionError('job did not finish')


def make_engine(tmp, artists=12):
    fake = FakeNai()
    engine = Engine(Path(tmp), generate=fake)
    engine.state['artists'] = [{'tag': f'artist:a{i}', 'count': 0, 'arena_matches': 0, 'arena_wins': 0}
                               for i in range(artists)]
    engine.update_settings({'api_key': 'test-key', 'delay': 0, 'gen_min': 3, 'gen_max': 5,
                            'global_min_w': 0.5, 'global_max_w': 1.5})
    return engine, fake


def vote_by(engine, taste, max_votes=2000, stop=None):
    """Answer the workflow's matches with a hidden taste score until only regular league
    matches remain (or ``stop`` says so); returns the number of votes cast."""
    for n in range(max_votes):
        m = engine.match()
        if m['kind'] in (None, 'league', 'top') or (stop and stop(m)):
            return n
        a, b = m['a'], m['b']
        engine.vote('a' if taste(a) >= taste(b) else 'b')
    raise AssertionError('too many votes')


def main():
    random.seed(11)
    with tempfile.TemporaryDirectory() as tmp:
        engine, fake = make_engine(tmp)

        # ---- 1. first run: stage guidance, random combos, one seed, rules respected
        assert engine.stage()['step'] == 0 and engine.stage()['action'] == '작가 확인', 'the default artist list is not a choice yet'
        engine.add_artists('artist:someone')
        assert engine.stage()['params'] == {'focus': 'prompts'}, 'editing the list checks it; the prompt comes next'
        engine.update_settings({'negative': 'lowres'})
        assert engine.stage()['route'] == 'library', 'editing the prompt counts as checking it'
        engine.start_random(12)
        wait_job(engine)
        assert len(engine.combos) == 12 and len(fake.calls) == 12
        seed = int(engine.settings()['seed'])
        assert {c['seed'] for c in fake.calls} == {seed}, 'every image uses the one stored seed'
        for combo in engine.combos:
            pairs = core.parse_style_combo(combo['style'])
            assert 3 <= len(pairs) <= 5 and all(0.5 <= w <= 1.5 for w, _ in pairs)
        assert engine.stage()['step'] == 1
        engine.match()  # the first combo silently becomes the anchor
        assert engine.ranking_counts() == (0, 12) and '12개' in engine.stage()['todo'], 'the anchor alone is no ranking'
        assert not any(v['rated'] for v in engine.list_combos()['active']), 'nothing shows as ranked before a vote'

        # ---- 2. placement by binary insertion reproduces a hidden taste exactly
        taste_of = {c['id']: random.random() for c in engine.combos}
        taste = lambda view: taste_of[view['id']]
        votes = vote_by(engine, taste)
        assert all(core.is_rated(c) for c in engine.combos)
        by_elo = [c['id'] for c in sorted(engine.combos, key=lambda c: c['elo'])]
        assert by_elo == sorted(taste_of, key=taste_of.get), 'placement order must match the taste'
        assert votes <= 12 * 4, votes  # binary insertion: about log2(k) per newcomer
        assert engine.stage()['step'] == 2

        # ---- 3. undo restores a newcomer that a vote had just placed
        engine.start_random(1)
        wait_job(engine)
        newcomer = engine.newcomers()[0]
        before = dict(newcomer)
        assert engine.match()['kind'] == 'place'
        while engine.newcomers():
            engine.vote('a')
        placed = engine.find(before['id'])
        assert core.is_rated(placed)
        engine.undo()
        assert not core.is_rated(engine.find(before['id'])) and engine.find(before['id'])['elo'] == before['elo']
        vote_by(engine, lambda v: taste_of.setdefault(v['id'], random.random()))

        # ---- 4. evolution: generate, select while generating, settle, population kept
        population = len(engine.combos)
        parents = engine.top_ids()
        assert len(parents) == core.top_tier_cut(population) >= 2
        engine.start_evolution(8)
        wait_job(engine)
        children = list(engine.selection['candidate_ids'])
        assert len(children) == 8
        for cid in children:
            taste_of[cid] = random.random()
        vote_by(engine, lambda v: taste_of[v['id']])
        assert engine._selection_done()
        plan = engine._generation_plan()
        entered = len(plan['entered'])
        log = engine.finish_generation()
        assert len(engine.combos) == population, 'population stays the same'
        assert log['failed'] + log['entered'] == 8 and log['dropped'] == entered
        reasons = {c['excluded']['reason'] for c in engine.retired}
        assert reasons <= {'탈락', '제외'}
        assert set(engine.top_ids()) == set(plan['next_parents'])
        for combo in engine.combos:
            pairs = core.parse_style_combo(combo['style'])
            assert 3 <= len(pairs) <= 5 and all(0.5 <= w <= 1.5 for w, _ in pairs), combo['style']
        saved_log = list(engine.selection['log'])
        engine.selection['log'] += [{'entered': 0}] * CONVERGED_GENERATIONS
        stage = engine.stage()
        if engine.top_tie() or engine.boundary_tie():  # these random votes can leave one: settling it comes first
            assert stage['route'] == 'arena', 'a top-30% tie is asked before refine'
        else:
            assert stage['step'] == 3 and stage['route'] == 'refine', 'converged evolution moves to refine'
        engine.selection['log'] = saved_log

        # ---- 5. boundary tie is forced first in any mode and resolved by a rated vote
        ranked = sorted(engine.rated(), key=lambda c: -c['elo'])
        cut = core.top_tier_cut(len(ranked))
        ranked[cut]['elo'] = ranked[cut - 1]['elo']
        engine.update_settings({'arena_mode': 'league'})
        m = engine.match()
        tie_elo = ranked[cut - 1]['elo']  # other combos may share it too: any two of the tied ones will do
        assert m['kind'] == 'tie' and engine.find(m['a']['id'])['elo'] == engine.find(m['b']['id'])['elo'] == tie_elo
        engine.vote('a')
        assert engine.boundary_tie() is None
        # Ties inside the top 30% come next and block evolution until settled. Settling one moves Elo, which can
        # make a new boundary tie: that one is always asked first.
        while engine.top_tie() or engine.boundary_tie():
            assert engine._evolution_blocker() and engine.stage()['route'] == 'arena'
            m = engine.match()
            expected = '상위 30% 경계 동점' if engine.boundary_tie() else '상위 30% 동점'
            assert m['kind'] == 'tie' and m['context']['title'] == expected, m.get('context')
            engine.vote('a')
        engine.update_settings({'arena_mode': 'auto'})

        # ---- 6. a second generation; the start request's count becomes the saved default
        generation = engine.selection['generation']
        engine.start_evolution(4)
        wait_job(engine)
        assert engine.settings()['evo_count'] == 4, 'the count asked for is kept for next time'
        for cid in engine.selection['candidate_ids']:
            taste_of[cid] = random.random()
        assert {engine.find(cid)['generation'] for cid in engine.selection['candidate_ids']} == {generation},             "a child's generation is the evolution tab's generation, not a lineage depth"
        vote_by(engine, lambda v: taste_of.setdefault(v['id'], random.random()))
        engine.finish_generation()
        assert engine.selection['generation'] == generation + 1

        while engine.boundary_tie() or engine.top_tie():  # ties in the top 30% are (rightly) asked first; settle them
            assert engine.match()['kind'] == 'tie'
            engine.vote('a')

        # ---- 6b. every page shows a combo under the same #n
        shown = {c['id']: c['rank'] for c in engine.list_combos()['active']}
        assert sorted(n for n in shown.values() if n) == list(range(1, len(engine.rated()) + 1))
        assert all(c['rank'] == shown[c['id']] for c in engine.improve_view()['candidates'] + engine.evolution_view()['parents'])

        # ---- 7. refine: the original as first champion, weight variants on its seed, narrowing, final check
        base = max(engine.rated(), key=lambda c: c['elo'])
        base_pairs = core.parse_style_combo(base['style'])
        base['seed'] = 1  # an old combo drawn with another seed: its variants use that seed, no re-render
        fake.calls.clear()
        engine.start_improve(base['id'], variants=3)
        wait_job(engine)
        imp = engine.state['improve']
        assert len(fake.calls) == 3 and all(c['seed'] == 1 and c['style'] != base['style'] for c in fake.calls)
        assert imp['champion_id'] == base['id']
        for key in imp['order']:
            variant = core.parse_style_combo(imp['items'][key]['style'])
            assert [t for _, t in variant] == [t for _, t in base_pairs], 'same artists, only weights move'
            assert all(abs(w - bw) <= 0.3 + 1e-9 for (w, _), (bw, _) in zip(variant, base_pairs))
        m = engine.match()
        assert m['kind'] == 'improve' and {m['a']['role'], m['b']['role']} == {'챔피언', '변형'}
        challenger = imp['pending'][0]
        engine.vote('a' if m['a']['id'] == challenger else 'b')
        assert imp['champion_id'] == challenger
        engine.undo()
        assert imp['champion_id'] != challenger and imp['pending'][0] == challenger
        engine.skip()  # "similar": shown again at the end of the round; undo puts it back in front
        assert imp['pending'][-1] == challenger and imp['champion_id'] == base['id'] and 'result' not in imp['items'][challenger]
        engine.undo()
        assert imp['pending'][0] == challenger and 'similar' not in imp['items'][challenger]
        while imp['pending']:
            m = engine.match()
            engine.vote('a' if m['a']['id'] in imp['pending'] else 'b')  # variants keep winning
        assert imp['status'] == 'round_done' and imp['changed']
        engine.improve_next_round()
        wait_job(engine)
        assert imp['jitter'] == 0.2 and imp['round'] == 2
        while imp['pending']:
            m = engine.match()
            engine.vote('a' if m['a']['id'] == imp['champion_id'] else 'b')  # champion holds
        engine.improve_final_check()
        wait_job(engine)
        assert imp['status'] == 'final_ready' and all(f['champion'] and f['base'] for f in imp['finals'])
        final_seeds = {f['seed'] for f in imp['finals']}
        assert len(final_seeds) == 3
        champion_id = imp['champion_id']
        for f in imp['finals']:  # the fake generator does not write files; make them real to see them deleted
            for role in ('champion', 'base'):
                (engine.img_dir / f[role]).write_bytes(PNG)
        final_files = [f[role] for f in imp['finals'] for role in ('champion', 'base')]
        result = engine.finish_improve('champion')
        assert result == champion_id and engine.find(champion_id)['final'] is True
        assert engine.find(champion_id) in engine.newcomers(), 'the refined combo finds its place in the ranking'
        assert engine.stage()['step'] == 4, 'a finished refine stays at 완성, also while its combo is being placed'
        assert engine.state['improve'] is None
        assert not any((engine.img_dir / f).exists() for f in final_files), 'final-check previews are cleaned up'
        assert all(c['excluded']['reason'] == '탈락' for c in engine.retired if c['excluded'].get('improve') == base['id'])

        # ---- 8. refusing to breed on an unresolved tie, stage 4/5 guidance, auth failure stops the job
        engine.start_improve(base['id'], variants=2)
        wait_job(engine)
        assert engine.stage()['step'] == 3
        engine.finish_improve('discard')
        fake.fail_status = 401
        calls_before = len(fake.calls)
        engine.start_random(5)
        wait_job(engine)
        assert len(fake.calls) - calls_before == 1 and engine.job['failures'] == 1, 'auth failure stops after one try'
        fake.fail_status = None

        # ---- 9. persistence: everything survives a restart
        engine.save()
        again = Engine(Path(tmp), generate=fake)
        assert len(again.combos) == len(engine.combos) and again.selection['generation'] == engine.selection['generation']
        assert again.settings()['seed'] == str(seed)

        # ---- 10. revive / delete / purge / Elo edit
        out = engine.retired[0]
        engine.revive([out['id']])
        assert engine.find(out['id']) and 'excluded' not in out
        engine.set_elo([out['id']], 1234)
        assert engine.find(out['id'])['elo'] == 1234
        (engine.img_dir / out['image_file']).write_bytes(PNG)
        engine.delete_combos([out['id']])
        assert not engine.find_any(out['id']) and not (engine.img_dir / out['image_file']).exists()
        n = sum(1 for c in engine.retired if c['excluded']['reason'] == '탈락')
        assert engine.purge('탈락')['deleted'] == n and not any(c['excluded']['reason'] == '탈락' for c in engine.retired)
        try:
            engine.set_elo([engine.combos[0]['id']], -1)
            raise AssertionError('a negative Elo must be refused')
        except UserError:
            pass

        # ---- 10b. export / import round trip (the API key travels with it; zip paths cannot escape)
        buffer = io.BytesIO()
        engine.export_data(buffer)
        exported = buffer.getvalue()
        with zipfile.ZipFile(io.BytesIO(exported)) as zf:
            assert zf.read('api_key.txt').decode() == engine.api_key
        before = json.dumps(engine.state, sort_keys=True)
        images_before = sorted(p.name for p in engine.img_dir.glob('*.png'))
        key_before = engine.api_key
        engine.reset('combos')
        engine.update_settings({'api_key': 'another-key'})
        assert not engine.combos
        result = engine.import_data(exported)
        assert result['images'] == len(images_before) and json.dumps(engine.state, sort_keys=True) == before
        assert sorted(p.name for p in engine.img_dir.glob('*.png')) == images_before and engine.api_key == key_before
        assert engine.key_file.read_text(encoding='utf-8') == key_before, 'the imported key is saved too'
        evil = io.BytesIO()
        with zipfile.ZipFile(evil, 'w') as zf:
            zf.writestr('state.json', json.dumps({'combinations': []}))
            zf.writestr('images/../../escaped.png', b'x')
        engine.import_data(evil.getvalue())
        assert not (Path(tmp).parent / 'escaped.png').exists() and not (Path(tmp) / 'escaped.png').exists()
        try:
            engine.import_data(b'not a zip')
            raise AssertionError('a non-zip must be refused')
        except UserError:
            pass
        engine.import_data(exported)

        # ---- 11. resets (no backups)
        engine.update_settings({'steps': 20})
        engine.reset('ratings')
        assert all(c['elo'] == core.START_ELO and c['matches'] == 0 for c in engine.combos) and not engine.state['history']
        assert engine.newcomers(), 'combos find their place again after a ratings reset'
        engine.reset('evolution')
        assert engine.selection['generation'] == 1 and not engine.selection['log']
        engine.reset('settings')
        assert engine.settings()['steps'] == 28 and engine.settings()['seed'] == str(seed), 'seed survives'
        kept_artists = len(engine.state['artists'])
        image = engine.combos[0]['image_file']
        engine.reset('combos')
        assert not engine.combos and not engine.retired and len(engine.state['artists']) == kept_artists
        assert not (engine.img_dir / image).exists() and engine.api_key
        assert not (Path(tmp) / 'backups').exists(), 'resets leave no backup'
        engine.reset('all')
        assert engine.settings()['seed'] == '' and not engine.api_key and not engine.key_file.exists()
        assert Engine(Path(tmp), generate=fake).combos == [], 'the reset is saved'

    print('PASS: first run, placement, undo, evolution, boundary tie, second generation, refine, persistence, library edits, resets.')


def refuses(fn, *args):
    try:
        fn(*args)
    except UserError:
        return True
    raise AssertionError(f'{fn.__name__}{args} should have been refused')


def guards():
    """Refused settings, failure streaks, safe import, refine undo / final check, live Elo edits."""
    import engine as engine_module
    random.seed(5)
    with tempfile.TemporaryDirectory() as tmp:
        engine, fake = make_engine(tmp)

        # ---- a refused setting leaves nothing behind
        for bad in ({'gen_min': 9}, {'steps': 0}, {'cfg': float('nan')}, {'model': 'nope'}, {'arena_blind': 'false'}, {'auto_evolution': True}):
            refuses(engine.update_settings, bad)
        assert engine.settings()['gen_min'] == 3 and engine.settings()['steps'] == 28 and engine.settings()['arena_blind'] is False
        engine.state['ui_state']['size'] = '세로 960x1088'  # saved by an older release that still offered it
        assert engine.settings()['size'] == engine_module.SETTING_DEFAULTS['size'], 'a dropped choice falls back to the default'
        del engine.state['ui_state']['size']

        # ---- errors that are not retried or auth still stop the job after a short streak
        fake.fail_status = 400
        engine.start_random(5)
        wait_job(engine)
        assert len(fake.calls) == engine.job['failures'] == engine.job['done'] == engine_module.MAX_FAILURE_STREAK, 'progress counts tries, failures shown apart'
        fake.fail_status = None

        # ---- the set delay goes between a job's images only; every new combo starts with the same fields
        pauses = []
        engine._pause = lambda settings: pauses.append(1)
        engine.start_random(3)
        wait_job(engine)
        assert len(pauses) == 2, 'three images, two waits: none before the first or after the last'
        del engine._pause
        assert all(set(c) >= {'placed', 'created', 'seed'} for c in engine.combos)

        # ---- a free prompt with no artist tags leaves no stray comma where {artist} was
        sent = {}

        class FakeConnection:
            def __init__(self, *a, **k): pass
            def request(self, method, path, body=None, headers=None): sent.update(json.loads(body))
            def getresponse(self): return type('R', (), {'status': 200, 'reason': 'OK', 'read': lambda self: PNG})()
            def close(self): pass
        real = core.http.client.HTTPSConnection
        core.http.client.HTTPSConnection = FakeConnection
        try:
            core.generate_style_image('k', '{artist}, 1girl, solo', [], '', '', 'm', 64, 64, 1, 5.0, 's', 1, 0.0)
            assert sent['parameters']['v4_prompt']['caption']['base_caption'] == '1girl, solo'
            # As novelai.net sends it, so an imported image comes out the same there: no Variety+, the base prompt
            # alone in input (the site imports it as the base prompt), and every character once in its own slot.
            core.generate_style_image('k', '{artist}, 2girls', ['ahri', 'ahri', 'sona'], 'artist:a', '', 'm', 64, 64,
                                      1, 5.0, 's', 1, 0.0)
        finally:
            core.http.client.HTTPSConnection = real
        p = sent['parameters']
        assert sent['input'] == p['v4_prompt']['caption']['base_caption'] == 'artist:a, 2girls'
        assert 'skip_cfg_above_sigma' not in p
        assert [c['char_caption'] for c in p['v4_prompt']['caption']['char_captions']] == ['ahri', 'ahri', 'sona']
        assert [c['prompt'] for c in p['characterPrompts']] == ['ahri', 'ahri', 'sona']
        assert len(p['v4_negative_prompt']['caption']['char_captions']) == 3

        engine.start_random(14)
        wait_job(engine)
        taste_of = {}
        taste = lambda v: taste_of.setdefault(v['id'], random.random())
        vote_by(engine, taste)

        # ---- an image a viewer holds open: deleting or wiping fails, the record stays
        held = engine.combos[-1]
        held_file = engine.img_dir / held['image_file']
        held_file.write_bytes(PNG)
        count_before = len(engine.combos)
        with open(held_file, 'rb'):  # Windows refuses to delete a file opened like this
            refuses(engine.delete_combos, [held['id']])
            refuses(engine.reset, 'combos')
        assert len(engine.combos) == count_before and held_file.exists()
        assert engine.delete_combos([held['id']]) == {'deleted': 1} and not held_file.exists()

        # ---- a failed import leaves the current data and images alone
        image, count_before = engine.combos[0]['image_file'], len(engine.combos)
        (engine.img_dir / image).write_bytes(PNG)
        broken = io.BytesIO()
        with zipfile.ZipFile(broken, 'w') as zf:
            zf.writestr('state.json', json.dumps({'combinations': [{'id': 'x'}]}))
        refuses(engine.import_data, broken.getvalue())
        assert (engine.img_dir / image).exists() and len(engine.combos) == count_before

        # ---- refine: undo of a round's last vote, renaming mid-session, a final check of the original alone
        base = max(engine.rated(), key=lambda c: c['elo'])
        engine.start_improve(base['id'], variants=2)
        wait_job(engine)
        imp = engine.state['improve']
        votes_before = engine.status()['counts']['votes']
        while imp['pending']:
            m = engine.match()
            engine.vote('a' if m['a']['id'] == imp['champion_id'] else 'b')  # champion holds
        assert imp['status'] == 'round_done'
        engine.undo()
        waiting = imp['pending'][0]
        assert imp['status'] == 'voting' and len(imp['pending']) == 1
        assert 'result' not in imp['items'][waiting] and engine.status()['counts']['votes'] == votes_before + 1
        m = engine.match()
        engine.vote('a' if m['a']['id'] == imp['champion_id'] else 'b')
        assert imp['status'] == 'round_done', 'the round ends again'
        tag = core.parse_style_combo(base['style'])[0][1]
        engine.rename_artist(tag, 'artist:renamed_x')
        assert all('artist:renamed_x' in item['style'] for item in imp['items'].values()), 'refine variants follow a rename'
        fake.calls.clear()
        engine.improve_final_check()
        wait_job(engine)
        assert len(fake.calls) == 3 and imp['status'] == 'final_ready', 'an unchanged champion is drawn once per seed'
        assert all(f['champion'] == f['base'] for f in imp['finals'])
        engine.improve_resume()
        fake.fail_status = 500
        engine.improve_final_check()
        wait_job(engine)
        assert imp['status'] == 'round_done' and imp['finals'] == [], 'a final check cut short goes back to the round'
        fake.fail_status = None
        engine.finish_improve('discard')

        # ---- editing a parent's Elo mid-selection moves the frozen ladder too
        engine.start_evolution(4)
        wait_job(engine)
        parent = engine.selection['parent_ids'][0]
        engine.set_elo([parent], 1777)
        assert engine.selection['parent_scores'][parent] == 1777
        refuses(engine.start_random, 999)  # a start count is checked like its settings field
    print('PASS: refused settings, failure streak, safe import, refine undo / final check, live Elo edits.')


class GatedNai:
    """A generator that holds every image until released, so a job can be kept running on purpose."""
    def __init__(self):
        self.started, self.release = threading.Semaphore(0), threading.Semaphore(0)

    def __call__(self, settings, style, seed, stop_event, log=None):
        self.started.release()
        assert self.release.acquire(timeout=10), 'image never released'
        return PNG

    def step(self):  # let the image being drawn finish, then wait for the next one to start
        self.release.release()
        assert self.started.acquire(timeout=10)


def busy_starts():
    """A start refused while images are drawn changes nothing; refine variants follow the current champion."""
    random.seed(11)
    with tempfile.TemporaryDirectory() as tmp:
        engine, fake = make_engine(tmp)
        engine.start_random(12)
        wait_job(engine)
        voted = []
        for _ in range(3):  # undoing placement votes brings back each placement match in turn, newest first
            m = engine.match()
            voted.append((m['kind'], m['a']['id'], m['b']['id']))
            engine.vote('a')
        shown = [engine.undo() for _ in range(3)]
        assert [(v['kind'], v['a']['id'], v['b']['id']) for v in shown] == voted[::-1], (shown, voted)
        vote_by(engine, lambda c: hash(c['style']) % 1000)
        assert engine._evolution_blocker() is None
        m = engine.match()  # a vote also hands back the pair as it is now (blind mode reveals it)
        after = engine.vote('a')['last']
        assert after['a']['elo'] > m['a']['elo'] and after['b']['elo'] < m['b']['elo']
        skipped = engine.match()
        assert after['a']['wins'] == m['a']['wins'] + 1 and engine.skip()['last'].keys() == {'a', 'b'}
        back = engine.undo()  # a league "비슷함" can be taken back too: its match comes back
        assert (back['a']['id'], back['b']['id']) == (skipped['a']['id'], skipped['b']['id']), 'undo brings back the skipped match'
        back = engine.undo()  # then the vote before it
        assert (back['kind'], back['a']['id'], back['b']['id']) == (m['kind'], m['a']['id'], m['b']['id']), 'same match, same sides'
        assert back['a']['elo'] == m['a']['elo'] and engine.match()['a']['id'] == m['a']['id'], 'and it stays the match'
        gate = GatedNai()
        engine._generate = gate

        # ---- another job runs: every start is refused before it touches anything
        engine.start_free('x')
        assert gate.started.acquire(timeout=10)
        base = core.rank_order(engine.rated())[0]
        assert refuses(engine.start_evolution, 4) and engine.selection['batch_id'] is None and engine.generating_children is None
        assert refuses(engine.start_improve, base['id']) and engine.state['improve'] is None
        gate.release.release()
        wait_job(engine)

        # ---- a variant that wins mid-round is what the next variants vary
        drawn = []
        jitter = core.jitter_weights
        core.jitter_weights = lambda pairs, *a, **k: (drawn.append(list(pairs)), jitter(pairs, *a, **k))[1]
        try:
            engine.start_improve(base['id'], 3)
            assert gate.started.acquire(timeout=10)
            gate.step()  # variant 1 is kept, variant 2 is drawn (still from the original) and being drawn
            m = engine.match()
            while m['kind'] != 'improve':
                m = engine.vote('a')
            winner = m['a'] if m['a']['role'] == '변형' else m['b']
            engine.vote('a' if winner is m['a'] else 'b')
            gate.step()
            gate.release.release()
            wait_job(engine)
        finally:
            core.jitter_weights = jitter
        assert drawn[0] == core.parse_style_combo(base['style'])
        assert drawn[-1] == core.parse_style_combo(winner['style']), 'variant 3 varies the new champion'

        # ---- a finished round stays as it is when the next round or the final check is refused
        m = engine.match()
        while m['kind'] == 'improve':
            m = engine.skip()
        imp = engine.state['improve']
        assert imp['status'] == 'round_done'
        assert all(imp['items'][k].get('result') for k in imp['order']), 'similar twice settles the variant'
        engine.start_free('y')
        assert gate.started.acquire(timeout=10)
        assert refuses(engine.improve_next_round) and imp['round'] == 1 and imp['status'] == 'round_done'
        assert refuses(engine.improve_final_check) and imp['status'] == 'round_done'
        gate.release.release()
        wait_job(engine)

        # ---- free results are all kept
        engine._generate = fake
        for n in range(30):
            engine.start_free(f'p{n}')
            wait_job(engine)
        assert len(engine.state['free_results']) == 32
    print('PASS: refused starts leave no state, refine follows the current champion, free results kept.')


def auto_subscription():
    """The app checks the subscription when a run starts and ends; engines made without it (tests) never do."""
    with tempfile.TemporaryDirectory() as tmp:
        checks = []
        engine = Engine(Path(tmp), generate=FakeNai(), fetch_subscription=lambda key: checks.append(key) or
                        {'tier': 'Opus', 'tier_num': 3, 'unlimited': True, 'active': True, 'remaining_percent': 80, 'anlas': 0},
                        auto_subscription=True)
        engine.update_settings({'api_key': 'k', 'delay': 0})
        engine.start_free('x')
        wait_job(engine)
        for _ in range(200):
            if len(checks) >= 2:
                break
            time.sleep(0.01)
        assert checks == ['k', 'k'] and engine.subscription_view()['tier'] == 'Opus', checks
        quiet = Engine(Path(tmp), generate=FakeNai(), fetch_subscription=lambda key: checks.append('no'))
        quiet.start_free('y')
        wait_job(quiet)
        time.sleep(0.05)
        assert 'no' not in checks
    print('PASS: subscription checked around runs.')


if __name__ == '__main__':
    main()
    guards()
    busy_starts()
    auto_subscription()
