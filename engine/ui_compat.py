"""Static Nuxt delivery and projected JSON for historical CLI entry points.

No HTML or browser JavaScript is generated here. Exported pages need an HTTP
server (for example ``python -m http.server --bind 127.0.0.1`` in the export directory).
"""
from pathlib import Path
import json
import shutil

from engine.runtime_ui import RuntimeUI

ROOT = Path(__file__).resolve().parents[1]


def ui_document() -> str:
    result = RuntimeUI(ROOT).read('/')
    if result is None or result[0] != 200:
        raise ValueError('UI_BUILD_UNAVAILABLE')
    return result[1].decode('utf-8')


def serve_ui(handler) -> bool:
    from urllib.parse import urlparse
    result = RuntimeUI(ROOT).read(urlparse(handler.path).path)
    if result is None:
        return False
    status, raw, media, cache = result
    handler.send_response(status)
    handler.send_header('Content-Type', media)
    handler.send_header('Content-Length', str(len(raw)))
    handler.send_header('Cache-Control', cache)
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.end_headers()
    try:
        handler.wfile.write(raw)
    except (BrokenPipeError, ConnectionResetError):
        pass  # A browser may abandon an asset while navigating.
    return True


def export_view(path, view):
    """Copy the verified SPA and write only observer-projected display data."""
    path = Path(path)
    markup = ui_document()
    ui = RuntimeUI(ROOT)
    spec = json.loads((ui.root / 'chombo-ui-manifest.json').read_text())
    # Validate the complete build before touching the requested output.
    for name in spec['files']:
        result = ui.read('/' if name == 'index.html' else '/' + name)
        if result is None or result[0] != 200:
            raise ValueError('UI_BUILD_UNAVAILABLE')
    path.parent.mkdir(parents=True, exist_ok=True)
    for name in spec['files']:
        if name == 'index.html':
            continue
        target = path.parent / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ui.root / name, target)
    path.with_suffix('.view.json').write_text(json.dumps(view, ensure_ascii=False), encoding='utf-8')
    path.write_text(markup, encoding='utf-8')
