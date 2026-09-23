import re

import requests


def handle_response(
    response: requests.Response,
    include_json: bool = False
    ) -> dict:
    """Affiche le statut HTTP de la réponse et retourne son contenu JSON.

    Si la requête a échoué ou ne contient pas de JSON valide, retourne un dictionnaire vide.
    """
    status = "OK" if response.ok else "KO"
    print(f"{status} : {response.status_code}")
    data = response.json() if response.ok else {}
    if include_json and response.ok:
        print(data)
    return data

def build_absolute_url(
    base_url: str,
    path: str
    ) -> str:
    """Convertit un lien relatif (ex: '/torrent/123') en URL absolue avec le domaine racine de base_url."""
    if not path or path.startswith("http"):
        return path or "N/A"
    base_domain = "/".join(base_url.split("/")[:3])
    return f"{base_domain}{path}" if path.startswith("/") else f"{base_domain}/{path}"

def clean_query(query: str) -> str:
    """Nettoie une chaîne de recherche en supprimant la ponctuation gênante (deux-points, tirets, etc.)."""
    q = re.sub(r'[:\'\-_]', ' ', query)
    return ' '.join(q.split())