#!/usr/bin/env python3
"""DeCloud VM Welcome Page Server with Caching"""
import http.server
import socketserver
import os
import socket
from email.utils import formatdate

PORT = 80
DIRECTORY = "/var/www"

# Read hostname once at startup. Cloud-init's `hostname:` field is
# substituted from VM_NAME at orchestrator render time, so this resolves
# to the VM's friendly name (e.g., "bu4-2770"). Same boundary the DHT
# dashboard's _send_file_with_substitution uses for its placeholders.
SUBSTITUTIONS = {"__VM_NAME__": socket.gethostname()}

CACHE_DURATION = {
    '.html': 300, '.css': 86400, '.js': 86400,
    '.jpg': 2592000, '.png': 2592000, '.svg': 2592000
}


class CachingHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def do_GET(self):
        path = self.translate_path(self.path)

        # HTML files: substitute placeholders before serving. Per-VM templating
        # belongs at the consumer (this server), not in the artifact pipeline.
        if path.endswith('.html') and os.path.isfile(path):
            self._serve_html_with_substitution(path)
            return

        # If-None-Match handling for non-HTML (etag based on file mtime+size,
        # stable per VM since SUBSTITUTIONS is fixed at startup).
        if os.path.isfile(path):
            inm = self.headers.get('If-None-Match')
            if inm:
                try:
                    s = os.stat(path)
                    if inm == f'"{int(s.st_mtime)}-{s.st_size}"':
                        self.send_response(304)
                        self.send_header('ETag', inm)
                        self.end_headers()
                        return
                except OSError:
                    pass

        super().do_GET()

    def _serve_html_with_substitution(self, fpath):
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                content = f.read()
            for placeholder, value in SUBSTITUTIONS.items():
                content = content.replace(placeholder, value)
            data = content.encode('utf-8')

            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            duration = CACHE_DURATION['.html']
            self.send_header('Cache-Control', f'public, max-age={duration}')
            try:
                s = os.stat(fpath)
                self.send_header('ETag', f'"{int(s.st_mtime)}-{s.st_size}"')
                self.send_header('Last-Modified', formatdate(s.st_mtime, usegmt=True))
            except OSError:
                pass
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(500, f"Internal error: {e}")

    def end_headers(self):
        # Caching headers for non-HTML responses (HTML branch above writes its own).
        path = self.translate_path(self.path)
        if os.path.exists(path) and not os.path.isdir(path) and not path.endswith('.html'):
            ext = os.path.splitext(path)[1].lower()
            duration = CACHE_DURATION.get(ext, 3600)
            self.send_header('Cache-Control', f'public, max-age={duration}')
            try:
                s = os.stat(path)
                self.send_header('ETag', f'"{int(s.st_mtime)}-{s.st_size}"')
                self.send_header('Last-Modified', formatdate(s.st_mtime, usegmt=True))
            except OSError:
                pass
            self.send_header('X-Content-Type-Options', 'nosniff')
        super().end_headers()


with socketserver.TCPServer(("", PORT), CachingHandler) as httpd:
    print(f"DeCloud Welcome Server on port {PORT}")
    httpd.serve_forever()