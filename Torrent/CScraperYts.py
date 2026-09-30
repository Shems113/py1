import urllib.parse

from Common.CLogger import CLogger as Logger
from Common.EEnums import ETorrentSource as TorrentSource
from Common.Helpers.HTML import parse_int
from Common.Helpers.System import get_mirrors_from_env
from Torrent.CCommonTorrent import CCommonTorrent, TorrentResult


class CScraperYts(CCommonTorrent):
    def __init__(
        self,
        logger:Logger
        )-> None:
        mirrors = get_mirrors_from_env(
            "YTS_ENDPOINTS",
            [
                "https://yts.mx/api/v2/list_movies.json",
                "https://yts.lt/api/v2/list_movies.json",
                "https://yts.do/api/v2/list_movies.json",
                "https://yts.ibit.to/api/v2/list_movies.json",
            ],
        )
        trackers = [
            "udp://open.demonii.com:1337/announce",
            "udp://tracker.openbittorrent.com:80",
            "udp://tracker.coppersurfer.tk:6969",
        ]
        super().__init__(mirrors, logger, trackers)

    @property
    def source_name(self)-> str:
        return TorrentSource.YTS.value

    def search(self, query: str)-> list[TorrentResult]:
        encoded = urllib.parse.quote(query.strip())
        trackers_str = "".join(f"&tr={urllib.parse.quote(t)}" for t in self.trackers)

        for endpoint in self.mirrors:
            url = f"{endpoint}?query_term={encoded}&sort_by=seeds&limit=20"
            try:
                res = self.session.get(url, doh_url=self.DOH_URL, timeout=8)
                if res.status_code != 200:
                    continue

                data = res.json()
                movies = data.get("data", {}).get("movies")
                if data.get("status") != "ok" or not movies:
                    continue

                results = []
                for movie in movies:
                    title_long = movie.get("title_long", movie.get("title", ""))
                    movie_year = str(movie.get("year", "N/A"))

                    for t in movie.get("torrents", []):
                        quality_val = str(t.get("quality", "N/A"))
                        torrent_type = str(t.get("type", "")).lower()
                        hash_code = t.get("hash")
                        magnet = f"magnet:?xt=urn:btih:{hash_code}&dn={urllib.parse.quote(title_long)}{trackers_str}"

                        results.append(
                            TorrentResult(
                                source=self.source_name,
                                title=f"{title_long} [{quality_val}] [{torrent_type}]",
                                quality=quality_val,
                                seeds=parse_int(t.get("seeds", 0)),
                                leechs=parse_int(t.get("peers", 0)),
                                size=t.get("size", "N/A"),
                                date=movie_year,
                                magnet=magnet,
                                detail_url=movie.get("url", "N/A"),
                            )
                        )
                if results:
                    return results
            except Exception as e:  # noqa: BLE001
                self.logger.log_error(f"Erreur API YTS {endpoint}: {e}")
        return []
