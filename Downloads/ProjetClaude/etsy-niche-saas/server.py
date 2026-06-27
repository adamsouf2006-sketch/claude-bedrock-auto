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
        if u.path == "/api/stop":
            sid = parse_qs(u.query).get("sid", [""])[0]
            ok = core.cancel_search(sid) if sid else False
            return self._send(200, json.dumps({"stopped": ok}))
        if u.path == "/api/etsy_login":
            # ouvre une fenetre Etsy visible dans le Chrome debug pour login + 1er Datadome
            import scraper
            r = scraper.etsy_login_window()
            ok = r.get("ok") if isinstance(r, dict) else bool(r)
            err = r.get("error", "") if isinstance(r, dict) else ""
            return self._send(200, json.dumps({"opened": ok, "error": err,
                "msg": "Connecte-toi a Etsy dans la fenetre ouverte, puis lance ta recherche."}))
        if u.path == "/api/etsy_status":
            import scraper
            return self._send(200, json.dumps({"session_ok": scraper.etsy_session_ok(),
                                               "via_chrome": scraper.SCRAPE_VIA_CHROME}))
        if u.path == "/api/complete_catalogs":
            q = parse_qs(u.query)
            try: lim = min(int(q.get("limit", ["50"])[0]), 300)
            except: lim = 50
            return self._send(200, json.dumps(core.complete_catalogs(lim)))
        if u.path in ("/api/similar", "/api/similar_stream"):
            q = parse_qs(u.query)
            def gi2(k, d):
                try: return int(q.get(k, [d])[0])
                except: return d
            def gb2(k):
                return q.get(k, ["false"])[0].lower() in ("1", "true", "yes", "on")
            cats = q.get("exclude_categories", [""])[0]
            sf = {
                "min_rate": gi2("min_rate", 0),
                "max_age_months": gi2("max_age_months", 999),
                "min_age_months": gi2("min_age_months", 0),
                "min_sold": gi2("min_sold", 0),
                "min_price": gi2("min_price", 0),
                "max_weight_g": gi2("max_weight_g", 0),
                "exclude_digital": gb2("exclude_digital"),
                "exclude_perso": gb2("exclude_perso"),
                "exclude_supply": gb2("exclude_supply"),
                "exclude_vintage": gb2("exclude_vintage"),
                "exclude_heavy": gb2("exclude_heavy"),
                "exclude_custom_shops": gb2("exclude_custom_shops"),
                "exclude_categories": [c for c in cats.split(",") if c],
                "use_ai": gb2("use_ai"),
                "use_vision": gb2("use_vision"),
                "validate_ali": gb2("validate_ali"),
                "ali_products": gi2("ali_products", 5),
                "ali_min_match": gi2("ali_min_match", 2),
                "ali_gate": gb2("ali_gate"),
                "fetch_titles": True,
                "only_cn_hk": gb2("only_cn_hk"),
                "min_per_niche": gi2("min_per_niche", 1),
            }
            shop = q.get("shop", [""])[0]
            smode = q.get("mode", ["live"])[0]
            target = gi2("target_count", 30)
            # BUDGET INSISTANT: le clone finder ne doit PAS s'arreter sur un petit budget (avant
            # target*6+200 = 260 credits pour 10 boutiques => il rendait 1 et stoppait). On lui
            # donne un gros budget pour PAGINER LOIN sur Etsy jusqu'a atteindre la cible. Borne a
            # 80% du quota restant (garde une reserve), large minimum 1500. Override via max_api.
            try: _rem = int(core.quota_remaining())
            except Exception: _rem = 5000
            # ~2 credits/boutique gardee + overhead filtrage. target*12 large pour paginer
            # un peu sur niche pauvre, MAIS borne a 40% du quota restant (jamais cramer le
            # quota d'un run). L'early-abort no_match_budget stoppe tot si filon sec.
            mxa = gi2("max_api", 0) or min(target * 12 + 100, max(int(_rem * 0.4), 300))
            if u.path == "/api/similar_stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                def sse2(obj):
                    try:
                        self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                    except Exception:
                        pass
                def prog2(matched, scanned):
                    sse2({"type": "progress", "matched": matched, "scanned": scanned})
                sid = q.get("sid", [""])[0]
                ev = core.make_cancel(sid) if sid else None
                try:
                    res = core.find_similar_shops(shop_input=shop, target_count=min(target, 300),
                                                  max_api=mxa, filters=sf, mode=smode,
                                                  progress=prog2, stop=ev)
                    sse2({"type": "done", "result": res})
                except Exception as e:
                    sse2({"type": "error", "error": str(e)})
                finally:
                    if sid: core.clear_cancel(sid)
                    core.close_browsers()      # ferme onglets/pages: recherche finie
                return
            try:
                res = core.find_similar_shops(shop_input=shop, target_count=min(target, 300),
                                              max_api=mxa, filters=sf, mode=smode)
                return self._send(200, json.dumps(res, ensure_ascii=False))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
            finally:
                core.close_browsers()          # ferme onglets/pages: recherche finie
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
                "use_vision": gb("use_vision"),
                "ai_dropship_gate": gb("ai_dropship_gate"),
                "dropship_min": gi("dropship_min", 50) / 100.0,
                "target_count": gi("target_count", 100),
                "min_per_niche": gi("min_per_niche", 1),
                "validate_ali": gb("validate_ali"),
                "ali_products": gi("ali_products", 5),
                "ali_min_match": gi("ali_min_match", 2),
                "ali_gate": gb("ali_gate"),
                "fetch_titles": gb("fetch_titles"),
                "only_cn_hk": gb("only_cn_hk"),
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
                sid = q.get("sid", [""])[0]
                ev = core.make_cancel(sid) if sid else None
                try:
                    if source == "scrape":
                        res = core.run_scrape_multi(keyword=keyword, target_count=min(target, 1000),
                                              filters=filters, progress=prog, stop=ev)
                    elif source == "live":
                        tgt = min(target, 1000)
                        # budget credits auto-dimensionne sur la cible (~3 credits/boutique:
                        # 1 enrich + 1 titres + part de listing). Sinon le budget fixe 500
                        # arretait la recherche avant d'atteindre la cible demandee.
                        # budget genereux: le sur-echantillonnage (IA/AliExpress filtrent
                        # apres) demande de scanner plus de candidats pour NET >= cible.
                        # PLAFOND CREDITS (protection quota): sans borne, target 500 => ~3100 credits
                        # AUTO => vidait le quota en silence sur une niche pauvre en drops. On plafonne
                        # le budget AUTO a MAX_API_AUTO (defaut 400). L'utilisateur relance s'il veut
                        # creuser plus, au lieu de tout cramer d'un coup. Un max_api explicite reste
                        # respecte (opt-in conscient).
                        import os as _os
                        # PLAFOND PROPORTIONNEL A LA CIBLE: un cap fixe (400) arretait le run
                        # bien avant la cible (target 500 => ~3100 credits requis, capait a 400 =>
                        # ~100 boutiques puis saut direct a la verif drop). On garde une reserve
                        # quota (20%) mais on laisse le budget monter avec la cible. Override
                        # explicite via max_api ou MAX_API_AUTO (cap dur facultatif).
                        try: _rem = int(core.quota_remaining())
                        except Exception: _rem = 5000
                        hard = max(int(_rem * 0.8), 400)
                        cap_env = int(_os.environ.get("MAX_API_AUTO", "0") or 0)
                        if cap_env: hard = min(hard, cap_env)
                        mxa = gi("max_api", 0) or min(tgt * 6 + 100, hard)
                        res = core.run_discovery(keyword=keyword, target_count=tgt,
                                                 max_api=mxa, filters=filters, progress=prog, stop=ev)
                    else:
                        res = core.search_cache(filters=filters, keyword=keyword)
                    sse({"type": "done", "result": res})
                except Exception as e:
                    sse({"type": "error", "error": str(e)})
                finally:
                    if sid: core.clear_cancel(sid)
                    core.close_browsers()      # ferme onglets/pages: recherche finie
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
                    res = core.run_scrape_multi(keyword=keyword, target_count=min(target, 1000), filters=filters)
                else:
                    tgt = min(target, 1000)
                    import os as _os
                    try: _rem = int(core.quota_remaining())
                    except Exception: _rem = 5000
                    hard = max(int(_rem * 0.8), 400)
                    cap_env = int(_os.environ.get("MAX_API_AUTO", "0") or 0)
                    if cap_env: hard = min(hard, cap_env)
                    mxa = gi("max_api", 0) or min(tgt * 6 + 100, hard)
                    res = core.run_discovery(keyword=keyword, target_count=tgt,
                                             max_api=mxa, filters=filters)
                return self._send(200, json.dumps(res, ensure_ascii=False))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
            finally:
                if source in ("scrape", "live"):
                    core.close_browsers()      # ferme onglets/pages: recherche finie
        if u.path == "/api/niche_finder_reset":
            # oublie les niches deja proposees => autorise a les re-suggerer
            import niche_finder
            niche_finder.reset_niche_history()
            return self._send(200, json.dumps({"reset": True}))
        if u.path in ("/api/niche_finder", "/api/niche_finder_stream"):
            # NICHE FINDER: propose des niches a fort potentiel drop + demande (2 phases).
            q = parse_qs(u.query)
            def gi3(k, d):
                try: return int(q.get(k, [d])[0])
                except: return d
            def gb3(k):
                return q.get(k, ["false"])[0].lower() in ("1", "true", "yes", "on")
            mode = q.get("mode", ["scrape"])[0]
            filters = {
                "exclude_digital": True, "exclude_perso": True,
                "use_ai": True,
                "only_cn_hk": gb3("only_cn_hk"),
                "ali_products": gi3("ali_products", 5),
                "ali_min_match": gi3("ali_min_match", 2),
            }
            n_candidates = min(gi3("n_candidates", 15), 30)
            sample_per_niche = min(gi3("sample_per_niche", 6), 15)
            deep_top = min(gi3("deep_top", 3), 8)
            import niche_finder
            if u.path == "/api/niche_finder_stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                def sse3(obj):
                    try:
                        self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                    except Exception:
                        pass
                sid = q.get("sid", [""])[0]
                ev = core.make_cancel(sid) if sid else None
                try:
                    res = niche_finder.scout_niches(filters=filters, mode=mode,
                            n_candidates=n_candidates, sample_per_niche=sample_per_niche,
                            deep_top=deep_top, progress=sse3, stop=ev)
                    sse3({"type": "done", "result": res})
                except Exception as e:
                    sse3({"type": "error", "error": str(e)})
                finally:
                    if sid: core.clear_cancel(sid)
                    core.close_browsers()
                return
            try:
                res = niche_finder.scout_niches(filters=filters, mode=mode,
                        n_candidates=n_candidates, sample_per_niche=sample_per_niche,
                        deep_top=deep_top)
                return self._send(200, json.dumps(res, ensure_ascii=False))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
            finally:
                core.close_browsers()
        return self._send(404, json.dumps({"error": "not found"}))


def _ensure_dropship_chrome():
    """Lance le Chrome debug (CDP) de la detection dropship des le demarrage => aucune commande
    a taper. 1er run: la fenetre s'ouvre sur lens.google.com pour le login Google (1 seule fois).
    Desactivable via ALI_CDP_AUTO=0."""
    import os
    if os.environ.get("ALI_CDP_AUTO", "1") in ("0", "false", "no"):
        return
    try:
        import ali_chrome
        first = ali_chrome.first_login_needed()
        url = ali_chrome.ensure_chrome()
        if url:
            os.environ["ALI_CDP_URL"] = url
            print(f"Detection dropship: Chrome debug pret ({url}).")
            if first:
                print(">>> 1re fois: CONNECTE-TOI a Google dans la fenetre Chrome qui s'ouvre,")
                print(">>> verifie lens.google.com sans captcha. Ensuite c'est automatique.")
        else:
            print("Detection dropship: Chrome debug indispo (repli moteur furtif).")
    except Exception as e:
        print("Detection dropship: init Chrome ignoree:", repr(e)[:80])


if __name__ == "__main__":
    print(f"SaaS niche Etsy -> http://localhost:{PORT}")
    _ensure_dropship_chrome()
    print("Ctrl+C pour arreter.")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
