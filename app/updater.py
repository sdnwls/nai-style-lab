"""Updates from GitHub Releases: check, download, then swap the app's files while it is closed.

data/ is never touched: a release that changes the data format upgrades it at its first launch (core.load_state,
which keeps the old file). The swap runs as its own process, ``python updater.py --root .. --new .. --pid ..``,
from a copy of the new release's Python, so every file of the old app (its Python too) can be replaced; if any
move fails, every one already made is undone and the old version starts again.
"""
import ctypes
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ASSET = 'NAI-Style-Lab.zip'  # the release zip's name (built by _dev\make_release.bat)
KEEP = {'data'}  # never replaced
NEEDED = ('main.py', 'app/updater.py', 'web/index.html', 'python/pythonw.exe')  # what a release zip must hold
WORK = '.update'  # next to data/: the download, the unpacked release, the old files (removed at the next launch)
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: outlives the app, no console


class UpdateError(Exception):
    """Shown to the user as it is."""


def parse_version(text):
    """'v4.1.0' or '4.1.0' -> (4, 1, 0); anything else -> None."""
    parts = str(text or '').strip().removeprefix('v').removeprefix('V').split('.')
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _fetch(url, accept):
    request = urllib.request.Request(url, headers={'Accept': accept, 'User-Agent': 'NAI-Style-Lab-updater'})
    return urllib.request.urlopen(request, timeout=20)


def _launch(args):
    subprocess.Popen(args, creationflags=DETACHED, close_fds=True, cwd=str(Path(args[1]).parent))


def tell(text):
    ctypes.windll.user32.MessageBoxW(None, text, 'NAI Style Lab 업데이트', 0x40)  # MB_ICONINFORMATION


class Updater:
    def __init__(self, root, version, repo, fetch=_fetch, launch=_launch):
        self.root = Path(root)
        self.version = version
        self.repo = repo
        self.work = self.root / WORK
        self.fetch, self.launch = fetch, launch
        self.lock = threading.Lock()
        # state: idle → checking → latest | available → downloading → preparing → restarting (or back, with error)
        self.info = {'current': version, 'state': 'idle', 'latest': None, 'notes': '', 'page': None,
                     'error': None, 'progress': None}
        self.asset = None

    def blocked(self):
        """Why this folder cannot install updates, or None. A source checkout is updated with git, never by
        dropping a release over it."""
        if (self.root / '_dev').exists() or (self.root / '.git').exists():
            return '소스(개발) 폴더에서는 업데이트를 설치하지 않습니다. 배포 zip으로 실행한 앱에서 설치하세요.'
        return None

    def view(self):
        with self.lock:
            return {**self.info, 'blocked': self.blocked()}

    def _set(self, **changes):
        with self.lock:
            self.info.update(changes)

    def check(self):
        """Ask GitHub for the latest release; the view afterwards (an error is in it, not raised)."""
        with self.lock:
            if self.info['state'] in ('downloading', 'preparing', 'restarting'):
                return {**self.info, 'blocked': self.blocked()}
            self.info.update(state='checking', error=None)
        try:
            with self.fetch(f'https://api.github.com/repos/{self.repo}/releases/latest', 'application/vnd.github+json') as resp:
                release = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            message = '아직 배포된 버전이 없습니다.' if exc.code == 404 else f'업데이트를 확인하지 못했습니다 (HTTP {exc.code}).'
            self._set(state='idle', error=message)
            return self.view()
        except Exception as exc:
            if isinstance(getattr(exc, 'reason', None), ssl.SSLCertVerificationError):  # not the connection
                message = ('GitHub의 보안 인증서를 확인하지 못했습니다. 백신의 HTTPS(웹) 검사 기능을 끄거나, '
                           '브라우저로 https://github.com 에 한 번 접속한 뒤 다시 시도해 주세요.')
            else:
                message = f'업데이트를 확인하지 못했습니다. 인터넷 연결을 확인해 주세요. ({exc})'
            self._set(state='idle', error=message)
            return self.view()
        tag = release.get('tag_name') if isinstance(release, dict) else None
        latest, current = parse_version(tag), parse_version(self.version)
        asset = next((a for a in release.get('assets') or [] if isinstance(a, dict) and a.get('name') == ASSET), None) \
            if latest else None
        if not latest or not current:
            self._set(state='idle', error='최신 버전 정보를 읽지 못했습니다.')
            return self.view()
        page = release.get('html_url')
        page = page if isinstance(page, str) and page.startswith('https://github.com/') else None
        if latest > current and asset is None:
            self._set(state='idle', page=page, error=f'새 버전 {tag}에 설치 파일({ASSET})이 없습니다.')
            return self.view()
        newer = latest > current
        self.asset = {'url': asset['browser_download_url'], 'size': asset.get('size'), 'digest': asset.get('digest')} \
            if newer else None
        self._set(state='available' if newer else 'latest', latest=f'{latest[0]}.{latest[1]}.{latest[2]}',
                  notes=str(release.get('body') or '')[:4000], page=page)
        return self.view()

    def install(self, on_ready):
        """Download and unpack the new release in the background, start the swap, then ``on_ready()`` (close the
        app: the swap waits for it to end)."""
        with self.lock:
            if self.info['state'] != 'available' or not self.asset:
                raise UpdateError('설치할 새 버전이 없습니다. 업데이트를 다시 확인해 주세요.')
            if self.blocked():
                raise UpdateError(self.blocked())
            self.info.update(state='downloading', error=None, progress=0)
        threading.Thread(target=self._install, args=(on_ready,), daemon=True).start()

    def _install(self, on_ready):
        try:
            shutil.rmtree(self.work, ignore_errors=True)
            self.work.mkdir()
            archive = self.work / ASSET
            self._download(archive)
            self._set(state='preparing', progress=None)
            new_root = self._unpack(archive)
            runner = self.work / 'runner'
            shutil.copytree(new_root / 'python', runner / 'python')  # the swap runs from this copy
            shutil.copy2(new_root / 'app' / 'updater.py', runner / 'updater.py')
            self._set(state='restarting')
            self.launch([str(runner / 'python' / 'pythonw.exe'), str(runner / 'updater.py'),
                         '--root', str(self.root), '--new', str(new_root), '--pid', str(os.getpid())])
        except Exception as exc:
            shutil.rmtree(self.work, ignore_errors=True)
            message = str(exc) if isinstance(exc, UpdateError) else f'업데이트를 준비하지 못했습니다: {exc}'
            self._set(state='available', progress=None, error=message)
            return
        on_ready()

    def _download(self, target):
        expected, digest = self.asset['size'], self.asset['digest']
        sha = hashlib.sha256()
        done = 0
        with self.fetch(self.asset['url'], 'application/octet-stream') as resp, open(target, 'wb') as fh:
            while chunk := resp.read(1 << 16):
                fh.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                self._set(progress=round(done / expected * 100) if expected else None)
        if expected and done != expected:
            raise UpdateError('다운로드가 중간에 끊겼습니다. 다시 시도해 주세요.')
        if isinstance(digest, str) and digest.startswith('sha256:') and digest[7:].lower() != sha.hexdigest():
            raise UpdateError('받은 파일이 배포된 파일과 다릅니다(체크섬 불일치). 다시 시도해 주세요.')

    def _unpack(self, archive):
        """The release's top folder, unpacked and checked."""
        target = self.work / 'new'
        try:
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(target)  # extractall keeps every entry inside target (no absolute paths, no ..)
        except zipfile.BadZipFile:
            raise UpdateError('받은 파일이 올바른 zip이 아닙니다.')
        tops = [p for p in target.iterdir() if p.is_dir()]
        root = tops[0] if len(tops) == 1 and not (target / 'main.py').exists() else target
        missing = [n for n in NEEDED if not (root / n).is_file()]
        if missing:
            raise UpdateError(f'받은 파일에 앱이 온전히 들어 있지 않습니다 ({", ".join(missing)}).')
        return root

    def clean(self):
        """Remove what the last update left behind (the old files, the runner); retried later if still in use."""
        for _ in range(30):
            if self.info['state'] in ('downloading', 'preparing', 'restarting'):
                return  # a new update has started there
            shutil.rmtree(self.work, ignore_errors=True)
            if not self.work.exists():
                return
            time.sleep(2)


# ---------------------------------------------------------------- the swap (its own process)
def _move(source, target):
    for attempt in range(40):  # antivirus or Explorer can hold a file for a moment
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.25)


def swap(root, new_root, old_dir):
    """Move each item of the release into ``root``, the old one aside into ``old_dir``. All or nothing: if one
    move fails, every move already made is undone and the error is raised."""
    shutil.rmtree(old_dir, ignore_errors=True)
    old_dir.mkdir(parents=True)
    done = []
    try:
        for name in sorted(p.name for p in new_root.iterdir() if p.name not in KEEP):
            target, had = root / name, (root / name).exists()
            if had:
                _move(target, old_dir / name)
            try:
                _move(new_root / name, target)
            except Exception:
                if had:
                    _move(old_dir / name, target)
                raise
            done.append((name, had))
    except Exception:
        for name, had in reversed(done):
            _move(root / name, new_root / name)
            if had:
                _move(old_dir / name, root / name)
        raise


def wait_for_exit(pid, seconds):
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return True  # already gone
    try:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), int(seconds * 1000)) == 0  # WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def main(argv):
    args = dict(zip(argv[::2], argv[1::2]))
    root, new_root, pid = Path(args['--root']), Path(args['--new']), int(args['--pid'])
    app = [str(root / 'python' / 'pythonw.exe'), str(root / 'main.py')]
    if not wait_for_exit(pid, 120):
        tell('앱이 닫히지 않아 업데이트를 설치하지 못했습니다. 앱을 닫은 뒤 설정 → 데이터에서 다시 시도해 주세요.')
        return
    try:
        swap(root, new_root, root / WORK / 'old')
    except Exception as exc:
        tell(f'업데이트를 설치하지 못해 이전 버전으로 다시 실행합니다.\n{exc}')
    subprocess.Popen(app, creationflags=DETACHED, close_fds=True, cwd=str(root))


if __name__ == '__main__':
    main(sys.argv[1:])
