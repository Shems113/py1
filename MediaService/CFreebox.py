"""Client minimal de l'API Freebox OS.

Usage :
    python -m MediaService.CFreebox register           # 1 fois, DEPUIS CHEZ TOI : enregistre l'application
    python -m MediaService.CFreebox status             # état de la connexion Internet
    python -m MediaService.CFreebox info               # modèle, firmware, uptime
    python -m MediaService.CFreebox reboot             # redémarre la Freebox (droit "Modification des réglages")
    python -m MediaService.CFreebox download "magnet:?xt=..."            # la Freebox télécharge le magnet
    python -m MediaService.CFreebox download "magnet:?xt=..." --dir "/Freebox/Films"
    python -m MediaService.CFreebox progress 12                          # avancement de la tâche 12
    python -m MediaService.CFreebox status --local     # force l'accès local (http://mafreebox.freebox.fr)

Depuis un autre script :
    from MediaService.CFreebox import add_magnet
    task_id = add_magnet("magnet:?xt=urn:btih:...")

Paramètres : section « Freebox » de Files/ExternalApiSettings.json
- LocalUrl, AppId, AppName, AppVersion : identité de l'application
- CaCertificate : autorités racines Freebox (PEM, une ligne par élément), nécessaire pour l'accès HTTPS à distance
- Token : écrit par `register` ; il vaut un mot de passe, ce fichier ne doit jamais être commité
"""

import argparse
import base64
import functools
import hashlib
import hmac
import ssl
import sys
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from Common.Helpers.ApiSettings import get_api_settings, update_api_settings
from Common.Helpers.System import CURRENT_MACHINE

SETTINGS_SECTION = "Freebox"
TIMEOUT = 10


def _settings()-> dict[str, Any]:
    return get_api_settings(SETTINGS_SECTION)


def _local_url()-> str:
    return _settings()["LocalUrl"]


class FreeboxTLSAdapter(HTTPAdapter):
    """Vérifie le certificat HTTPS de la box avec les autorités racines Freebox.

    Python 3.13+ active VERIFY_X509_STRICT par défaut, que les certificats Freebox
    ne respectent pas : on le désactive, sans désactiver la vérification elle-même.
    """

    def init_poolmanager(self, *args: Any, **kwargs: Any)-> None:
        # Autorités racines Freebox (le certificat HTTPS de la box n'est pas signé par une CA publique)
        ca_lines = _settings().get("CaCertificate") or []
        if not ca_lines:
            raise FileNotFoundError("Certificats Freebox absents (Freebox > CaCertificate dans ExternalApiSettings.json)")
        ctx = ssl.create_default_context(cadata="\n".join(ca_lines) + "\n")
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)


@functools.cache
def _session(secure: bool)-> requests.Session:
    session = requests.Session()
    if secure:
        session.mount("https://", FreeboxTLSAdapter())
    return session


def http(url: str)-> requests.Session:
    """Session adaptée à l'URL, créée au premier appel (et non à l'import du module).

    Le certificat Freebox (CaCertificate) n'est chargé que pour les URL https.
    """
    return _session(url.startswith("https://"))


class FreeboxError(Exception):
    """Erreur renvoyée par l'API Freebox (success = false)."""


def call(method: str, url: str, session_token: str | None = None, **kwargs: Any)-> Any:
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
def find_api_major(base_url: str, announced: int)-> str:
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


def register()-> None:
    """Demande l'autorisation à la Freebox et sauvegarde le jeton d'application."""
    settings = _settings()
    local_url = settings["LocalUrl"]
    info = http(local_url).get(f"{local_url}/api_version", timeout=TIMEOUT).json()
    announced = int(info["api_version"].split(".")[0])
    major = find_api_major(f"{local_url}{info['api_base_url']}", announced)
    print(f"ℹ️  API annoncée : v{announced} — version utilisée : v{major}")
    local_api = f"{local_url}{info['api_base_url']}v{major}"

    result = call("POST", f"{local_api}/login/authorize/", json={
        "app_id": settings["AppId"],
        "app_name": settings["AppName"],
        "app_version": settings["AppVersion"],
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
        raise FreeboxError(f"Autorisation refusée ou expirée (statut : {status})")

    update_api_settings(SETTINGS_SECTION, {"Token": {
        "AppToken": app_token,
        "ApiDomain": info["api_domain"],
        "HttpsPort": info["https_port"],
        "ApiBaseUrl": info["api_base_url"],
        "ApiMajor": major,
    }})
    print("✅ Application autorisée. Jeton enregistré dans ExternalApiSettings.json (Freebox > Token)")
    print("   Pense à cocher ses droits : Freebox OS > Paramètres > Gestion des accès > Applications")


# --------------------------------------------------------------------------- #
# Session (local ou à distance)
# --------------------------------------------------------------------------- #
def load_config()-> dict[str, Any]:
    """Jeton d'application enregistré par `register` (Freebox > Token).

    Lève FreeboxError (et non sys.exit) : appelée depuis l'application Flask, une
    sortie du programme arrêterait tout le serveur."""
    token = _settings().get("Token") or {}
    if not token.get("AppToken"):
        raise FreeboxError(
            "Aucun jeton : lance d'abord `python -m MediaService.CFreebox register` depuis chez toi.")
    return token


def api_url(config: dict[str, Any], local: bool)-> str:
    """URL de base de l'API : HTTP en local, HTTPS (certificat Freebox, vérifié via CaCertificate) à distance."""
    root = _local_url() if local else f"https://{config['ApiDomain']}:{config['HttpsPort']}"
    return f"{root}{config['ApiBaseUrl']}v{config['ApiMajor']}"


def open_session(config: dict[str, Any], base: str)-> str:
    """Ouvre une session : password = HMAC-SHA1(app_token, challenge)."""
    challenge = call("GET", f"{base}/login/")["challenge"]
    password = hmac.new(config["AppToken"].encode(), challenge.encode(), hashlib.sha1).hexdigest()
    result = call("POST", f"{base}/login/session/", json={"app_id": _settings()["AppId"], "password": password})
    return result["session_token"]


def connect(local: bool = False)-> tuple[str, str]:
    """Ouvre une session et renvoie (URL de base de l'API, jeton de session)."""
    config = load_config()
    base = api_url(config, local)
    return base, open_session(config, base)


# --------------------------------------------------------------------------- #
# Téléchargements (droit « Accès au gestionnaire de téléchargements »)
# --------------------------------------------------------------------------- #
def add_magnet(magnet: str, download_dir: str | None = None, local: bool = False)-> int:
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


def get_default_download_dir(base: str, session_token: str)-> str:
    """Dossier de téléchargement par défaut de la box (disque dur ou clé USB configuré
    dans Freebox OS), décodé du base64 : ex. « /Disque dur/Téléchargements »."""
    config = call("GET", f"{base}/downloads/config/", session_token)
    return base64.b64decode(config["download_dir"]).decode("utf-8")


def get_download(task_id: int, local: bool = False)-> dict[str, Any]:
    """Renvoie l'état d'une tâche : name, status, size, rx_pct, eta, rx_rate..."""
    base, token = connect(local)
    return call("GET", f"{base}/downloads/{task_id}", token)


# --------------------------------------------------------------------------- #
# Commandes
# --------------------------------------------------------------------------- #
def cmd_status(base: str, token: str)-> None:
    c = call("GET", f"{base}/connection/", token)
    print(f"État       : {c.get('state')}")
    print(f"Média      : {c.get('media')}")
    print(f"IPv4       : {c.get('ipv4')}  (ports {c.get('ipv4_port_range')})")
    print(f"IPv6       : {c.get('ipv6')}")
    print(f"Débit ↓/↑  : {c.get('rate_down', 0) / 1e6:.2f} / {c.get('rate_up', 0) / 1e6:.2f} Mo/s")


def cmd_info(base: str, token: str)-> None:
    s = call("GET", f"{base}/system/", token)
    for key in ("model_info", "firmware_version", "uptime", "mac", "serial"):
        value = s.get(key)
        if isinstance(value, dict):
            value = value.get("pretty_name", value)
        print(f"{key:<17}: {value}")


def cmd_reboot(base: str, token: str)-> None:
    call("POST", f"{base}/system/reboot/", token)
    print("🔄 Redémarrage demandé.")


def print_download(task: dict[str, Any])-> None:
    # rx_pct est exprimé en centièmes de pourcent (10000 = 100 %)
    pct = task.get("rx_pct", 0) / 100
    size_go = task.get("size", 0) / 1e9
    eta = task.get("eta", 0)
    print(f"#{task.get('id')}  {task.get('name')}")
    print(f"   {task.get('status')} — {pct:.1f} % de {size_go:.2f} Go — reste ~{eta // 60} min")


def main()-> None:
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