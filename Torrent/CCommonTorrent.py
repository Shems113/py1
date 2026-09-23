import re
import urllib.parse
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from bs4 import BeautifulSoup
from Common.CLogger import CLogger as Logger
from curl_cffi import requests


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

    DEFAULT_HEADERS = {  # noqa: RUF012
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    DOH_URL = "https://1.1.1.1/dns-query"

    def __init__(
        self,
        mirrors: list[str],
        logger: Logger,
        trackers: list[str] | None = None):
        self.mirrors = mirrors
        # Liste consolidée de trackers publics haute disponibilité
        self.trackers = trackers or [
            "udp://tracker.opentrackr.org:1337/announce",
            "udp://open.demonii.com:1337/announce",
            "udp://tracker.openbittorrent.com:80",
            "udp://tracker.coppersurfer.tk:6969",
            "udp://glotorrents.pw:6969/announce",
            "udp://open.stealth.si:80/announce",
            "udp://tracker.torrent.eu.org:451/announce",
            "udp://tracker.moeking.me:6969/announce",
            "udp://explodie.org:6969/announce",
            "udp://ipv4.tracker.harry.lu:80/announce",
            "https://tracker.tamersrealm.com:443/announce",
        ]
        self.session = requests.Session(impersonate="chrome120")
        self.logger = logger

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Nom identifiant la source (ex: TorrentSource.YTS.value)."""

    @abstractmethod
    def search(self, query: str) -> list[TorrentResult]:
        """Méthode principale à implémenter pour chaque source."""

    def fetch_html(self, url: str, headers: dict | None = None, timeout: int = 8) -> BeautifulSoup | None:
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
            self.logger.log(f"[{self.source_name}] Réponse {res.status_code} pour {url}")
        except Exception as e:  # noqa: BLE001
            self.logger.logError(f"[{self.source_name}] Erreur d'accès à {url}: {e}")
        return None

    def fetch_with_fallback(
        self,
        urls: list[str],
        parse_fn: Callable[[BeautifulSoup, str], list[TorrentResult]],
        headers: dict | None = None,
    ) -> list[TorrentResult]:
        """Tente les différents miroirs jusqu'à obtenir des résultats valides."""
        for url in urls:
            soup = self.fetch_html(url, headers=headers)
            if soup:
                results = parse_fn(soup, url)
                if results:
                    return results
                self.logger.log(f"[{self.source_name}] {url} accessible mais 0 résultat extrait.")
        self.logger.log(f"[{self.source_name}] Tous les miroirs ont échoué : {urls}")
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

    def check_health(self, timeout: int = 6) -> dict[str, object]:
        """Vérifie l'accessibilité des miroirs de la source."""
        mirrors_status = []
        for mirror in self.mirrors:
            try:
                res = self.session.get(
                    mirror, headers=self.DEFAULT_HEADERS, doh_url=self.DOH_URL, timeout=timeout
                )
                mirrors_status.append({"url": mirror, "ok": res.status_code == 200, "status": res.status_code})
            except Exception as e:  # noqa: BLE001
                mirrors_status.append({"url": mirror, "ok": False, "status": None, "error": str(e)})

        healthy_count = sum(1 for m in mirrors_status if m["ok"])
        return {
            "source": self.source_name,
            "healthy": healthy_count,
            "total": len(self.mirrors),
            "mirrors": mirrors_status,
        }
