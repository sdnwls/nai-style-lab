"""Updates: versions, the GitHub check, download (size and checksum), unpack, the all-or-nothing swap (data/
untouched, every move undone on a failure), the swap process itself, and the data format upgrade at launch."""
import hashlib
import io
import json
import os
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'app'))

import core  # noqa: E402
import updater  # noqa: E402
from updater import Updater, parse_version, swap  # noqa: E402


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def release_zip(files):
    """A release zip as make_release.bat builds it: everything under one NAI-Style-Lab/ folder."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, data in files.items():
            zf.writestr(f'NAI-Style-Lab/{name}', data)
    return buf.getvalue()


FULL = {'main.py': 'new main', 'app/updater.py': 'new updater', 'app/engine.py': 'new engine',
        'web/index.html': 'new page', 'python/pythonw.exe': 'new python', 'run.bat': 'new run', 'data/': ''}


def github(blob, tag='v4.1.0', digest=True, size=None, asset_name=updater.ASSET, status=None, body='notes'):
    """A stand-in for GitHub: the latest-release JSON, then the asset."""
    calls = []

    def fetch(url, accept):
        calls.append(url)
        if url.startswith('https://api.github.com/'):
            if status:
                raise urllib.error.HTTPError(url, status, 'x', {}, None)
            asset = {'name': asset_name, 'browser_download_url': 'https://github.com/o/r/releases/download/x/z.zip',
                     'size': len(blob) if size is None else size}
            if digest:
                asset['digest'] = 'sha256:' + (hashlib.sha256(blob).hexdigest() if digest is True else digest)
            return Reply(json.dumps({'tag_name': tag, 'assets': [asset], 'body': body,
                                     'html_url': 'https://github.com/o/r/releases/tag/' + tag}).encode())
        return Reply(blob)
    fetch.calls = calls
    return fetch


def wait_state(up, states, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if up.view()['state'] in states and (up.view()['state'] != 'available' or up.view()['error']):
            return up.view()
        time.sleep(0.02)
    raise AssertionError(f'stuck in {up.view()}')


def checks_and_installs(tmp):
    assert parse_version('v4.10.2') == (4, 10, 2) and parse_version('4.0.0') == (4, 0, 0)
    assert parse_version('v4.1') is None and parse_version('latest') is None and parse_version(None) is None
    assert parse_version('v4.10.0') > parse_version('v4.9.9'), 'compared as numbers, not text'

    root = tmp / 'app'
    (root / 'data').mkdir(parents=True)
    blob = release_zip(FULL)

    # ---- the check
    up = Updater(root, '4.0.0', 'o/r', fetch=github(blob))
    view = up.check()
    assert view['state'] == 'available' and view['latest'] == '4.1.0' and view['notes'] == 'notes'
    assert view['page'] == 'https://github.com/o/r/releases/tag/v4.1.0' and view['blocked'] is None
    assert Updater(root, '4.1.0', 'o/r', fetch=github(blob)).check()['state'] == 'latest'
    assert Updater(root, '4.2.0', 'o/r', fetch=github(blob)).check()['state'] == 'latest', 'never a downgrade'
    view = Updater(root, '4.0.0', 'o/r', fetch=github(blob, status=404)).check()
    assert view['state'] == 'idle' and '아직 배포된' in view['error']
    view = Updater(root, '4.0.0', 'o/r', fetch=github(blob, asset_name='other.zip')).check()
    assert view['state'] == 'idle' and '설치 파일' in view['error'] and view['page']
    view = Updater(root, '4.0.0', 'o/r', fetch=github(blob, tag='nightly')).check()
    assert view['state'] == 'idle' and view['error']

    def offline(url, accept):
        raise OSError('no network')
    view = Updater(root, '4.0.0', 'o/r', fetch=offline).check()
    assert view['state'] == 'idle' and '인터넷' in view['error']

    def untrusted(url, accept):  # an antivirus scanning HTTPS, say
        raise urllib.error.URLError(ssl.SSLCertVerificationError(1, 'certificate verify failed'))
    view = Updater(root, '4.0.0', 'o/r', fetch=untrusted).check()
    assert view['state'] == 'idle' and '인증서' in view['error'] and '인터넷' not in view['error']
    try:
        Updater(root, '4.0.0', 'o/r', fetch=offline).install(lambda: None)
        raise AssertionError('nothing to install')
    except updater.UpdateError:
        pass

    # ---- a source checkout never installs a release over itself
    (root / '_dev').mkdir()
    dev = Updater(root, '4.0.0', 'o/r', fetch=github(blob))
    assert dev.check()['blocked'] and dev.view()['state'] == 'available'
    try:
        dev.install(lambda: None)
        raise AssertionError('blocked in a source folder')
    except updater.UpdateError as exc:
        assert '소스' in str(exc)
    (root / '_dev').rmdir()

    # ---- download, unpack, start the swap, then close the app
    launched, closed = [], threading.Event()
    up = Updater(root, '4.0.0', 'o/r', fetch=github(blob), launch=launched.append)
    up.check()
    up.install(closed.set)
    assert closed.wait(10), up.view()
    args = launched[0]
    work = root / updater.WORK
    assert args[0] == str(work / 'runner' / 'python' / 'pythonw.exe') and args[1] == str(work / 'runner' / 'updater.py')
    assert (work / 'runner' / 'updater.py').read_text() == 'new updater', 'the new release swaps itself in'
    assert dict(zip(args[2::2], args[3::2])) == {'--root': str(root), '--new': str(work / 'new' / 'NAI-Style-Lab'),
                                                 '--pid': str(os.getpid())}
    assert up.view()['state'] == 'restarting'
    Updater(root, '4.1.0', 'o/r').clean()  # what the next launch does
    assert not work.exists()

    # ---- a broken download never gets as far as the swap
    for fetch, why in ((github(blob, digest='0' * 64), '체크섬'), (github(blob, size=len(blob) + 5), '끊겼'),
                       (github(release_zip({'main.py': 'x'})), '온전히'), (github(b'not a zip'), 'zip')):
        launched.clear()
        up = Updater(root, '4.0.0', 'o/r', fetch=fetch, launch=launched.append)
        up.check()
        up.install(lambda: None)
        view = wait_state(up, ('available',))
        assert why in view['error'] and not launched and not work.exists(), (why, view)
    up = Updater(root, '4.0.0', 'o/r', fetch=github(blob), launch=launched.append)
    up.check()
    up.install(lambda: None)
    try:
        up.install(lambda: None)
        raise AssertionError('one install at a time')
    except updater.UpdateError:
        pass
    wait_state(up, ('restarting',))
    Updater(root, '4.1.0', 'o/r').clean()


def swaps(tmp):
    def tree(base, files):
        for name, text in files.items():
            (base / name).parent.mkdir(parents=True, exist_ok=True)
            (base / name).write_text(text)

    old = {'main.py': 'old main', 'app/engine.py': 'old engine', 'app/server.py': 'gone in the new one',
           'web/index.html': 'old page', 'python/pythonw.exe': 'old python', 'data/state.json': 'mine',
           'data/images/a.png': 'mine'}
    new = {'main.py': 'new main', 'app/engine.py': 'new engine', 'web/index.html': 'new page',
           'python/pythonw.exe': 'new python', 'data/README': 'release data must not land'}

    root, new_root = tmp / 'root', tmp / 'new'
    tree(root, old)
    tree(new_root, new)
    swap(root, new_root, tmp / 'old')
    assert (root / 'main.py').read_text() == 'new main' and (root / 'python' / 'pythonw.exe').read_text() == 'new python'
    assert not (root / 'app' / 'server.py').exists(), 'a folder is replaced whole: no stale files'
    assert (root / 'data' / 'state.json').read_text() == 'mine' and (root / 'data' / 'images' / 'a.png').exists()
    assert not (root / 'data' / 'README').exists(), 'data/ is never touched'

    # A move that fails part-way: every move undone, the old version whole again.
    root2, new2 = tmp / 'root2', tmp / 'new2'
    tree(root2, old)
    tree(new2, new)
    real = updater._move
    moves = []

    def flaky(source, target):
        moves.append((source, target))
        if Path(source).name == 'python' and Path(source).parent == new2:
            raise PermissionError('in use')
        real(source, target)
    updater._move = flaky
    try:
        swap(root2, new2, tmp / 'old2')
        raise AssertionError('the failure is raised')
    except PermissionError:
        pass
    finally:
        updater._move = real
    for name, text in old.items():
        assert (root2 / name).read_text() == text, name
    assert (new2 / 'main.py').read_text() == 'new main' and len(moves) > 3

    # The swap process: waits for the app to end, swaps, starts the app (on a failure, the old one).
    started, told = [], []
    real_popen, real_tell = updater.subprocess.Popen, updater.tell
    updater.subprocess.Popen = lambda args, **kw: started.append(args)
    updater.tell = told.append
    try:
        root3, new3 = tmp / 'root3', tmp / 'new3'
        tree(root3, old)
        tree(new3, new)
        updater.main(['--root', str(root3), '--new', str(new3), '--pid', '999999'])  # already gone
        assert (root3 / 'main.py').read_text() == 'new main' and not told
        assert started == [[str(root3 / 'python' / 'pythonw.exe'), str(root3 / 'main.py')]]
        root4, new4 = tmp / 'root4', tmp / 'new4'
        tree(root4, old)
        tree(new4, {'main.py': 'new'})
        (new4 / 'web').mkdir()
        updater._move = lambda s, t: (_ for _ in ()).throw(PermissionError('locked')) if Path(s).name == 'web' else real(s, t)
        updater.main(['--root', str(root4), '--new', str(new4), '--pid', '999999'])
        assert told and (root4 / 'main.py').read_text() == 'old main' and len(started) == 2, 'old version back'
        assert not updater.wait_for_exit(os.getpid(), 0.1), 'a running app is waited for'
    finally:
        updater.subprocess.Popen, updater.tell, updater._move = real_popen, real_tell, real


def data_formats(tmp):
    """state.json carries its format; older data is kept as it was, then upgraded; newer data is never opened."""
    real_schema, real_upgrades = core.SCHEMA, dict(core.UPGRADES)
    try:
        state_file = tmp / 'd' / 'state.json'
        state_file.parent.mkdir(parents=True)
        assert core.load_state(state_file)['schema'] == core.SCHEMA, 'a new state carries the format'
        state_file.write_text(json.dumps({**core.empty_state(), 'schema': None}), encoding='utf-8')
        assert core.load_state(state_file).get('load_error'), 'a broken format number: set aside like a broken file'
        old = {k: v for k, v in core.empty_state().items() if k != 'schema'}  # saved before formats were numbered
        old['combinations'] = [{'id': 'a', 'elo': 1000}]
        state_file.write_text(json.dumps(old), encoding='utf-8')
        assert core.load_state(state_file)['schema'] == 1 and real_schema == 1

        # A release with format 3: format-1 data goes through both steps, in order, and the original is kept.
        core.SCHEMA = 3
        core.UPGRADES[1] = lambda d: [c.update(elo=c['elo'] * 10) for c in d['combinations']]
        core.UPGRADES[2] = lambda d: [c.update(seen=c['elo']) for c in d['combinations']]
        state_file.write_text(json.dumps(old), encoding='utf-8')
        data = core.load_state(state_file)
        assert data['schema'] == 3 and data['combinations'] == [{'id': 'a', 'elo': 10000, 'seen': 10000}]
        assert json.loads(state_file.read_text(encoding='utf-8'))['schema'] == 3, 'saved upgraded'
        kept = list(state_file.parent.glob('state.schema1-*.json'))
        assert len(kept) == 1 and json.loads(kept[0].read_text(encoding='utf-8')) == old
        again = core.load_state(state_file)
        assert again['combinations'][0]['elo'] == 10000 and len(list(state_file.parent.glob('state.schema*'))) == 1

        # Data from a newer release: refused, and the file left exactly as it was.
        newer = json.dumps({**old, 'schema': 4})
        set_aside = len(list(state_file.parent.glob('state.corrupt*')))
        state_file.write_text(newer, encoding='utf-8')
        try:
            core.load_state(state_file)
            raise AssertionError('newer data must not open')
        except core.NewerDataError:
            pass
        assert state_file.read_text(encoding='utf-8') == newer and len(list(state_file.parent.glob('state.corrupt*'))) == set_aside
        # An export is upgraded (or refused) the same way.
        assert core.upgrade_state({'schema': 2, 'combinations': [{'elo': 5}]}) == 2
        try:
            core.upgrade_state({'schema': 9})
            raise AssertionError('newer')
        except core.NewerDataError:
            pass
    finally:
        core.SCHEMA = real_schema
        core.UPGRADES.clear()
        core.UPGRADES.update(real_upgrades)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        checks_and_installs(Path(tmp) / 'a')
        swaps(Path(tmp) / 'b')
        data_formats(Path(tmp) / 'c')
    print('PASS: versions, update check, download checks, all-or-nothing swap (data untouched), data format upgrades.')


if __name__ == '__main__':
    main()
