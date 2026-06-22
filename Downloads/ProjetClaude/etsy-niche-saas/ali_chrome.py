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

def launch(port=PORT, profile=PROFILE, url="https://lens.google.com/"):
    """Lance le Chrome debug (non bloquant). Retourne True si le process a ete lance."""
    exe = chrome_exe()
    if not exe:
        return False
    os.makedirs(profile, exist_ok=True)
    args = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", "--new-window", url]
    try:
        subprocess.Popen(args, close_fds=True)
        return True
    except Exception:
        return False

def ensure_chrome(port=PORT, profile=PROFILE, wait=18):
    """Garantit un Chrome debug joignable. Le lance si besoin et attend le port.
    Retourne l'URL CDP ('http://localhost:PORT') ou None si echec.
    NON-INTERACTIF: si c'est le 1er run sans login, Lens captcha encore -> a l'utilisateur
    de se connecter une fois (la fenetre s'ouvre sur lens.google.com)."""
    if debug_ok(port):
        return f"http://localhost:{port}"
    if not launch(port, profile):
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
