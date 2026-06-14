"""
Validation dropship par IMAGE: pour les images produit d'une boutique Etsy, on fait
une recherche PAR IMAGE sur AliExpress (upload via l'icone camera), on recupere les
vignettes des resultats et on compare perceptuellement (average hash) a l'image Etsy.
Un produit est "trouve sur AliExpress" si une vignette resultat est visuellement
quasi-identique (distance de Hamming faible). Une boutique est validee si >= min_match
de ses produits sont trouves.

Sync API utilisable depuis etsy_core:
  validate_shop_images(image_urls, min_match=3, hash_thresh=12) -> dict
"""
import asyncio, io, re, threading, urllib.parse, urllib.request
from PIL import Image

# ---- similarite titre (fallback texte, fiable) -------------------------------
_TSTOP = set("de la le les un une des et ou en pour avec sur au aux du d l the a an of for "
             "with in to and or handmade fait main decoration deco cadeau gift set new custom "
             "personalized your".split())
def _kw(title, n=6):
    t = re.sub(r"[^a-zA-Z0-9 ]", " ", (title or "").lower())
    return " ".join([w for w in t.split() if len(w) > 2 and w not in _TSTOP][:n])
def _tok(s):
    s = re.sub(r"[^a-zA-Z0-9 ]", " ", (s or "").lower())
    return set(w for w in s.split() if len(w) > 2 and w not in _TSTOP)
def _sim(a, b):
    x, y = _tok(a), _tok(b)
    return len(x & y) / len(x | y) if x and y else 0.0

try:
    from patchright.async_api import async_playwright
    PATCHRIGHT_OK = True
except Exception:
    PATCHRIGHT_OK = False

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

# Upload image AliExpress bloque par anti-automation (l'input n'apparait que sur vrai
# geste humain). Par defaut on saute l'image et on valide par TEXTE (fiable, ~5s).
# TRY_IMAGE=True: tente d'abord le match PAR IMAGE (vraie detection "identique"),
# puis retombe sur le TEXTE si l'upload est bloque. Override via env ALI_TRY_IMAGE=0.
import os as _os
TRY_IMAGE = _os.environ.get("ALI_TRY_IMAGE", "1") not in ("0", "false", "no")

# ---- perceptual hash (Pillow seul) -------------------------------------------
def _ahash(img_bytes):
    try:
        im = Image.open(io.BytesIO(img_bytes)).convert("L").resize((8, 8))
    except Exception:
        return None
    px = list(im.getdata()); avg = sum(px) / 64.0
    bits = 0
    for i, p in enumerate(px):
        if p > avg:
            bits |= (1 << i)
    return bits

def _hamming(a, b):
    return bin(a ^ b).count("1") if (a is not None and b is not None) else 64

def _download(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except Exception:
        return None

# ---- boucle asyncio dediee ---------------------------------------------------
_loop = None
_lock = threading.Lock()
def _ensure_loop():
    global _loop
    if _loop is None:
        with _lock:
            if _loop is None:
                _loop = asyncio.new_event_loop()
                threading.Thread(target=_loop.run_forever, daemon=True).start()
def _run(coro):
    _ensure_loop()
    return asyncio.run_coroutine_threadsafe(coro, _loop).result()

# ---- recherche image AliExpress ----------------------------------------------
_PIC_BTN = ('[class*="picture-search"], [class*="picSearch"], [class*="searchByImage"], '
            '[class*="image-search"], [class*="pic-search"], #searchByImage, '
            '[aria-label*="image" i], [aria-label*="photo" i]')
_CONSENT = ['button:has-text("Accept All")', 'button:has-text("Accept")',
            'button:has-text("Accepter")', 'button:has-text("Tout accepter")',
            '[data-role="gdpr-accept"]', '.btn-accept', '[class*="gdpr"] button',
            '[aria-label="Close"]', 'div[class*="close-icon"]']

async def _dismiss_overlays(pg):
    for sel in _CONSENT:
        try:
            el = await pg.query_selector(sel)
            if el:
                await el.click(timeout=1200); await pg.wait_for_timeout(300)
        except Exception:
            pass

async def _ali_image_search(pg, img_path):
    """Upload une image sur AliExpress (recherche par image) -> URLs vignettes resultats.
    Robuste: gere les bannieres consentement, plusieurs selecteurs de bouton camera,
    et l'alimentation directe de input[type=file] (visible ou cache)."""
    await pg.goto("https://www.aliexpress.com", wait_until="domcontentloaded", timeout=40000)
    await pg.wait_for_timeout(2200)
    await _dismiss_overlays(pg)
    uploaded = False
    # 1) bouton camera -> file chooser natif
    try:
        async with pg.expect_file_chooser(timeout=4000) as fc:
            btn = await pg.query_selector(_PIC_BTN)
            if btn: await btn.click()
        ch = await fc.value
        await ch.set_files(img_path); uploaded = True
    except Exception:
        pass
    # 2) clic bouton puis alimentation directe de l'input file (visible ou cache)
    if not uploaded:
        try:
            btn = await pg.query_selector(_PIC_BTN)
            if btn:
                await btn.click(); await pg.wait_for_timeout(1000)
        except Exception:
            pass
        for inp in await pg.query_selector_all('input[type="file"]'):
            try:
                await inp.set_input_files(img_path); uploaded = True; break
            except Exception:
                continue
    if not uploaded:
        return []
    # attendre la page resultats image (plusieurs patterns d'URL possibles)
    for pat in ("**/image-search**", "**/wholesale**", "**imageId**"):
        try:
            await pg.wait_for_url(pat, timeout=6000); break
        except Exception:
            continue
    await pg.wait_for_timeout(6000)
    # vignettes des cartes resultats
    urls = await pg.evaluate("""() => {
        const out=[]; document.querySelectorAll('a[href*="/item/"] img, div[class*="card"] img').forEach(i=>{
            const s=i.src||i.getAttribute('data-src'); if(s && s.startsWith('http')) out.push(s);
        }); return [...new Set(out)].slice(0,12);
    }""")
    return urls

async def _ali_text_search(pg, query):
    """Recherche texte AliExpress -> [titres resultats]. Fallback fiable.
    Slug AliExpress = mots separes par des TIRETS (pas %20), sinon 0 resultat."""
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-") or "gift"
    url = ("https://www.aliexpress.com/w/wholesale-" + slug + ".html"
           "?SearchText=" + urllib.parse.quote(query))
    try:
        await pg.goto(url, wait_until="domcontentloaded", timeout=35000)
        await pg.wait_for_timeout(2500)
        await _dismiss_overlays(pg)
        await pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await pg.wait_for_timeout(1200)
        return await pg.evaluate("""()=>{const o=[];document.querySelectorAll('a[href*="/item/"]').forEach(a=>{
            const t=(a.textContent||'').trim();if(t&&t.length>15)o.push(t.slice(0,90));});return [...new Set(o)].slice(0,10);}""")
    except Exception:
        return []

async def _check_one(pg, prod, hash_thresh, sim_thresh):
    """1 produit: tente image, sinon fallback texte. Retourne (hit, via, detail)."""
    import tempfile, os
    # --- tentative IMAGE (desactivee par defaut: bloquee + lente) ---
    img = prod.get("image_url")
    if TRY_IMAGE and img:
        q = _download(img); qh = _ahash(q) if q else None
        if qh is not None:
            fd, path = tempfile.mkstemp(suffix=".jpg"); os.close(fd)
            with open(path, "wb") as f: f.write(q)
            try:
                thumbs = await _ali_image_search(pg, path)
            except Exception:
                thumbs = []
            finally:
                try: os.remove(path)
                except Exception: pass
            if thumbs:   # l'upload image a fonctionne
                best = 64
                for tu in thumbs:
                    tb = _download(tu)
                    d = _hamming(qh, _ahash(tb)) if tb else 64
                    if d < best: best = d
                return (best <= hash_thresh, "image", {"dist": best})
    # --- fallback TEXTE ---
    title = prod.get("title", "")
    items = await _ali_text_search(pg, _kw(title))
    # detection captcha AliExpress (anti-bot) -> validation auto impossible
    if "punish" in (pg.url or "") or "_____tmd_____" in (pg.url or ""):
        return (False, "captcha", {})
    best = max((_sim(title, it) for it in items), default=0.0)
    return (best >= sim_thresh, "texte", {"sim": round(best, 2)})

async def _validate(products, min_match, hash_thresh, sim_thresh, headless):
    res = {"checked": 0, "hits": 0, "total": len(products), "via": {}, "matches": []}
    if not PATCHRIGHT_OK or not products:
        res["error"] = "patchright indisponible" if not PATCHRIGHT_OK else "pas de produit"
        res["validated"] = False
        return res
    async with async_playwright() as p:
        br = await p.chromium.launch(headless=headless)
        ctx = await br.new_context(locale="fr-FR", viewport={"width":1440,"height":900}, user_agent=UA)
        pg = await ctx.new_page()
        remaining = len(products)
        for prod in products:
            res["checked"] += 1; remaining -= 1
            try:
                hit, via, detail = await _check_one(pg, prod, hash_thresh, sim_thresh)
            except Exception:
                hit, via, detail = False, "erreur", {}
            res["via"][via] = res["via"].get(via, 0) + 1
            if via == "captcha":
                res["blocked"] = True
                break   # AliExpress nous captcha -> inutile d'insister
            if hit:
                res["hits"] += 1
                res["matches"].append({"title": prod.get("title","")[:60], "via": via, **detail})
            if res["hits"] >= min_match:
                break                       # assez de preuves -> stop (economie)
            if res["hits"] + remaining < min_match:
                break                       # impossible d'atteindre min_match -> stop
            await pg.wait_for_timeout(800)
        await br.close()
    if res.get("blocked"):
        res["validated"] = None   # inconnu (AliExpress a bloque)
    else:
        res["validated"] = res["hits"] >= min_match
    return res

def validate_shop(products, min_match=3, hash_thresh=12, sim_thresh=0.30, headless=None):
    """Sync: valide une boutique. products = [{title, image_url}]. Tente image/produit,
    fallback texte si l'upload image est bloque. Boutique validee si >= min_match trouves.
    headless=None => non-headless par defaut (patchright stealth passe mieux l'anti-bot
    AliExpress/Datadome). Override via env ALI_HEADLESS=1."""
    if not PATCHRIGHT_OK:
        return {"validated": False, "hits": 0, "total": len(products or []), "error": "patchright indisponible"}
    if headless is None:
        import os as _os
        headless = _os.environ.get("ALI_HEADLESS", "0") in ("1", "true", "yes")
    return _run(_validate(products or [], min_match, hash_thresh, sim_thresh, headless))
