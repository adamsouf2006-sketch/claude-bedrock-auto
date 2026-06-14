"""
Serveur SaaS niche Etsy (stdlib, zero dependance).
Sert le dashboard + API JSON.
Lancer: python server.py  ->  http://localhost:8000
"""
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import etsy_core as core

ROOT = Path(__file__).parent
PORT = 8000

class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):  # silencieux
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, (ROOT / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/quota":
            return self._send(200, json.dumps(core.quota_state()))
        if u.path == "/api/complete_catalogs":
            q = parse_qs(u.query)
            try: lim = min(int(q.get("limit", ["50"])[0]), 300)
            except: lim = 50
            return self._send(200, json.dumps(core.complete_catalogs(lim)))
        if u.path in ("/api/discover", "/api/export", "/api/discover_stream"):
            q = parse_qs(u.query)
            def gi(k, d):  # get int
                try: return int(q.get(k, [d])[0])
                except: return d
            def gb(k):  # get bool
                return q.get(k, ["false"])[0].lower() in ("1", "true", "yes", "on")
            cats = q.get("exclude_categories", [""])[0]
            filters = {
                "min_rate": gi("min_rate", 0),
                "max_age_months": gi("max_age_months", 999),
                "min_age_months": gi("min_age_months", 0),
                "min_sold": gi("min_sold", 0),
                "min_price": gi("min_price", 0),
                "max_weight_g": gi("max_weight_g", 0),
                "exclude_digital": gb("exclude_digital"),
                "exclude_perso": gb("exclude_perso"),
                "exclude_supply": gb("exclude_supply"),
                "exclude_vintage": gb("exclude_vintage"),
                "exclude_heavy": gb("exclude_heavy"),
                "exclude_custom_shops": gb("exclude_custom_shops"),
                "exclude_categories": [c for c in cats.split(",") if c],
                "use_ai": gb("use_ai"),
                "ai_dropship_gate": gb("ai_dropship_gate"),
                "dropship_min": gi("dropship_min", 50) / 100.0,
                "target_count": gi("target_count", 100),
                "min_per_niche": gi("min_per_niche", 1),
                "validate_ali": gb("validate_ali"),
                "ali_products": gi("ali_products", 10),
                "ali_min_match": gi("ali_min_match", 3),
                "ali_gate": gb("ali_gate"),
                "fetch_titles": gb("fetch_titles"),
            }
            target = gi("target_count", 100)
            keyword = q.get("keyword", [""])[0]
            source = q.get("source", ["cache"])[0]

            # ----- flux temps reel (SSE): compteur live pendant le scrape -----
            if u.path == "/api/discover_stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                def sse(obj):
                    try:
                        self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                    except Exception:
                        pass
                def prog(matched, scanned):
                    sse({"type": "progress", "matched": matched, "scanned": scanned})
                try:
                    if source == "scrape":
                        res = core.run_scrape(keyword=keyword, target_count=min(target, 1000),
                                              filters=filters, progress=prog)
                    elif source == "live":
                        res = core.run_discovery(keyword=keyword, target_count=min(target, 500),
                                                 max_api=gi("max_api", 500), filters=filters, progress=prog)
                    else:
                        res = core.search_cache(filters=filters, keyword=keyword)
                    sse({"type": "done", "result": res})
                except Exception as e:
                    sse({"type": "error", "error": str(e)})
                return

            if u.path == "/api/export":
                csv = core.export_csv(filters=filters, keyword=q.get("keyword", [""])[0])
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=niches_etsy.csv")
                b = csv.encode("utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                return self.wfile.write(b)
            try:
                if source == "cache":
                    res = core.search_cache(filters=filters, keyword=keyword)
                elif source == "scrape":
                    res = core.run_scrape(keyword=keyword, target_count=min(target, 1000), filters=filters)
                else:
                    res = core.run_discovery(keyword=keyword, target_count=min(target, 500),
                                             max_api=gi("max_api", 500), filters=filters)
                return self._send(200, json.dumps(res, ensure_ascii=False))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
        return self._send(404, json.dumps({"error": "not found"}))


if __name__ == "__main__":
    print(f"SaaS niche Etsy -> http://localhost:{PORT}")
    print("Ctrl+C pour arreter.")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
