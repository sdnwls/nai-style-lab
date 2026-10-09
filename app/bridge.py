"""The app window's link to :class:`engine.Engine`: no server, no network.

The page calls :meth:`App.call` through pywebview's own channel, and its files (the page itself, the
pictures) are handed to WebView2 straight from disk by main.py through :meth:`App.resource`. Nothing
listens on a port, so no browser and no other program can reach the app.
"""
import json
import mimetypes
import os
import queue
import threading
import time
import traceback
import urllib.parse
from datetime import datetime
from pathlib import Path

import core
from engine import SETTING_RANGES, Engine, UserError
from updater import UpdateError
from version import VERSION

WEB_DIR = Path(__file__).resolve().parent.parent / 'web'
THUMB_SIZE = (440, 760)
# Never cached: a picture whose thumbnail is still being made, or the page's own files.
NO_STORE, CACHED = 'no-store', 'max-age=31536000, immutable'


class NotFound(Exception):
    """Unknown route (kept apart from KeyError/IndexError raised inside handlers)."""


def open_external(target):
    """Open a file, folder or URL with its usual program. The app window belongs to this process, so what
    it starts may come to the front."""
    os.startfile(target)


mimetypes.add_type('text/javascript', '.js')
mimetypes.add_type('text/css', '.css')
mimetypes.add_type('image/webp', '.webp')
mimetypes.add_type('image/svg+xml', '.svg')


class App:
    def __init__(self, engine: Engine, log_file: Path | None = None):
        self.engine = engine
        # Set by main.py from the app window: Save As (zip name -> path or None), Open (-> path or None), close,
        # and a file drag out of the window (path). updater: an updater.Updater.
        self.save_dialog = self.open_dialog = self.close_window = self.start_drag = None
        self.updater = None
        self.log_file = log_file
        self.thumb_lock = threading.Lock()  # held while one is made
        self.thumb_queue = queue.Queue()
        self.thumb_waiting = set()  # queued and not made yet; its own lock, so the window never waits on a making
        self.waiting_lock = threading.Lock()
        self.picked_import = None  # the zip chosen in Open, until it is imported
        # Settings typed but not saved yet, kept by the page as it goes ((seq, changes): the newest wins),
        # so closing the window mid-typing still saves them (see shutdown).
        self.draft = (-1, {})

    def log(self, text):
        if self.log_file:
            with open(self.log_file, 'a', encoding='utf-8') as fh:
                fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")

    # ---------------------------------------------------------------- the page's calls
    def call(self, method, path, body=None):
        """One request from the page: ``{ok, data}`` or ``{ok: false, error}``, as JSON text (so anything the
        page cannot read is an error here, not a broken reply)."""
        try:
            url = urllib.parse.urlsplit(path)
            if method == 'GET':
                data = self.get(url.path, dict(urllib.parse.parse_qsl(url.query)))
            elif method == 'POST':
                data = self.post(url.path, body if isinstance(body, dict) else {})
            else:
                raise NotFound(path)
            return json.dumps({'ok': True, 'data': data}, ensure_ascii=False)
        except (UserError, UpdateError) as exc:
            return json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False)
        except NotFound:
            return json.dumps({'ok': False, 'error': f'알 수 없는 요청입니다: {path}'}, ensure_ascii=False)
        except Exception as exc:
            self.log(f'{method} {path}\n{traceback.format_exc()}')
            return json.dumps({'ok': False, 'error': f'내부 오류가 났습니다: {exc}'}, ensure_ascii=False)

    def get(self, path, query):
        e = self.engine
        routes = {
            # update: the updater's state, so every page can show that a new version is out
            '/api/status': lambda: {**e.status(int(query.get('since', 0))),
                                    'update': self.updater.view() if self.updater else None},
            '/api/update': lambda: self.updater.view() if self.updater else None,
            '/api/match': e.match,
            '/api/combos': e.list_combos,
            '/api/artists': e.list_artists,
            '/api/evolution': e.evolution_view,
            '/api/improve': e.improve_view,
            '/api/settings': e.settings_payload,
            '/api/meta': lambda: {'models': core.MODELS, 'sizes': list(core.SIZE_PRESETS),
                                  'samplers': core.SAMPLERS, 'tiers': core.TIER_ORDER,
                                  'data_dir': str(e.data_dir), 'max_seed': core.MAX_SEED, 'version': VERSION,
                                  # the one table of input limits; every field on every page reads it
                                  'ranges': {key: [low, high] for key, (_, low, high) in SETTING_RANGES.items()}},
            '/api/free': lambda: {'results': [{**r, 'url': f"/img/{r['image']}"} for r in e.state['free_results']]},
        }
        if path not in routes:
            raise NotFound(path)
        return routes[path]()

    def post(self, path, body):
        e = self.engine
        ids = body.get('ids', [])
        routes = {
            '/api/vote': lambda: e.vote(body.get('side')),
            '/api/skip': e.skip,
            '/api/undo': e.undo,
            '/api/settings': lambda: e.update_settings(body.get('changes', {})),
            '/api/settings/draft': lambda: self.keep_draft(body.get('seq'), body.get('changes')),
            '/api/subscription': e.check_subscription,
            '/api/artists/add': lambda: {'added': e.add_artists(body.get('text', ''))},
            '/api/artists/scan': lambda: e.scan_artists(body.get('text', '')),
            '/api/artists/rename': lambda: e.rename_artist(body.get('old'), body.get('new')),
            '/api/artists/delete': lambda: e.delete_artists(body.get('tags', [])),
            '/api/generate/random': lambda: e.start_random(body.get('count')),
            '/api/generate/custom': lambda: e.start_custom(body.get('text', '')),
            '/api/generate/free': lambda: e.start_free(body.get('prompt', '')),
            '/api/job/stop': e.stop_job,
            '/api/combos/revive': lambda: {'revived': e.revive(ids)},
            '/api/combos/elo': lambda: e.set_elo(ids, body.get('elo')),
            '/api/combos/delete': lambda: e.delete_combos(ids),
            '/api/combos/purge': lambda: e.purge(body.get('reason')),
            '/api/open': lambda: self.open_image(body.get('id'), body.get('file')),
            '/api/open-folder': lambda: open_external(str(e.data_dir)),
            '/api/drag': lambda: self.drag_image(body.get('file')),
            # Fixed URL only, in the user's own browser where they are signed in to NovelAI.
            '/api/open-novelai': lambda: open_external('https://novelai.net/image'),
            '/api/data/export': lambda: {'path': self.export_data()},
            '/api/data/pick': self.pick_import,
            '/api/data/import': self.import_data,
            '/api/evolution/start': lambda: e.start_evolution(body.get('count')),
            '/api/evolution/finish': e.finish_generation,
            '/api/improve/start': lambda: e.start_improve(body.get('id'), body.get('variants')),
            '/api/improve/next': e.improve_next_round,
            '/api/improve/final': e.improve_final_check,
            '/api/improve/finish': lambda: {'result': e.finish_improve(body.get('choice', 'champion'))},
            '/api/improve/resume': e.improve_resume,
            '/api/reset': lambda: e.reset(body.get('scope')),
            '/api/update/check': lambda: self.updater.check(),
            '/api/update/install': self.install_update,
            '/api/update/page': self.open_release_page,
        }
        if path not in routes:
            raise NotFound(path)
        return routes[path]()

    def install_update(self):
        job = self.engine.job
        if job and job['running']:
            raise UserError('생성 작업이 끝난 뒤 업데이트해 주세요.')
        # Ready: close the window like the user would (main.py saves); the swap starts once the app has ended.
        self.updater.install(on_ready=lambda: (self.close_window or self.shutdown)())

    def open_release_page(self):
        page = self.updater.view()['page']
        if not page:
            raise UserError('릴리스 페이지 주소가 없습니다. 업데이트를 먼저 확인해 주세요.')
        open_external(page)

    def keep_draft(self, seq, changes):
        if isinstance(seq, int) and isinstance(changes, dict) and seq > self.draft[0]:
            self.draft = (seq, changes)

    def open_image(self, combo_id=None, file=None):
        path = self.engine.image_path(combo_id) if combo_id else self.safe_image(file)
        if not path or not path.exists():
            raise UserError('원본 이미지 파일을 찾을 수 없습니다.')
        open_external(str(path))

    def drag_image(self, file):
        """Hand an image to Windows as a file being dragged, while the mouse button is still down in the window."""
        path = self.safe_image(file)
        if not path or not path.exists():
            raise UserError('원본 이미지 파일을 찾을 수 없습니다.')
        if self.start_drag:
            self.start_drag(path)

    def export_data(self):
        """Save As, then write the zip there. None when the dialog was cancelled."""
        path = self.save_dialog(f'nai-style-lab-{datetime.now():%Y%m%d_%H%M%S}.zip')
        if not path:
            return None
        part = Path(path + '.part')  # a failed export never replaces a file that was already there
        try:
            with open(part, 'wb') as fh:
                self.engine.export_data(fh)
            os.replace(part, path)
        except OSError as exc:
            raise UserError(f'파일을 저장할 수 없습니다: {exc}')
        finally:
            part.unlink(missing_ok=True)
        return path

    def pick_import(self):
        """Open, for the zip to import: its name (for the page to confirm), or None when cancelled."""
        path = self.open_dialog()
        self.picked_import = Path(path) if path else None
        return {'name': self.picked_import.name} if self.picked_import else None

    def import_data(self):
        """Import the zip picked in Open (only that one: the page never names a file itself)."""
        path, self.picked_import = self.picked_import, None
        if not path:
            raise UserError('불러올 파일을 먼저 고르세요.')
        try:
            blob = path.read_bytes()
        except OSError as exc:
            raise UserError(f'파일을 읽을 수 없습니다: {exc}')
        return self.engine.import_data(blob)

    def retire(self):
        """A launch with newer code takes over: close now unless generating. Closing the window ends the
        app (main.py then saves); without a window, save directly."""
        job = self.engine.job
        if job and job['running']:
            return {'closing': False}
        threading.Timer(0.1, self.close_window or self.shutdown).start()  # after this reply is sent
        return {'closing': True}

    # ---------------------------------------------------------------- files
    def resource(self, path):
        """What the window shows at ``path`` (the page, its files, the pictures): ``(file, content type,
        cache)``, or None. Only files inside web/, images/ and thumbs/ are ever handed out. ``path`` is as in the
        URL (still %-encoded): decoded once, here or by safe_image."""
        if path in ('/', '/index.html'):
            return self._typed(WEB_DIR / 'index.html', NO_STORE)
        if path.startswith('/static/'):
            target = (WEB_DIR / urllib.parse.unquote(path[len('/static/'):])).resolve()
            return self._typed(target, NO_STORE) if WEB_DIR.resolve() in target.parents else None
        if path.startswith('/img/'):
            return self._typed(self.safe_image(path[len('/img/'):]), CACHED)
        if path.startswith('/thumb/'):
            name = path[len('/thumb/'):]
            source = self.safe_image(name)
            if not source or not source.is_file():
                return None
            thumb = self.engine.thumb_dir / (source.stem + '.webp')
            if thumb.is_file() and thumb.stat().st_mtime >= source.stat().st_mtime:
                return self._typed(thumb, CACHED)
            # A picture made after the window opened: the full image this once (never blocking the window),
            # its thumbnail being made in the background for next time.
            self.queue_thumbnail(name)
            return self._typed(source, NO_STORE)
        return None

    @staticmethod
    def _typed(path, cache):
        if not path or not path.is_file():
            return None
        ctype = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        return path, ctype + ('; charset=utf-8' if ctype.startswith('text/') else ''), cache

    def safe_image(self, name):
        if not name:
            return None
        path = (self.engine.img_dir / urllib.parse.unquote(name)).resolve()
        return path if path.parent == self.engine.img_dir.resolve() and path.suffix.lower() == '.png' else None

    def thumbnail(self, name):
        source = self.safe_image(name)
        if not source or not source.exists():
            return None
        thumb_dir = self.engine.thumb_dir
        target = thumb_dir / (source.stem + '.webp')
        with self.thumb_lock:
            if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
                from PIL import Image
                thumb_dir.mkdir(parents=True, exist_ok=True)
                part = target.with_suffix('.part')  # never a half-written thumbnail where the window reads it
                with Image.open(source) as img:
                    img.thumbnail(THUMB_SIZE, Image.LANCZOS)
                    img.convert('RGB').save(part, 'WEBP', quality=84, method=4)
                os.replace(part, target)
        return target

    def queue_thumbnail(self, name):
        with self.waiting_lock:
            if name in self.thumb_waiting:
                return
            self.thumb_waiting.add(name)
        self.thumb_queue.put(name)

    def thumbnail_worker(self):
        """Make thumbnails one at a time, in the background: every missing one first (so galleries open
        instantly), then each one the window asked for."""
        with self.engine.lock:
            names = [c.get('image_file') for c in self.engine.combos + self.engine.retired]
        for name in names:
            if name:
                self.queue_thumbnail(name)
        while True:
            name = self.thumb_queue.get()
            try:
                self.thumbnail(name)
            except Exception as exc:
                self.log(f'thumbnail {name}: {exc}')
            finally:
                with self.waiting_lock:
                    self.thumb_waiting.discard(name)

    # ---------------------------------------------------------------- lifecycle
    def shutdown(self):
        """Stop (a running generation is stopped, not waited for), keep what was still being typed, save."""
        self.engine.stop_job()
        changes = self.draft[1]
        if changes:
            try:
                self.engine.update_settings(changes)
            except UserError as exc:  # out of range: like any refused change, the saved value stays
                self.log(f'unsaved settings on close: {exc}')
            except Exception:
                self.log(f'unsaved settings on close\n{traceback.format_exc()}')
        self.engine.save()
