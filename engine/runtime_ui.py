"""Allowlisted static SPA assets with explicit failure when the build is absent."""
import hashlib
import json
import mimetypes
import re
from pathlib import Path


class RuntimeUI:
    routes = frozenset({'/', '/index.html', '/live', '/replay', '/analysis', '/conditions', '/rules', '/rules/new'})

    def __init__(self, assets_root):
        self.root = Path(assets_root) / 'ui' / '.output' / 'public'

    def read(self, path):
        """Return status, bytes, media type, cache policy, or None for non-UI routes."""
        page = path in self.routes or re.fullmatch(r'/rules/edit/[0-9a-f-]{36}', path) is not None
        if not page and not path.startswith('/_nuxt/'):
            return None
        manifest = self.root / 'chombo-ui-manifest.json'
        if not manifest.exists():
            return 503, b'UI_BUILD_UNAVAILABLE', 'text/plain; charset=utf-8', 'no-store'
        name = 'index.html' if page else path.lstrip('/')
        try:
            spec = json.loads(manifest.read_text(encoding='utf-8'))
            if not isinstance(spec, dict) or spec.get('version') != 1 or not isinstance(spec.get('files'), dict):
                raise ValueError()
            expected = spec['files'].get(name)
            if not expected or any(part in {'', '.', '..'} for part in name.split('/')) or '%' in name or '\\' in name:
                return 404, b'NOT_FOUND', 'text/plain', 'no-store'
            target = self.root / name
            if not target.resolve().is_relative_to(self.root.resolve()) or any(p.is_symlink() for p in [target, *target.parents] if p == self.root or self.root in p.parents):
                raise ValueError()
            raw = target.read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError()
            media = 'text/html; charset=utf-8' if name == 'index.html' else mimetypes.guess_type(name)[0] or 'application/octet-stream'
            return 200, raw, media, 'no-cache' if name == 'index.html' else 'public, max-age=31536000, immutable'
        except (OSError, ValueError, KeyError, TypeError):
            return 503, b'UI_BUILD_UNAVAILABLE', 'text/plain; charset=utf-8', 'no-store'
