import logging
import re
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from bs4 import BeautifulSoup
from curl_cffi import requests

logger = logging.getLogger(__name__)


@dataclass
class TorrentResult:
    source: str
    title: str
    quality: str
    seeds: int
    leechs: int
    size: str
    date: str
    magnet: str
    detail_url: str


class CCommonTorrent(ABC):
    """Classe abstraite représentant un scraper de source Torrent."""

    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    DOH_URL = "https://1.1.1.1/dns-query"

    def __init__(self, mirrors: List[str], trackers: Optional[List[str]] = None):
        self.mirrors = mirrors
        self.trackers = trackers or [
            "udp://tracker.opentrackr.org:1337/announce",
            "udp://open.stealth.si:80/announce",
            "udp://tracker.torrent.eu.org:451/announce",
        ]
        self.session = requests.Session(impersonate="chrome120")

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Nom identifiant la source (ex: TorrentSource.YTS.value)."""
        pass

    @abstractmethod
    def search(self, query: str) -> List[TorrentResult]:
        """Méthode principale à implémenter pour chaque source."""
        pass

    def fetch_html(self, url: str, headers: Optional[dict] = None, timeout: int = 8) -> Optional[BeautifulSoup]:
        """Effectue la requête HTTP et retourne un objet BeautifulSoup."""
        req_headers = {**self.DEFAULT_HEADERS, **(headers or {})}
        try:
            res = self.session.get(
                url,
                headers=req_headers,
                doh_url=self.DOH_URL,
                timeout=timeout,
            )
            if res.status_code == 200:
                return BeautifulSoup(res.text, "html.parser")
            logger.warning(f"[{self.source_name}] Réponse {res.status_code} pour {url}")
        except Exception as e:
            logger.warning(f"[{self.source_name}] Erreur d'accès à {url}: {e}")
        return None

    def fetch_with_fallback(
        self,
        urls: List[str],
        parse_fn: Callable[[BeautifulSoup, str], List[TorrentResult]],
        headers: Optional[dict] = None,
    ) -> List[TorrentResult]:
        """Tente les différents miroirs jusqu'à obtenir des résultats valides."""
        for url in urls:
            soup = self.fetch_html(url, headers=headers)
            if soup:
                results = parse_fn(soup, url)
                if results:
                    return results
                logger.warning(f"[{self.source_name}] {url} accessible mais 0 résultat extrait.")
        logger.warning(f"[{self.source_name}] Tous les miroirs ont échoué : {urls}")
        return []

    def get_magnet_from_detail(self, detail_url: str) -> str:
        """Extrait un lien magnet depuis une page de détail (balise ou regex InfoHash)."""
        soup = self.fetch_html(detail_url, timeout=6)
        if not soup:
            return "N/A"

        magnet_a = soup.select_one("a[href^='magnet:']")
        if magnet_a and magnet_a.get("href"):
            return magnet_a["href"]

        match = re.search(r'\b([a-fA-F0-9]{40})\b', str(soup))
        if match:
            infohash = match.group(1)
            trackers_str = "".join(f"&tr={urllib.parse.quote(t)}" for t in self.trackers)
            return f"magnet:?xt=urn:btih:{infohash}{trackers_str}"

        return "N/A"

    def check_health(self, timeout: int = 6) -> Dict[str, object]:
        """Vérifie l'accessibilité des miroirs de la source."""
        mirrors_status = []
        for mirror in self.mirrors:
            try:
                res = self.session.get(
                    mirror, headers=self.DEFAULT_HEADERS, doh_url=self.DOH_URL, timeout=timeout
                )
                mirrors_status.append({"url": mirror, "ok": res.status_code == 200, "status": res.status_code})
            except Exception as e:
                mirrors_status.append({"url": mirror, "ok": False, "status": None, "error": str(e)})

        healthy_count = sum(1 for m in mirrors_status if m["ok"])
        return {
            "source": self.source_name,
            "healthy": healthy_count,
            "total": len(self.mirrors),
            "mirrors": mirrors_status,
        }