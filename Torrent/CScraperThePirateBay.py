import urllib.parse
from datetime import datetime

from Common.CLogger import CLogger as Logger
from Common.EEnums import ETorrentSource as TorrentSource
from Common.Helpers.HTML import parse_int
from Common.Helpers.Other import clean_query, get_mirrors_from_env
from Common.Helpers.Torrent import parse_quality_torrent
from Torrent.CCommonTorrent import CCommonTorrent, TorrentResult


class CScraperThePirateBay(CCommonTorrent):
    def __init__(self,logger:Logger):
        mirrors = get_mirrors_from_env(
            "THEPIRATEBAY_MIRRORS",
            ["https://apibay.org"],
        )
        super().__init__(mirrors,logger=logger)

    @property
    def source_name(self) -> str:
        return TorrentSource.THEPIRATEBAY.value

    def search(self, query: str) -> list[TorrentResult]:
        encoded = urllib.parse.quote(clean_query(query))
        url = f"https://apibay.org/q.php?q={encoded}"
        trackers_str = "".join(f"&tr={urllib.parse.quote(t)}" for t in self.trackers)

        try:
            res = self.session.get(url, doh_url=self.DOH_URL, timeout=8)
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

                magnet = f"magnet:?xt=urn:btih:{infohash}&dn={urllib.parse.quote(title)}{trackers_str}"

                # Fix Size : conversion des octets bruts (int) vers un format lisible
                raw_size = parse_int(item.get("size", 0)) or 0
                formatted_size = self._format_bytes(raw_size) if raw_size > 0 else "N/A"

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
                        seeds=parse_int(item.get("seeders", 0)) or 0,
                        leechs=parse_int(item.get("leechers", 0)) or 0,
                        size=formatted_size,
                        date=formatted_date,
                        magnet=magnet,
                        detail_url=f"https://thepiratebay.org/description.php?id={item.get('id')}",
                    )
                )
            return results
        except Exception as e:  # noqa: BLE001
            self.logger.logError(f"Erreur API TPB: {e}")
            return []

    @staticmethod
    def _format_bytes(size_in_bytes: int) -> str:
        """Convertit une taille en octets vers MB, GB, TB, etc."""
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if abs(size_in_bytes) < 1024.0:
                return f"{size_in_bytes:.2f} {unit}"
            size_in_bytes /= 1024.0
        return f"{size_in_bytes:.2f} PB"
