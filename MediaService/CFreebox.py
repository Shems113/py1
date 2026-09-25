"""Client minimal de l'API Freebox OS.

Usage :
    python freebox_api.py register           # 1 fois, DEPUIS CHEZ TOI : enregistre l'application
    python freebox_api.py status             # état de la connexion Internet
    python freebox_api.py info               # modèle, firmware, uptime
    python freebox_api.py reboot             # redémarre la Freebox (droit "Modification des réglages")
    python freebox_api.py download "magnet:?xt=..."            # la Freebox télécharge le magnet
    python freebox_api.py download "magnet:?xt=..." --dir "/Freebox/Films"
    python freebox_api.py progress 12                          # avancement de la tâche 12
    python freebox_api.py status --local     # force l'accès local (http://mafreebox.freebox.fr)

Depuis un autre script :
    from MediaService.CFreebox import add_magnet
    task_id = add_magnet("magnet:?xt=urn:btih:...")

Fichiers dans FILES_DIR (py1\\Files par défaut) :
- freebox_ca.pem     : autorités racines Freebox, nécessaire pour l'accès HTTPS à distance
- freebox_token.json : créé par `register` ; il vaut un mot de passe, ne le commite jamais
"""

import argparse
import base64
import functools
import hashlib
import hmac
import json
import ssl
import sys
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from Common.Helpers.File import PROJECT_ROOT
from Common.Helpers.System import CURRENT_MACHINE

APP_ID = "fr.sedoud.freebox-client"
APP_NAME = "Freebox client Python"
APP_VERSION = "1.0"
LOCAL_URL = "http://mafreebox.freebox.fr"
# Dossier des fichiers de configuration (ici py1\Files, à adapter si besoin)
FILES_DIR = PROJECT_ROOT / "Files"
TOKEN_FILE = FILES_DIR / "freebox_token.json"
# Autorités racines Freebox (le certificat HTTPS de la box n'est pas signé par une CA publique)
CA_FILE = FILES_DIR / "freebox_ca.pem"
TIMEOUT = 10


class FreeboxTLSAdapter(HTTPAdapter):
    """Vérifie le certificat HTTPS de la box avec les autorités racines Freebox.

    Python 3.13+ active VERIFY_X509_STRICT par défaut, que les certificats Freebox
    ne respectent pas : on le désactive, sans désactiver la vérification elle-même.
    """

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        if not CA_FILE.is_file():
            raise FileNotFoundError(f"Certificats Freebox introuvables : {CA_FILE}")
        ctx = ssl.create_default_context(cafile=str(CA_FILE))
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)


@functools.cache
def _session(secure: bool) -> requests.Session:
    session = requests.Session()
    if secure:
        session.mount("https://", FreeboxTLSAdapter())
    return session


def http(url: str) -> requests.Session:
    """Session adaptée à l'URL, créée au premier appel (et non à l'import du module).

    Le certificat Freebox (freebox_ca.pem) n'est chargé que pour les URL https.
    """
    return _session(url.startswith("https://"))


class FreeboxError(Exception):
    """Erreur renvoyée par l'API Freebox (success = false)."""


def call(method: str, url: str, session_token: str | None = None, **kwargs: Any) -> Any:
    """Appelle l'API et renvoie le champ `result`, ou lève FreeboxError."""
    headers = {"X-Fbx-App-Auth": session_token} if session_token else {}
    response = http(url).request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
    data = response.json()
    if not data.get("success"):
        raise FreeboxError(f"{data.get('error_code')} : {data.get('msg')}")
    return data.get("result")


# --------------------------------------------------------------------------- #
# Enregistrement (une seule fois, sur le réseau local)
# --------------------------------------------------------------------------- #
def find_api_major(base_url: str, announced: int) -> str:
    """Première version d'API acceptée dans l'URL, en partant de la version annoncée.

    La box peut annoncer une version (ex. 16.1) sans encore l'accepter dans l'URL
    (erreur invalid_api_version) : on redescend jusqu'à trouver une version qui répond.
    """
    for major in range(announced, 0, -1):
        url = f"{base_url}v{major}/login/"
        data = http(url).get(url, timeout=TIMEOUT).json()
        if data.get("success"):
            return str(major)
        if data.get("error_code") != "invalid_api_version":
            raise FreeboxError(f"{data.get('error_code')} : {data.get('msg')}")
    raise FreeboxError("aucune version d'API acceptée par la Freebox")


def register() -> None:
    """Demande l'autorisation à la Freebox et sauvegarde le jeton d'application."""
    info = http(LOCAL_URL).get(f"{LOCAL_URL}/api_version", timeout=TIMEOUT).json()
    announced = int(info["api_version"].split(".")[0])
    major = find_api_major(f"{LOCAL_URL}{info['api_base_url']}", announced)
    print(f"ℹ️  API annoncée : v{announced} — version utilisée : v{major}")
    local_api = f"{LOCAL_URL}{info['api_base_url']}v{major}"

    result = call("POST", f"{local_api}/login/authorize/", json={
        "app_id": APP_ID,
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "device_name": CURRENT_MACHINE,
    })
    app_token, track_id = result["app_token"], result["track_id"]

    print("➡️  Valide la demande sur la Freebox (façade ou application Freebox Connect)…")
    while True:
        status = call("GET", f"{local_api}/login/authorize/{track_id}")["status"]
        if status != "pending":
            break
        time.sleep(2)

    if status != "granted":
        sys.exit(f"❌ Autorisation refusée ou expirée (statut : {status})")

    TOKEN_FILE.write_text(json.dumps({
        "app_token": app_token,
        "api_domain": info["api_domain"],
        "https_port": info["https_port"],
        "api_base_url": info["api_base_url"],
        "api_major": major,
    }, indent=2), encoding="utf-8")
    print(f"✅ Application autorisée. Jeton enregistré dans {TOKEN_FILE.name}")
    print("   Pense à cocher ses droits : Freebox OS > Paramètres > Gestion des accès > Applications")


# --------------------------------------------------------------------------- #
# Session (local ou à distance)
# --------------------------------------------------------------------------- #
def load_config() -> dict[str, Any]:
    if not TOKEN_FILE.is_file():
        sys.exit("❌ Aucun jeton : lance d'abord `python freebox_api.py register` depuis chez toi.")
    return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))


def api_url(config: dict[str, Any], local: bool) -> str:
    """URL de base de l'API : HTTP en local, HTTPS (certificat Freebox, vérifié via freebox_ca.pem) à distance."""
    root = LOCAL_URL if local else f"https://{config['api_domain']}:{config['https_port']}"
    return f"{root}{config['api_base_url']}v{config['api_major']}"


def open_session(config: dict[str, Any], base: str) -> str:
    """Ouvre une session : password = HMAC-SHA1(app_token, challenge)."""
    challenge = call("GET", f"{base}/login/")["challenge"]
    password = hmac.new(config["app_token"].encode(), challenge.encode(), hashlib.sha1).hexdigest()
    result = call("POST", f"{base}/login/session/", json={"app_id": APP_ID, "password": password})
    return result["session_token"]


def connect(local: bool = False) -> tuple[str, str]:
    """Ouvre une session et renvoie (URL de base de l'API, jeton de session)."""
    config = load_config()
    base = api_url(config, local)
    return base, open_session(config, base)


# --------------------------------------------------------------------------- #
# Téléchargements (droit « Accès au gestionnaire de téléchargements »)
# --------------------------------------------------------------------------- #
def add_magnet(magnet: str, download_dir: str | None = None, local: bool = False) -> int:
    """Ajoute un lien magnet au gestionnaire de téléchargements de la Freebox.

    download_dir : dossier sur le disque de la box (ex. "/Freebox/Téléchargements").
    Si None, la Freebox utilise son dossier de téléchargement par défaut.
    Renvoie l'identifiant de la tâche créée.
    """
    if not magnet.startswith("magnet:?"):
        raise ValueError(f"Lien magnet invalide : {magnet[:40]}…")
    base, token = connect(local)
    form = {"download_url": magnet}
    if download_dir:
        # L'API attend le chemin encodé en base64
        form["download_dir"] = base64.b64encode(download_dir.encode("utf-8")).decode("ascii")
    # Cet endpoint attend un formulaire (application/x-www-form-urlencoded), pas du JSON
    return call("POST", f"{base}/downloads/add", token, data=form)["id"]


def get_download(task_id: int, local: bool = False) -> dict[str, Any]:
    """Renvoie l'état d'une tâche : name, status, size, rx_pct, eta, rx_rate..."""
    base, token = connect(local)
    return call("GET", f"{base}/downloads/{task_id}", token)


# --------------------------------------------------------------------------- #
# Commandes
# --------------------------------------------------------------------------- #
def cmd_status(base: str, token: str) -> None:
    c = call("GET", f"{base}/connection/", token)
    print(f"État       : {c.get('state')}")
    print(f"Média      : {c.get('media')}")
    print(f"IPv4       : {c.get('ipv4')}  (ports {c.get('ipv4_port_range')})")
    print(f"IPv6       : {c.get('ipv6')}")
    print(f"Débit ↓/↑  : {c.get('rate_down', 0) / 1e6:.2f} / {c.get('rate_up', 0) / 1e6:.2f} Mo/s")


def cmd_info(base: str, token: str) -> None:
    s = call("GET", f"{base}/system/", token)
    for key in ("model_info", "firmware_version", "uptime", "mac", "serial"):
        value = s.get(key)
        if isinstance(value, dict):
            value = value.get("pretty_name", value)
        print(f"{key:<17}: {value}")


def cmd_reboot(base: str, token: str) -> None:
    call("POST", f"{base}/system/reboot/", token)
    print("🔄 Redémarrage demandé.")


def print_download(task: dict[str, Any]) -> None:
    # rx_pct est exprimé en centièmes de pourcent (10000 = 100 %)
    pct = task.get("rx_pct", 0) / 100
    size_go = task.get("size", 0) / 1e9
    eta = task.get("eta", 0)
    print(f"#{task.get('id')}  {task.get('name')}")
    print(f"   {task.get('status')} — {pct:.1f} % de {size_go:.2f} Go — reste ~{eta // 60} min")


def main() -> None:
    parser = argparse.ArgumentParser(description="Client API Freebox OS")
    parser.add_argument("command", choices=["register", "status", "info", "reboot", "download", "progress"])
    parser.add_argument("target", nargs="?", help="lien magnet (download) ou id de tâche (progress)")
    parser.add_argument("--dir", help='dossier de destination sur la box, ex. "/Freebox/Films"')
    parser.add_argument("--local", action="store_true", help="passer par http://mafreebox.freebox.fr")
    args = parser.parse_args()

    try:
        if args.command == "register":
            register()
        elif args.command == "download":
            if not args.target:
                parser.error('download attend un lien magnet entre guillemets : download "magnet:?xt=..."')
            task_id = add_magnet(args.target, args.dir, args.local)
            print(f"⬇️  Téléchargement ajouté (tâche #{task_id}). Suivi : progress {task_id}")
        elif args.command == "progress":
            if not args.target or not args.target.isdigit():
                parser.error("progress attend l'id de la tâche, ex. : progress 12")
            print_download(get_download(int(args.target), args.local))
        else:
            base, token = connect(args.local)
            {"status": cmd_status, "info": cmd_info, "reboot": cmd_reboot}[args.command](base, token)
    except ValueError as e:
        sys.exit(f"❌ {e}")
    except FreeboxError as e:
        sys.exit(f"❌ Erreur API : {e}")
    except requests.RequestException as e:
        sys.exit(f"❌ Freebox injoignable : {e}")
    except FileNotFoundError as e:
        sys.exit(f"❌ {e}")


if __name__ == "__main__":
    main()