"""Navigator's supervised pywb frontend with atomically refreshed catalog state."""
from __future__ import annotations

# pywb's CLI applies its required gevent monkey patch before importing the app.
from pywb.apps.cli import ReplayCli
from pywb.apps.frontendapp import FrontEndApp
from pywb.rewrite.templateview import BaseInsertView
from pywb.apps.wbrequestresponse import WbResponse

import json
import logging
from pathlib import Path


class CatalogFrontend(FrontEndApp):
    def __init__(self, *args, **kwargs):
        self.catalog = None
        self.catalog_stamp = None
        super().__init__(*args, **kwargs)
        self._refresh_catalog()

    def _refresh_catalog(self):
        path = Path('catalog-state.json')
        if not path.exists():
            return
        stamp = path.stat().st_mtime_ns
        if stamp == self.catalog_stamp:
            return
        state = json.loads(path.read_text())
        # The parent publishes validated configs only. Existing index paths stay
        # fixed; register just newly recovered archives without restarting pywb.
        for name, config in state['collections'].items():
            if name not in self.warcserver.fixed_routes:
                handler = self.warcserver.load_coll(name, config)
                self.warcserver.add_route('/' + name, handler)
                self.warcserver.fixed_routes[name] = handler
                self.warcserver.config.setdefault('collections', {})[name] = config
        self.catalog = state
        self.catalog_stamp = stamp

    def __call__(self, environ, start_response):
        try:
            self._refresh_catalog()
        except Exception:
            logging.exception('Cannot adopt catalog snapshot; retaining previous state')
        return super().__call__(environ, start_response)

    def serve_home(self, environ):
        if self.catalog is None:
            return super().serve_home(environ)
        view = BaseInsertView(self.rewriterapp.jinja_env, 'index.html')
        content = view.render_to_string(environ, catalog=self.catalog)
        return WbResponse.text_response(content, content_type='text/html; charset=utf-8')


class NavigatorReplayCli(ReplayCli):
    def load(self):
        super().load()
        return CatalogFrontend(custom_config=self.extra_config)


if __name__ == '__main__':
    NavigatorReplayCli(desc='Archive Magic Navigator replay server').run()
