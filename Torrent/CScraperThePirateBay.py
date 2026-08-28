from datetime import datetime
import logging
import urllib.parse
from typing import List

from Common.EEnums import ETorrentSource as TorrentSource
from Common.Helper import (
    clean_query,
    get_mirrors_from_env,
    parse_int,
    parse_quality,
)
from Torrent.CCommonTorrent import CCommonTorrent, TorrentResult

logger = logging.getLogger(__name__)


class CScraperThePirateBay(CCommonTorrent):
    def __init__(self):
        mirrors = get_mirrors_from_env(
            "THEPIRATEBAY_MIRRORS",
            ["https://apibay.org"],
        )
        super().__init__(mirrors)

    @property
    def source_name(self) -> str:
        return TorrentSource.THEPIRATEBAY.value

    def search(self, query: str) -> List[TorrentResult]:
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
                    formatted_date = datetime.fromtimestamp(raw_added).strftime("%Y-%m-%d")
                else:
                    formatted_date = "N/A"

                results.append(
                    TorrentResult(
                        source=self.source_name,
                        title=title,
                        quality=parse_quality(title),
                        seeds=parse_int(item.get("seeders", 0)) or 0,
                        leechs=parse_int(item.get("leechers", 0)) or 0,
                        size=formatted_size,
                        date=formatted_date,
                        magnet=magnet,
                        detail_url=f"https://thepiratebay.org/description.php?id={item.get('id')}",
                    )
                )
            return results
        except Exception as e:
            logger.debug(f"Erreur API TPB: {e}")
            return []

    @staticmethod
    def _format_bytes(size_in_bytes: int) -> str:
        """Convertit une taille en octets vers MB, GB, TB, etc."""
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if abs(size_in_bytes) < 1024.0:
                return f"{size_in_bytes:.2f} {unit}"
            size_in_bytes /= 1024.0
        return f"{size_in_bytes:.2f} PB"