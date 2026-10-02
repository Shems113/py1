import urllib.parse
from datetime import datetime

from Common.CLogger import CLogger as Logger
from Common.EEnums import ETorrentSource as TorrentSource
from Common.Helpers.ApiSettings import get_api_settings
from Common.Helpers.HTML import parse_int
from Common.Helpers.System import get_mirrors_from_env
from Common.Helpers.Torrent import build_magnet, format_bytes, parse_quality_torrent
from Common.Helpers.Web import clean_query
from Torrent.CCommonTorrent import CCommonTorrent, TorrentResult


class CScraperThePirateBay(CCommonTorrent):
    def __init__(
        self,
        logger:Logger
        )-> None:
        # Miroirs : Files/ExternalApiSettings.json, sauf si THEPIRATEBAY_MIRRORS est définie
        settings = get_api_settings("Torrent")["ThePirateBay"]
        mirrors = get_mirrors_from_env("THEPIRATEBAY_MIRRORS", settings["Mirrors"])
        self.detail_url: str = settings["DetailUrl"]
        super().__init__(mirrors,logger=logger)

    @property
    def source_name(self)-> str:
        return TorrentSource.THEPIRATEBAY.value

    def search(self, query: str)-> list[TorrentResult]:
        # Miroirs essayés dans l'ordre (THEPIRATEBAY_MIRRORS) : l'URL était auparavant
        # écrite en dur, la variable d'environnement n'avait aucun effet
        encoded = urllib.parse.quote(clean_query(query))
        for mirror in self.mirrors:
            results = self._search_mirror(f"{mirror.rstrip('/')}/q.php?q={encoded}")
            if results:
                return results
        return []

    def _search_mirror(self, url: str)-> list[TorrentResult]:
        try:
            res = self.session.get(url, doh_url=self.doh_url, timeout=8)
            if res.status_code != 200:
                return []

            data = res.json()
            if not isinstance(data, list) or len(data) == 0:
                return []

            results = []
            for item in data:
                if item.get("id") == "0":
                    continue

                title = item.get("name", "N/A")
                infohash = item.get("info_hash")
                if not infohash:
                    continue

                magnet = build_magnet(infohash, title, self.trackers)
                formatted_size = format_bytes(parse_int(item.get("size", 0)))

                # Fix Date : conversion du Timestamp UNIX (int) vers YYYY-MM-DD
                raw_added = parse_int(item.get("added", 0))
                if raw_added and raw_added > 0:
                    formatted_date = datetime.fromtimestamp(raw_added).astimezone().strftime("%Y-%m-%d")
                else:
                    formatted_date = "N/A"

                results.append(
                    TorrentResult(
                        source=self.source_name,
                        title=title,
                        quality=parse_quality_torrent(title),
                        seeds=parse_int(item.get("seeders", 0)),
                        leechs=parse_int(item.get("leechers", 0)),
                        size=formatted_size,
                        date=formatted_date,
                        magnet=magnet,
                        detail_url=f"{self.detail_url}{item.get('id')}",
                    )
                )
            return results
        except Exception as e:  # noqa: BLE001
            self.logger.log_error(f"Erreur API TPB ({url.split('?')[0]}): {e}")
            return []
