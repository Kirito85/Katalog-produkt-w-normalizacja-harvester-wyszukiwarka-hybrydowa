"""
Krok 5 (wariant HTTP zamiast/obok CLI): prosty endpoint bez zewnętrznych
zależności (tylko stdlib), żeby nie dokładać frameworka dla jednego route'a.

Uruchomienie:  python -m app.server            # nasłuchuje na :8000
Przykład:      curl "http://localhost:8000/search?q=izolacja+RNA&producent=NovaGen+Labs"
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from app.db import get_conn
from app.search import hybrid_search


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # ciszej w konsoli

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/search":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"use /search?q=..."}')
            return

        qs = parse_qs(parsed.query)
        query = (qs.get("q") or [""])[0]
        producent = (qs.get("producent") or [None])[0]
        kategoria = (qs.get("kategoria") or [None])[0]
        top = int((qs.get("top") or ["10"])[0])

        if not query:
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "brak parametru q"}).encode("utf-8"))
            return

        conn = get_conn()
        results = hybrid_search(conn, query, producent, kategoria, top)
        conn.close()

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps({"query": query, "results": results}, ensure_ascii=False).encode("utf-8"))


def main(port: int = 8000):
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Serwer wyszukiwania na http://localhost:{port}/search?q=...")
    server.serve_forever()


if __name__ == "__main__":
    main()
