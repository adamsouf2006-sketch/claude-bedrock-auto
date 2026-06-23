"""Gere le Chrome debug (CDP) utilise par la detection dropship. Le serveur l'appelle
AUTOMATIQUEMENT (ensure_chrome) => aucune manip terminal. Le 1er lancement demande un login
Google manuel dans la fenetre (vrai Chrome GUI => non bloque); ensuite le profil dedie
(cache/chrome_debug) est reutilise tout seul.

Pourquoi un profil dedie: Chrome 136+ refuse --remote-debugging-port sur le profil par defaut.

API:
    ensure_chrome(port=9222) -> "http://localhost:9222" si un Chrome debug repond (lance si besoin),
                                sinon None.
CLI:
    python ali_chrome.py            # lance + attend le port (login Google 1x au 1er run)
    python ali_chrome.py --check    # verifie juste le port
"""
import os, sys, time, json, subprocess, urllib.request, glob

PORT = int(os.environ.get("ALI_CDP_PORT", "9222"))
PROFILE = os.environ.get(
    "ALI_CDP_PROFILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "chrome_debug"))

def chrome_exe():
    cands = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    hits = glob.glob(os.path.expandvars(r"%ProgramFiles%\Google\Chrome*\Application\chrome.exe"))
    return hits[0] if hits else None

def debug_ok(port=PORT):
    """Retourne le 'Browser' string si le port debug repond, sinon None."""
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=3) as r:
            return json.loads(r.read()).get("Browser", "chrome")
    except Exception:
        return None

def launch(port=PORT, profile=PROFILE, url="https://lens.google.com/", hidden=False):
    """Lance le Chrome debug (non bloquant). hidden=True => fenetre HORS-ECRAN (invisible mais
    fonctionnelle: DBSC/Lens marchent car c'est un vrai Chrome, pas du headless). Retourne True
    si le process a ete lance."""
    exe = chrome_exe()
    if not exe:
        return False
    os.makedirs(profile, exist_ok=True)
    # CAP DISQUE: le profil dedie laisse grossir Cache/Code Cache sans limite (=> 700+ MB, a
    # sature le disque). On borne le cache disque a 100 Mo et le media-cache a 50 Mo: largement
    # assez pour Lens/AliExpress, et ca ne touche PAS les cookies/login (stockes ailleurs).
    args = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", "--new-window",
            "--disk-cache-size=104857600", "--media-cache-size=52428800"]
    if hidden:
        # pousse la fenetre tres loin hors de l'ecran + petite taille => l'utilisateur ne la
        # voit pas. On NE met PAS --headless (Google challenge le headless; un Chrome reel
        # hors-ecran garde la session connectee et passe Lens).
        # ANTI-THROTTLING: une fenetre hors-ecran/arriere-plan est ralentie par Chrome (timers
        # JS brides) => Lens lazy-load trop lent => hits rates. Ces flags gardent le plein regime.
        args += ["--window-position=-32000,-32000", "--window-size=1100,850",
                 "--disable-background-timer-throttling",
                 "--disable-backgrounding-occluded-windows",
                 "--disable-renderer-backgrounding"]
    args.append(url)
    try:
        subprocess.Popen(args, close_fds=True)
        return True
    except Exception:
        return False

def ensure_chrome(port=PORT, profile=PROFILE, wait=18):
    """Garantit un Chrome debug joignable. Le lance si besoin et attend le port.
    Retourne l'URL CDP ('http://localhost:PORT') ou None si echec.
    INVISIBLE par defaut: la fenetre est lancee HORS-ECRAN, SAUF au 1er run (login Google a
    faire => visible). Une fois le profil connecte, tout tourne en arriere-plan."""
    if debug_ok(port):
        return f"http://localhost:{port}"
    hidden = not first_login_needed(profile)   # 1er run visible (login), ensuite hors-ecran
    if not launch(port, profile, hidden=hidden):
        return None
    for _ in range(int(wait / 0.7) + 1):
        time.sleep(0.7)
        if debug_ok(port):
            return f"http://localhost:{port}"
    return None

def first_login_needed(profile=PROFILE):
    """True si le profil dedie n'a jamais ete utilise (=> login Google a faire)."""
    return not os.path.exists(os.path.join(profile, "Default"))

def main():
    if "--check" in sys.argv:
        print(f"debug port {PORT}:", debug_ok() or "INJOIGNABLE")
        return
    first = first_login_needed()
    url = ensure_chrome()
    if not url:
        print("Echec lancement Chrome debug. Chrome installe? Port occupe? (python ali_chrome.py --check)")
        return
    print(f"OK Chrome debug actif sur {url} (profil {PROFILE})")
    if first:
        print(">>> 1er lancement: CONNECTE-TOI a Google dans la fenetre, verifie lens.google.com sans captcha.")
    print("Le serveur reutilisera ce Chrome automatiquement. Laisse-le ouvert.")

if __name__ == "__main__":
    main()
