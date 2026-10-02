import re
import threading
import time
from collections.abc import Callable
from typing import Any

import requests

_QUERY_PUNCTUATION = re.compile(r"[:'\-_]")


def handle_response(
    response: requests.Response,
    include_json: bool = False
    )-> dict:
    """Affiche le statut HTTP de la réponse et retourne son contenu JSON.

    Si la requête a échoué ou ne contient pas de JSON valide, retourne un dictionnaire vide.
    """
    status = "OK" if response.ok else "KO"
    print(f"{status} : {response.status_code}")
    if not response.ok:
        return {}
    try:
        data = response.json()
    except ValueError:  # corps vide ou HTML (page d'erreur, Cloudflare…)
        print("Réponse non JSON")
        return {}
    if include_json:
        print(data)
    return data

def build_absolute_url(
    base_url: str,
    path: str
    )-> str:
    """Convertit un lien relatif (ex: '/torrent/123') en URL absolue avec le domaine racine de base_url."""
    if not path:
        return "N/A"
    if path.startswith(("http://", "https://")):
        return path
    scheme = base_url.split(":", 1)[0] if "://" in base_url else "https"
    if path.startswith("//"):  # URL relative au protocole (ex: //cdn.site.com/img.jpg)
        return f"{scheme}:{path}"
    base_domain = "/".join(base_url.split("/")[:3])
    return f"{base_domain}{path}" if path.startswith("/") else f"{base_domain}/{path}"

def clean_query(query: str)-> str:
    """Nettoie une chaîne de recherche en supprimant la ponctuation gênante (deux-points, tirets, etc.)."""
    return " ".join(_QUERY_PUNCTUATION.sub(" ", query).split())


class RateLimiter:
    """Limite de débit thread-safe : au plus un appel toutes les `interval` secondes,
    tous threads confondus. Le créneau est réservé sous verrou, l'attente se fait
    hors verrou (les autres threads ne sont pas bloqués pendant qu'un thread dort).

        acoustid = RateLimiter(1 / 3)   # 3 requêtes par seconde
        acoustid.wait()
    """

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._next_call = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_call)
            self._next_call = slot + self._interval
        if slot > now:
            time.sleep(slot - now)


_RETRY_STATUS = {429, 500, 502, 503, 504}

def fetch_json(
    url: str,
    data: dict[str, Any] | bytes | None = None,
    headers: dict[str, str] | None = None,
    limiter: RateLimiter | None = None,
    timeout: float = 20,
    retries: int = 3,
    allow_404: bool = False,
    retry_if: Callable[[Any], bool] | None = None,
    retry_delay: float = 5,
    )-> Any:
    """GET (ou POST si `data`) d'une API JSON, avec limite de débit et nouvelles tentatives.

    - erreurs réseau et HTTP 429 / 5xx : nouvelle tentative (attente 1 s, 2 s…)
    - retry_if(payload) vrai : nouvelle tentative après retry_delay secondes
      (API qui signalent un quota dépassé avec un HTTP 200, ex. Deezer)
    - HTTP 404 : None si allow_404, sinon RuntimeError
    """
    for attempt in range(retries):
        if limiter:
            limiter.wait()
        last = attempt == retries - 1
        try:
            response = requests.request("POST" if data is not None else "GET", url,
                                        data=data, headers=headers, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout):
            if last:
                raise
            time.sleep(2 ** attempt)
            continue
        if response.status_code == 404 and allow_404:
            return None
        if response.status_code in _RETRY_STATUS and not last:
            time.sleep(2 ** attempt)
            continue
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status_code} sur {url.split('?')[0]} : {response.text[:200]}")
        payload = response.json()
        if retry_if and retry_if(payload) and not last:
            time.sleep(retry_delay)
            continue
        return payload
    raise RuntimeError(f"{url.split('?')[0]} : échec après {retries} tentatives")
