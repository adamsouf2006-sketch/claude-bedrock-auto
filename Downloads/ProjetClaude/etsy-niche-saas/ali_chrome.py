"""Lance un Chrome reel en mode debug (--remote-debugging-port) sur un profil DEDIE, pour que
la detection dropship s'y connecte via CDP (ALI_CDP_URL). Tu te connectes a Google UNE FOIS
dans cette fenetre (vrai Chrome GUI => login non bloque), DBSC reste valide (meme machine),
et Google Lens repond sans captcha. Le profil est reutilise aux runs suivants.

Pourquoi un profil dedie et pas ton profil habituel:
 - Chrome 136+ REFUSE --remote-debugging-port sur le profil par defaut (anti-vol de cookies).
 - Un profil dedie contourne ce blocage; il suffit de s'y connecter a Google la 1re fois.

Usage:
    python ali_chrome.py            # lance Chrome debug (profil dedie) + attend le port
    python ali_chrome.py --check    # verifie juste si le port debug repond

Ensuite, terminal detection (PowerShell):
    $env:ALI_CDP_URL="http://localhost:9222"
    python test_dropship_live.py
"""
import os, sys, time, json, subprocess, urllib.request, glob

PORT = int(os.environ.get("ALI_CDP_PORT", "9222"))
# profil DEDIE (sous cache/, gitignore). 1er run: login Google manuel. Ensuite reutilise.
PROFILE = os.environ.get(
    "ALI_CDP_PROFILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "chrome_debug"))

def _chrome_exe():
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

def _debug_ok():
    try:
        with urllib.request.urlopen(f"http://localhost:{PORT}/json/version", timeout=3) as r:
            return json.loads(r.read()).get("Browser", "chrome")
    except Exception:
        return None

def _kill_stray_debug():
    """Tue UNIQUEMENT les chrome.exe lances sur NOTRE profil dedie (pas ton Chrome perso)."""
    try:
        import getpass  # noqa
        out = subprocess.run(
            ["wmic", "process", "where", "name='chrome.exe'", "get", "ProcessId,CommandLine"],
            capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return
    for line in out.splitlines():
        if "chrome_debug" in line or PROFILE in line:
            tok = line.split()
            pid = tok[-1] if tok and tok[-1].isdigit() else None
            if pid:
                try: subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=10)
                except Exception: pass

def main():
    if "--check" in sys.argv:
        print(f"debug port {PORT}:", _debug_ok() or "INJOIGNABLE")
        return
    if _debug_ok():
        print(f"Chrome debug deja actif sur :{PORT}. Rien a faire.")
        print(f'Exporte: $env:ALI_CDP_URL="http://localhost:{PORT}"')
        return
    exe = _chrome_exe()
    if not exe:
        print("chrome.exe introuvable. Installe Chrome ou ajuste _chrome_exe().")
        return
    os.makedirs(PROFILE, exist_ok=True)
    first = not os.path.exists(os.path.join(PROFILE, "Default"))
    _kill_stray_debug()
    print(f"Chrome : {exe}")
    print(f"Profil dedie : {PROFILE}")
    # PAS besoin de fermer ton Chrome perso: profil dedie + port = instance separee.
    args = [exe, f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
            "--no-first-run", "--no-default-browser-check", "--new-window",
            "https://lens.google.com/"]
    subprocess.Popen(args, close_fds=True)
    for _ in range(25):
        time.sleep(0.7)
        b = _debug_ok()
        if b:
            print(f"\nOK Chrome debug actif: {b} sur :{PORT}")
            if first:
                print("\n>>> 1er lancement: CONNECTE-TOI a Google dans cette fenetre Chrome.")
                print(">>> Verifie que lens.google.com s'ouvre SANS captcha.")
                print(">>> (login dans un vrai Chrome GUI => pas de blocage 'navigateur pas securise')")
            else:
                print("Profil deja connecte. Verifie lens.google.com sans captcha.")
            print(f'\nDetection:\n  $env:ALI_CDP_URL="http://localhost:{PORT}"')
            print("  python test_dropship_live.py")
            print("\nLAISSE cette fenetre Chrome OUVERTE pendant la detection.")
            return
    print("Port debug ne repond toujours pas.")
    print("=> un autre Chrome tourne peut-etre sur ce profil. Lance: python ali_chrome.py --check")

if __name__ == "__main__":
    main()
