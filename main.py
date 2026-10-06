"""NAI Style Lab — show the app in its own window (pywebview / WebView2). No server: nothing listens on a port.

The window and the engine live in this one process: closing the window saves and ends the app.
"""
import ctypes
import ctypes.wintypes
import json
import math
import os
import secrets
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import zlib
from multiprocessing.connection import Client, Listener
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'app'))

from bridge import App, open_external  # noqa: E402
from core import NewerDataError  # noqa: E402
from engine import Engine  # noqa: E402
from updater import Updater  # noqa: E402
from version import REPO, VERSION  # noqa: E402

DATA_DIR = ROOT / 'data'
LOCK_FILE = DATA_DIR / '.instance.json'
LEGACY_LOCK = '.server.json'  # next to LOCK_FILE, from the versions that ran a local server
# Where the window's page lives. The name never resolves (.invalid, RFC 2606): every request to it is answered
# from disk inside the window (serve_files), so nothing goes over any network.
ORIGIN = 'https://nai-style-lab.invalid'
ICON = ROOT / 'web' / 'icon.ico'
# Per folder: Windows keeps a shortcut per ID with the paths of its first launch, so a moved or renamed folder
# would get a blank taskbar icon (the old icon path) under a fixed ID.
APP_ID = f'NAIStyleLab.App.{zlib.crc32(str(ROOT).lower().encode()):08x}'


def code_version():
    """Changes whenever the app's code changes, so a running app with old code can be replaced."""
    files = [ROOT / 'main.py', *(ROOT / 'app').glob('*.py'), *(ROOT / 'web').rglob('*')]
    return str(max(f.stat().st_mtime_ns for f in files if f.is_file()))


def tell(text, icon=0x40):  # MB_ICONINFORMATION; 0x10 is MB_ICONERROR
    ctypes.windll.user32.MessageBoxW(None, text, 'NAI Style Lab', icon | 0x10000)  # MB_SETFOREGROUND


def _is_app_process(pid):
    """True while ``pid`` is a running Python (the app runs as pythonw.exe), so a reused pid is not mistaken for it."""
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    handle = ctypes.wintypes.HANDLE(handle)
    try:
        code, size = ctypes.wintypes.DWORD(), ctypes.wintypes.DWORD(32768)
        name = ctypes.create_unicode_buffer(size.value)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
            return False
        if not kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            return False
        return Path(name.value).name.lower().startswith('python')
    finally:
        kernel32.CloseHandle(handle)


def legacy_running():
    """True while a version with a local server runs on this data folder: it cannot be asked to close, and two
    apps must never write the same data. Its lock file is dropped once it is gone (it crashed, say)."""
    legacy = LOCK_FILE.with_name(LEGACY_LOCK)
    try:
        pid = json.loads(legacy.read_text(encoding='utf-8'))['pid']
        if _is_app_process(pid):
            return True
    except FileNotFoundError:
        return False
    except Exception:  # unreadable: as good as gone
        pass
    legacy.unlink(missing_ok=True)
    return False


class Instance:
    """Answers a second launch over a named pipe: no network, and only a holder of the key (in the lock file,
    which only this Windows user can read) gets through. ``ping`` says it is running, ``bye`` closes it."""

    def __init__(self, app):
        self.address = rf'\\.\pipe\nai-style-lab-{uuid.uuid4().hex}'
        self.key = secrets.token_bytes(32)
        self.closed = threading.Event()
        self.listener = Listener(self.address, 'AF_PIPE', authkey=self.key)
        threading.Thread(target=self._serve, args=(app,), daemon=True).start()

    def _serve(self, app):
        while not self.closed.is_set():
            try:
                with self.listener.accept() as conn:
                    if self.closed.is_set() or not conn.poll(5):
                        continue
                    message = conn.recv_bytes(16)
                    if message == b'ping':
                        reply = {'ok': True}
                    elif message == b'bye':
                        reply = app.retire()
                    else:
                        reply = {'error': 'unknown'}
                    conn.send_bytes(json.dumps(reply).encode())
            except Exception:  # a caller without the key, or one that hung up: wait for the next
                time.sleep(0.05)

    def lock_info(self):
        return {'pipe': self.address, 'key': self.key.hex(), 'pid': os.getpid(), 'version': code_version()}

    def close(self):
        self.closed.set()
        try:
            self.listener.close()
        except OSError:
            pass


def _ask(info, message, timeout=5):
    """Send ``message`` to the running app; its reply, or None if it is not there (or does not answer)."""
    reply = []

    def talk():
        try:
            with Client(info['pipe'], 'AF_PIPE', authkey=bytes.fromhex(info['key'])) as conn:
                conn.send_bytes(message)
                if conn.poll(timeout):
                    reply.append(json.loads(conn.recv_bytes(4096)))
        except Exception:
            pass

    worker = threading.Thread(target=talk, daemon=True)
    worker.start()
    worker.join(timeout + 1)  # a hung app never hangs this launch
    return reply[0] if reply else None


def hand_over():
    """True if the app is already running for this data folder: this launch then says so and opens nothing
    (two apps on one data folder would overwrite each other's saves).

    One running older code closes instead (unless it is generating), so this launch starts on the new code.
    """
    if legacy_running():
        tell('이전 버전의 NAI Style Lab이 실행 중입니다.\n그 창을 닫은 뒤 다시 실행해 주세요.')
        return True
    try:
        info = json.loads(LOCK_FILE.read_text(encoding='utf-8'))
    except Exception:
        return False
    if not isinstance(info, dict):
        return False
    if info.get('version') != code_version():
        reply = _ask(info, b'bye')
        if reply is None:  # not running: a lock file left by a crash
            return False
        if reply.get('closing'):
            for _ in range(100):  # it saves and removes the lock file as it goes
                if not LOCK_FILE.exists():
                    return False
                time.sleep(0.1)
    if _ask(info, b'ping') is None:  # not running: a lock file left by a crash
        return False
    tell('NAI Style Lab이 이미 실행 중입니다.')
    return True


class _PropertyKey(ctypes.Structure):
    _fields_ = [('fmtid', ctypes.c_byte * 16), ('pid', ctypes.c_ulong)]


class _PropVariant(ctypes.Structure):  # VT_LPWSTR only
    _fields_ = [('vt', ctypes.c_ushort), ('reserved', ctypes.c_ushort * 3), ('value', ctypes.c_wchar_p),
                ('pad', ctypes.c_void_p)]


def name_taskbar_button(hwnd):
    """Give the window's taskbar button the app's name, icon and launch command.

    Without them, its right-click menu says "Python" (pythonw.exe's description), and pinning it pins a bare pythonw.exe.
    """
    def guid(text):
        return (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(text).bytes_le)

    ctypes.windll.ole32.CoInitialize(None)
    store = ctypes.c_void_p()
    if ctypes.windll.shell32.SHGetPropertyStoreForWindow(ctypes.c_void_p(hwnd), ctypes.byref(guid(
            '886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99')), ctypes.byref(store)):  # IID_IPropertyStore
        return
    vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_PropertyKey),
                                   ctypes.POINTER(_PropVariant))(vtable[6])
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    # PKEY_AppUserModel_ID (5), _RelaunchCommand (2), _RelaunchIconResource (3), _RelaunchDisplayNameResource (4)
    for pid, value in ((5, APP_ID), (2, f'"{pythonw}" "{ROOT / "main.py"}"'), (3, f'{ICON},0'), (4, 'NAI Style Lab')):
        key = _PropertyKey(guid('9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3'), pid)
        set_value(store, ctypes.byref(key), ctypes.byref(_PropVariant(vt=31, value=value)))  # 31 = VT_LPWSTR
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(store)  # Release


# The page (the window's inside) on opening: 1502 is the narrowest page with 8 tiles a row on 그림체
# (248 sidebar + 64 padding + 10 x 110 tiles and preview + 9 x 10 gaps); the height makes it 16:10.
PAGE_SIZE = (1502, 939)
FRAME = (14, 38)  # border and title bar at 125% DPI: the first guess, made exact by fit_page once the window is shown


def fit_page(hwnd, width, height):
    """Resize the window so the page inside (its client area) is ``width`` x ``height`` CSS pixels, whatever the
    frame and DPI."""
    user32 = ctypes.windll.user32
    outer, client = ctypes.wintypes.RECT(), ctypes.wintypes.RECT()
    user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(outer))
    user32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(client))
    scale = user32.GetDpiForWindow(ctypes.c_void_p(hwnd)) / 96
    # Rounded up: never under the size asked for (125% does not divide every width evenly).
    outer_w = math.ceil(width * scale) + (outer.right - outer.left) - client.right
    outer_h = math.ceil(height * scale) + (outer.bottom - outer.top) - client.bottom
    user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 0, 0, outer_w, outer_h, 0x0002 | 0x0004)  # NOMOVE | NOZORDER


def serve_files(window, app):
    """Answer every request the window makes to ORIGIN from disk (app.resource), and keep the window there:
    a link elsewhere opens in the user's browser instead (the page's calls into Python are for this page only)."""
    from Microsoft.Web.WebView2.Core import CoreWebView2WebResourceContext
    from System.IO import File, MemoryStream

    def respond(core, args):
        url = ''
        try:
            url = str(args.Request.Uri)
            if not url.startswith(ORIGIN + '/'):
                return
            found = app.resource(urllib.parse.urlsplit(url).path) if args.Request.Method == 'GET' else None
            if not found:
                args.Response = core.Environment.CreateWebResourceResponse(None, 404, 'Not Found', 'Cache-Control: no-store')
                return
            path, ctype, cache = found
            # Read whole, in .NET: no file stays open (a picture can then be deleted at any time).
            body = MemoryStream(File.ReadAllBytes(str(path)))
            args.Response = core.Environment.CreateWebResourceResponse(
                body, 200, 'OK', f'Content-Type: {ctype}\r\nCache-Control: {cache}')
        except Exception:
            app.log(f'resource {url}\n{traceback.format_exc()}')
            try:
                args.Response = core.Environment.CreateWebResourceResponse(None, 500, 'Error', 'Cache-Control: no-store')
            except Exception:
                pass

    def stay(core, args):
        url = str(args.Uri)
        if url.startswith(ORIGIN + '/'):
            return
        args.Cancel = True
        if url.startswith(('https://', 'http://')):
            threading.Thread(target=open_external, args=(url,), daemon=True).start()

    def ready(control, args):
        if not args.IsSuccess:
            return
        core = control.CoreWebView2
        core.AddWebResourceRequestedFilter(ORIGIN + '/*', CoreWebView2WebResourceContext.All)
        core.WebResourceRequested += respond
        core.NavigationStarting += stay

    # Added on the window's thread right after the control is made, so it runs before the first page is asked for.
    window.native.webview.CoreWebView2InitializationCompleted += ready


def open_window(app):
    """Show the app window and block until it is closed. Gives ``app`` its Save As, Open, close and drag."""
    import webview

    # screen= places it at the screen's centre: pywebview's own centring is ignored, so Windows would cascade each
    # launch further down and right.
    # _dev\run_min_width.bat sets NAI_PAGE_WIDTH to check the narrowest layout.
    page_w, page_h = int(os.environ.get('NAI_PAGE_WIDTH') or PAGE_SIZE[0]), PAGE_SIZE[1]
    window = webview.create_window('NAI Style Lab', ORIGIN + '/', width=page_w + FRAME[0], height=page_h + FRAME[1],
                                   text_select=True, screen=webview.screens[0])
    window.expose(app.call)  # the page's one way into Python: window.pywebview.api.call (web/js/api.js)

    def file_dialog(kind, **options):
        picked = window.create_file_dialog(kind, file_types=('zip 파일 (*.zip)',), **options)
        if not picked:
            return None
        return picked if isinstance(picked, str) else picked[0]

    def start_drag(path):
        # A real file drag (Explorer, chat apps, editors take it), started on the window's thread while the button
        # is still held: the page cancels its own drag of the picture and asks for this one.
        from System import Action, Array, String
        from System.Windows.Forms import DataFormats, DataObject, DragDropEffects
        form = window.native
        data = DataObject(DataFormats.FileDrop, Array[String]([str(path)]))

        def drag():
            # Only while the left button is still down: started after it was let go, the drag would hang the
            # window until the next click and drop the file wherever that is.
            if ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000:  # VK_LBUTTON
                form.DoDragDrop(data, DragDropEffects.Copy)

        form.BeginInvoke(Action(drag))

    app.save_dialog = lambda name: file_dialog(webview.FileDialog.SAVE, save_filename=name)
    app.open_dialog = lambda: file_dialog(webview.FileDialog.OPEN)
    app.close_window, app.start_drag = window.destroy, start_drag
    window.events.before_show += lambda: serve_files(window, app)
    window.events.shown += lambda: name_taskbar_button(window.native.Handle.ToInt64())
    window.events.shown += lambda: fit_page(window.native.Handle.ToInt64(), page_w, page_h)
    # Own taskbar entry with the app's icon, not grouped under Python.
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    webview.start(icon=str(ICON))


def main():
    DATA_DIR.mkdir(exist_ok=True)
    if hand_over():
        return
    try:
        engine = Engine(DATA_DIR, auto_subscription=True)
    except NewerDataError:  # left as it is: this older release would lose what it does not know
        tell('이 데이터는 더 새 버전의 NAI Style Lab에서 저장되었습니다.\n최신 버전을 받아 실행해 주세요.')
        return
    app = App(engine, DATA_DIR / 'app.log')
    app.updater = updater = Updater(ROOT, VERSION, REPO)
    instance = Instance(app)
    LOCK_FILE.write_text(json.dumps(instance.lock_info()), encoding='utf-8')
    threading.Thread(target=app.thumbnail_worker, daemon=True).start()
    # What the last update left behind goes; then one look for a newer release (the pages show it when there is).
    threading.Thread(target=lambda: (updater.clean(), updater.check()), daemon=True).start()
    engine.refresh_subscription()  # the sidebar shows it from the start
    try:
        open_window(app)
    finally:
        try:
            app.shutdown()  # stops a running generation, keeps unsaved typing, saves
        finally:
            instance.close()
            LOCK_FILE.unlink(missing_ok=True)


def launch():
    """main(), but a launch that fails says why: pythonw.exe has no console, so it would just vanish. The message
    box takes Ctrl+C, and the whole traceback goes to data\\app.log for a bug report."""
    try:
        main()
    except Exception:
        details = traceback.format_exc()
        try:
            DATA_DIR.mkdir(exist_ok=True)
            with open(DATA_DIR / 'app.log', 'a', encoding='utf-8') as fh:
                fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] launch failed\n{details}\n")
            where = '자세한 내용은 data\\app.log에 저장했습니다.\n문의하실 때 이 창을 캡처하거나(Ctrl+C로 복사) app.log 파일을 보내 주세요.'
        except OSError:  # a folder it cannot write to (Program Files, say): the message is all there is
            where = '문의하실 때 이 창을 캡처하거나 Ctrl+C로 복사해 보내 주세요.'
        tell(f'NAI Style Lab을 시작하지 못했습니다.\n\n{details.strip().splitlines()[-1]}\n\n{where}', icon=0x10)


if __name__ == '__main__':
    launch()
