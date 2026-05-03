#!/usr/bin/env python3
"""DeCloud VM Welcome Page Server with Caching"""
import http.server
import socketserver
import os
import hashlib
import time
from email.utils import formatdate

PORT = 80
DIRECTORY = "/var/www"

CACHE_DURATION = {
    '.html': 300, '.css': 86400, '.js': 86400,
    '.jpg': 2592000, '.png': 2592000, '.svg': 2592000
}

class CachingHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)
    
    def end_headers(self):
        path = self.translate_path(self.path)
        if os.path.exists(path) and not os.path.isdir(path):
            ext = os.path.splitext(path)[1].lower()
            duration = CACHE_DURATION.get(ext, 3600)
            
            self.send_header('Cache-Control', f'public, max-age={duration}')
            
            try:
                stat = os.stat(path)
                etag = f'"{int(stat.st_mtime)}-{stat.st_size}"'
                self.send_header('ETag', etag)
                self.send_header('Last-Modified', formatdate(stat.st_mtime, usegmt=True))
            except: pass
            
            self.send_header('X-Content-Type-Options', 'nosniff')
        
        super().end_headers()
    
    def do_GET(self):
        path = self.translate_path(self.path)
        if os.path.exists(path) and not os.path.isdir(path):
            if_none_match = self.headers.get('If-None-Match')
            if if_none_match:
                try:
                    stat = os.stat(path)
                    etag = f'"{int(stat.st_mtime)}-{stat.st_size}"'
                    if if_none_match == etag:
                        self.send_response(304)
                        self.send_header('ETag', etag)
                        self.end_headers()
                        return
                except: pass
        
        super().do_GET()

with socketserver.TCPServer(("", PORT), CachingHandler) as httpd:
    print(f"DeCloud Welcome Server on port {PORT}")
    httpd.serve_forever()