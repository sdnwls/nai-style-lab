"""Launch lifecycle with a stand-in window: start, single instance, newer code takes over, close → save and exit,
an older version (one with a local server) still running, and no port opened along the way."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import main  # noqa: E402


def listening_ports():
    """Every TCP port this process listens on (netstat), to prove the app opens none."""
    out = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True, text=True, errors='replace').stdout
    pid = str(os.getpid())
    return {line.split()[1] for line in out.splitlines() if 'LISTENING' in line and line.split()[-1] == pid}


class Offline(main.Updater):
    """Tests never ask GitHub: the launch's own check finds no network."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, fetch=self.offline, **kwargs)

    @staticmethod
    def offline(url, accept):
        raise OSError('offline in tests')


def start(data):
    opened, closed = [], threading.Event()
    main.Updater = Offline

    def fake_window(app):  # what open_window does, minus the real window: hooks, then block until closed
        opened.append(app)
        app.close_window = closed.set
        closed.wait(15)

    main.DATA_DIR = data
    main.LOCK_FILE = data / '.instance.json'
    main.open_window = fake_window
    runner = threading.Thread(target=main.main, daemon=True)
    runner.start()
    for _ in range(100):
        if opened:
            break
        time.sleep(0.05)
    assert opened, 'the window opened'
    return runner, opened[0], closed


def main_test():
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp)
        ports_before = listening_ports()
        told = []
        main.tell = told.append
        runner, app, closed = start(data)
        assert main.LOCK_FILE.exists()
        info = json.loads(main.LOCK_FILE.read_text(encoding='utf-8'))
        assert info['pipe'].startswith('\\\\.\\pipe\\') and len(info['key']) == 64 and 'port' not in info
        assert listening_ports() == ports_before, 'no port: nothing to reach from a browser or another PC'
        assert json.loads(app.call('GET', '/api/status'))['ok'], 'the window talks to the engine directly'

        assert main.hand_over() and told == ['NAI Style Lab이 이미 실행 중입니다.'], 'a second launch says so, opens nothing'
        assert runner.is_alive() and not closed.is_set(), 'the running app carries on'
        wrong = dict(info, key='00' * 32)
        assert main._ask(wrong, b'ping', timeout=1) is None, 'without the key: nothing'
        assert main._ask(info, b'nonsense') == {'error': 'unknown'} and runner.is_alive()

        real_version = main.code_version
        main.code_version = lambda: 'updated'
        started = time.monotonic()
        assert not main.hand_over(), 'a running app with older code closes so the new launch can start'
        assert time.monotonic() - started < 5
        main.code_version = real_version
        assert closed.is_set(), 'it closed its window'
        runner.join(timeout=5)
        assert not runner.is_alive(), 'closing the window ends the app'
        assert not main.LOCK_FILE.exists() and (data / 'state.json').exists(), 'state saved, lock removed'
        assert not main.hand_over()
        assert main._ask(info, b'ping', timeout=1) is None, 'a closed app answers nothing'

        # A lock file left by a crash: the next launch starts as usual.
        main.LOCK_FILE.write_text(json.dumps(info), encoding='utf-8')
        assert not main.hand_over()
        main.LOCK_FILE.write_text('not json', encoding='utf-8')
        assert not main.hand_over()
        main.LOCK_FILE.unlink()

        # An older version (it ran a local server) still open on this data: say so and do not start.
        told.clear()
        legacy = data / main.LEGACY_LOCK
        legacy.write_text(json.dumps({'port': 1, 'pid': os.getpid(), 'token': 'x'}), encoding='utf-8')
        assert main.hand_over() and told and legacy.exists(), 'two apps never write the same data'
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        legacy.write_text(json.dumps({'port': 1, 'pid': dead.pid}), encoding='utf-8')
        assert not main.hand_over() and not legacy.exists() and len(told) == 1, 'gone (crashed): start, drop its lock'
        legacy.write_text(json.dumps({'pid': 4}), encoding='utf-8')  # Windows' System process
        assert not main.hand_over() and len(told) == 1, 'a reused pid that is not Python is not the app'

        # Data saved by a newer release (a newer format): say so, open nothing, leave it as it was.
        state = (data / 'state.json').read_text(encoding='utf-8')
        newer = json.dumps({**json.loads(state), 'schema': 999})
        (data / 'state.json').write_text(newer, encoding='utf-8')
        opened = []
        main.open_window = opened.append
        main.main()
        assert len(told) == 2 and '더 새 버전' in told[1] and not opened and not main.LOCK_FILE.exists()
        assert (data / 'state.json').read_text(encoding='utf-8') == newer
        (data / 'state.json').write_text(state, encoding='utf-8')
        legacy.unlink(missing_ok=True)

        # A second run on the same folder works the same (the pipe name is new each time).
        runner, app, closed = start(data)
        assert main.hand_over() and told[-1] == 'NAI Style Lab이 이미 실행 중입니다.'
        closed.set()
        runner.join(timeout=5)
        assert not runner.is_alive() and not main.LOCK_FILE.exists()
    print('PASS: one window, no port, single instance (says so, opens nothing), newer code takes over, close saves and exits, '
          'older version still open.')


def failed_launch_test():
    """A launch that fails says why in a message box and logs the traceback, instead of vanishing (pythonw)."""
    told = []

    def broken_window(app):
        raise RuntimeError('Failed to resolve Python.Runtime.Loader.Initialize')

    main.tell = lambda text, icon=0x40: told.append((text, icon))
    with tempfile.TemporaryDirectory() as tmp:
        runner, _, closed = start(Path(tmp))  # sets up the stand-ins; that window is closed again
        closed.set()
        runner.join(timeout=5)
        main.open_window = broken_window
        main.launch()
        text, icon = told[-1]
        assert icon == 0x10 and 'RuntimeError: Failed to resolve' in text and 'app.log' in text, told
        assert 'Traceback' in (Path(tmp) / 'app.log').read_text(encoding='utf-8')
        assert not main.LOCK_FILE.exists()
    # Downloaded zips mark every file as from the internet; without this .NET refuses the window's DLLs.
    for exe in ('python', 'pythonw'):
        assert 'loadFromRemoteSources enabled="true"' in (ROOT / 'python' / f'{exe}.exe.config').read_text()
    print('PASS: a failed launch says why and logs it; .NET loads DLLs from a downloaded zip.')


if __name__ == '__main__':
    main_test()
    failed_launch_test()
