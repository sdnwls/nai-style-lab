"""The window's link to Python: calls, files the window may see (and not), thumbnails, import/export, drag,
unsaved typing kept on close, and a second launch closing the app."""
import json
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'app'))

from PIL import Image  # noqa: E402

from bridge import App  # noqa: E402
from engine import Engine  # noqa: E402
from updater import Updater  # noqa: E402
from version import VERSION  # noqa: E402

PNG = b'\x89PNG\r\n\x1a\n'


def call(app, method, path, body=None):
    reply = json.loads(app.call(method, path, body))  # the page reads JSON text, so check it is that
    assert isinstance(reply, dict) and isinstance(reply['ok'], bool)
    return reply


def ok(app, method, path, body=None):
    reply = call(app, method, path, body)
    assert reply['ok'], reply
    return reply['data']


def error(app, method, path, body=None):
    reply = call(app, method, path, body)
    assert not reply['ok'] and reply['error'], reply
    return reply['error']


def main():
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp)
        (data / 'images').mkdir()
        (data / 'images' / 'ok.png').write_bytes(PNG)
        (data / 'secret.txt').write_text('do not serve', encoding='utf-8')
        engine = Engine(data, generate=lambda *a, **k: PNG)
        app = App(engine)

        # ---- calls: the same answers the page always had, as JSON text
        assert ok(app, 'GET', '/api/status')['stage']['step'] == 0
        assert ok(app, 'GET', '/api/status?since=3')['events'] == [], 'a query in the path is read'
        assert '작가 수' in error(app, 'POST', '/api/settings', {'changes': {'gen_min': 9, 'gen_max': 3}})
        meta = ok(app, 'GET', '/api/meta')
        assert meta['ranges']['evo_count'] == [1, 60], 'the input limits come from the one table'
        assert meta['version'] == VERSION
        rev = lambda: ok(app, 'GET', '/api/status')['settings_rev']
        before = rev()
        ok(app, 'POST', '/api/settings', {'changes': {'steps': 20}})
        assert rev() != before, 'a settings change is visible in the status, so every page refetches them'
        assert ok(app, 'POST', '/api/artists/add', {'text': 'a, b\n1.2::c::'})['added'] == 3, 'bare names: each one'
        # A whole prompt: its artist: tags only, not one pushed away by a negative weight, nor 1girl and the like.
        prompt = '1girl, 1.2::artist:d, artist:e::, {artist:f}, -1::artist:g::, masterpiece, artist:a'
        assert ok(app, 'POST', '/api/artists/scan', {'text': prompt}) == {
            'pairs': [{'w': 1.2, 'tag': 'artist:d'}, {'w': 1.2, 'tag': 'artist:e'}, {'w': 1.0, 'tag': 'artist:f'},
                      {'w': 1.0, 'tag': 'artist:a'}],
            'new': ['artist:d', 'artist:e', 'artist:f']}
        assert ok(app, 'POST', '/api/artists/add', {'text': prompt})['added'] == 3
        assert '알 수 없는' in error(app, 'GET', '/api/nope')
        assert '알 수 없는' in error(app, 'POST', '/api/nope', {})
        assert '알 수 없는' in error(app, 'DELETE', '/api/status')
        reply = call(app, 'POST', '/api/undo', None)
        assert reply['ok'] or '내부 오류' not in reply['error'], 'no body is an empty one'
        broken = []
        app.engine.list_artists = lambda: {'bad': {1, 2}}  # not JSON: an error for the page, not a broken reply
        app.log = broken.append
        assert '내부 오류' in error(app, 'GET', '/api/artists') and broken
        del app.engine.list_artists
        app.log = lambda text: None

        # ---- files: the page, its files and the pictures, nothing else
        page = app.resource('/')
        assert page and page[0] == ROOT / 'web' / 'index.html' and page[1].startswith('text/html')
        assert 'token' not in page[0].read_text(encoding='utf-8'), 'nothing secret in the page'
        js = app.resource('/static/js/app.js')
        assert js and js[1].startswith('text/javascript') and js[2] == 'no-store'
        assert app.resource('/static/app.css')[1].startswith('text/css')
        assert app.resource('/static/icon.svg')[1] == 'image/svg+xml'
        for sneaky in ('/static/../app/engine.py', '/static/..%2Fapp%2Fengine.py', '/static/', '/static/nope.js',
                       '/img/..%2Fsecret.txt', '/img/../secret.txt', '/img/%2E%2E%2Fsecret.txt', '/img/missing.png',
                       '/thumb/..%2Fsecret.txt', '/thumb/missing.png', '/secret.txt', '/api/status', '/img/', ''):
            assert app.resource(sneaky) is None, sneaky
        image = app.resource('/img/ok.png')
        assert image == ((data / 'images' / 'ok.png').resolve(), 'image/png', 'max-age=31536000, immutable')

        # ---- thumbnails: a missing one is the full picture this once (not cached), made in the background
        Image.new('RGB', (832, 1216), (200, 40, 90)).save(data / 'images' / 'big.png')
        first = app.resource('/thumb/big.png')
        assert first == ((data / 'images' / 'big.png').resolve(), 'image/png', 'no-store')
        app.resource('/thumb/big.png')
        assert app.thumb_queue.qsize() == 1, 'asked twice, made once'
        threading.Thread(target=app.thumbnail_worker, daemon=True).start()
        for _ in range(100):
            if (data / 'thumbs' / 'big.webp').exists() and not app.thumb_waiting:
                break
            time.sleep(0.05)
        thumb = app.resource('/thumb/big.png')
        assert thumb == (data / 'thumbs' / 'big.webp', 'image/webp', 'max-age=31536000, immutable')
        with Image.open(thumb[0]) as made:
            assert made.size[0] <= 440 and made.size[1] <= 760
        assert not list((data / 'thumbs').glob('*.part')), 'no half-written thumbnail left'

        # ---- import: Open picks the zip, Python reads it; the page never names a file
        assert '먼저 고르세요' in error(app, 'POST', '/api/data/import')
        not_zip = data / 'not.zip'
        not_zip.write_bytes(b'not a zip')
        app.open_dialog = lambda: str(not_zip)
        assert ok(app, 'POST', '/api/data/pick') == {'name': 'not.zip'}
        assert 'zip' in error(app, 'POST', '/api/data/import', {'path': str(data / 'secret.txt')})
        assert '먼저 고르세요' in error(app, 'POST', '/api/data/import'), 'a pick is used once'
        app.open_dialog = lambda: None
        assert ok(app, 'POST', '/api/data/pick') is None
        assert '먼저 고르세요' in error(app, 'POST', '/api/data/import'), 'a cancelled Open leaves nothing to import'

        # ---- export: Save As is Windows UI; stand in for the user's choice (a path, then a cancel)
        target = data / 'picked.zip'
        app.save_dialog = lambda name: str(target)
        assert ok(app, 'POST', '/api/data/export', {})['path'] == str(target)
        with zipfile.ZipFile(target) as zf:
            assert 'state.json' in zf.namelist() and 'images/ok.png' in zf.namelist()
        assert not (data / 'exports').exists() and not Path(str(target) + '.part').exists(), 'nothing left behind'
        app.save_dialog = lambda name: None
        assert ok(app, 'POST', '/api/data/export', {})['path'] is None
        # ... and that export imports again
        app.open_dialog = lambda: str(target)
        ok(app, 'POST', '/api/data/pick')
        imported = ok(app, 'POST', '/api/data/import')
        assert imported['images'] >= 1 and (data / 'images' / 'ok.png').exists()

        # ---- an export from a newer release (a newer data format) is refused, the current data kept
        newer = data / 'newer.zip'
        with zipfile.ZipFile(newer, 'w') as zf:
            zf.writestr('state.json', json.dumps({'schema': 99, 'combinations': []}))
        before = len(engine.combos)
        app.open_dialog = lambda: str(newer)
        ok(app, 'POST', '/api/data/pick')
        assert '더 새 버전' in error(app, 'POST', '/api/data/import') and len(engine.combos) == before

        # ---- updates: the state rides on every status, installing waits for a running generation
        assert ok(app, 'GET', '/api/status')['update'] is None, 'no updater (tests): nothing to show'

        def offline(url, accept):
            raise OSError('offline')
        app.updater = Updater(data, VERSION, 'o/r', fetch=offline)
        assert ok(app, 'GET', '/api/status')['update']['current'] == VERSION
        assert '인터넷' in ok(app, 'POST', '/api/update/check')['error']
        assert '설치할 새 버전' in error(app, 'POST', '/api/update/install'), 'its message, as a readable error'
        assert '릴리스 페이지' in error(app, 'POST', '/api/update/page')
        engine.job = {'running': True}
        assert '생성 작업' in error(app, 'POST', '/api/update/install')
        engine.job = None

        # ---- a picture dragged out of the window: only a real file in images/ is handed to Windows
        dragged = []
        app.start_drag = dragged.append
        ok(app, 'POST', '/api/drag', {'file': 'ok.png'})
        assert dragged == [(data / 'images' / 'ok.png').resolve()]
        for sneaky in ('..%2Fsecret.txt', '../secret.txt', 'missing.png', None):
            error(app, 'POST', '/api/drag', {'file': sneaky})
        assert len(dragged) == 1

        # ---- typing not saved yet is saved on close: the newest draft wins, a refused one changes nothing
        ok(app, 'POST', '/api/settings/draft', {'seq': 2, 'changes': {'steps': 31}})
        ok(app, 'POST', '/api/settings/draft', {'seq': 1, 'changes': {'steps': 12}})  # arrived late: older
        ok(app, 'POST', '/api/settings/draft', {'seq': 'x', 'changes': {'steps': 13}})
        app.shutdown()
        assert engine.settings()['steps'] == 31
        assert json.loads((data / 'state.json').read_text(encoding='utf-8'))['ui_state']['steps'] == 31, 'saved'
        ok(app, 'POST', '/api/settings/draft', {'seq': 3, 'changes': {}})  # saved by the page: nothing left
        app.shutdown()
        assert engine.settings()['steps'] == 31
        ok(app, 'POST', '/api/settings/draft', {'seq': 4, 'changes': {'gen_min': 9, 'gen_max': 3}})
        app.shutdown()  # out of range: like any refused change, nothing changes and closing still saves
        assert engine.settings()['gen_min'] != 9

        # ---- a second launch with newer code: close unless generating
        closed = threading.Event()
        app.close_window = closed.set
        assert app.retire() == {'closing': True} and closed.wait(2)
        engine.job = {'running': True}
        assert app.retire() == {'closing': False}
        engine.job = None
    print('PASS: calls, files (page, pictures, nothing else), thumbnails, import/export, drag, typing kept on close.')


if __name__ == '__main__':
    main()
