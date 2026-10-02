import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from bs4 import BeautifulSoup
from curl_cffi import requests

from Common.CLogger import CLogger as Logger
from Common.Helpers.ApiSettings import get_api_settings
from Common.Helpers.Torrent import build_magnet


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

    def __init__(
        self,
        mirrors: list[str],
        logger: Logger,
        trackers: list[str] | None = None
        )-> None:
        self.mirrors = mirrors
        # Trackers publics et résolveur DNS-over-HTTPS : section « Torrent » de
        # Files/ExternalApiSettings.json
        settings = get_api_settings("Torrent")
        self.trackers = trackers or settings["Trackers"]
        self.doh_url: str = settings["DnsOverHttpsUrl"]
        self.logger = logger
        self._thread_local = threading.local()

    @property
    def session(self)-> requests.Session:
        """Session curl_cffi propre à chaque thread : une session curl n'est pas prévue pour
        être utilisée par plusieurs threads à la fois (LimeTorrents en lance jusqu'à 10)."""
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = self._thread_local.session = requests.Session(impersonate="chrome120")
        return session

    @property
    @abstractmethod
    def source_name(self)-> str:
        """Nom identifiant la source (ex: TorrentSource.YTS.value)."""

    @abstractmethod
    def search(self, query: str)-> list[TorrentResult]:
        """Méthode principale à implémenter pour chaque source."""

    def fetch_html(self, url: str, headers: dict | None = None, timeout: int = 8)-> BeautifulSoup | None:
        """Effectue la requête HTTP et retourne un objet BeautifulSoup."""
        req_headers = {**self.DEFAULT_HEADERS, **(headers or {})}
        try:
            res = self.session.get(
                url,
                headers=req_headers,
                doh_url=self.doh_url,
                timeout=timeout,
            )
            if res.status_code == 200:
                return BeautifulSoup(res.text, "html.parser")
            self.logger.log(f"[{self.source_name}] Réponse {res.status_code} pour {url}")
        except Exception as e:  # noqa: BLE001
            self.logger.log_error(f"[{self.source_name}] Erreur d'accès à {url}: {e}")
        return None

    def fetch_with_fallback(
        self,
        urls: list[str],
        parse_fn: Callable[[BeautifulSoup, str], list[TorrentResult]],
        headers: dict | None = None,
    )-> list[TorrentResult]:
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

    def get_magnet_from_detail(self, detail_url: str)-> str:
        """Extrait un lien magnet depuis une page de détail (balise ou regex InfoHash)."""
        soup = self.fetch_html(detail_url, timeout=6)
        if not soup:
            return "N/A"

        magnet_a = soup.select_one("a[href^='magnet:']")
        if magnet_a and magnet_a.get("href"):
            return magnet_a["href"]

        match = re.search(r'\b([a-fA-F0-9]{40})\b', str(soup))
        if match:
            return build_magnet(match.group(1), trackers=self.trackers)

        return "N/A"

    def check_health(self, timeout: int = 6)-> dict[str, object]:
        """Vérifie l'accessibilité des miroirs de la source."""
        mirrors_status = []
        for mirror in self.mirrors:
            try:
                res = self.session.get(
                    mirror, headers=self.DEFAULT_HEADERS, doh_url=self.doh_url, timeout=timeout
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
