import urllib.parse

from Common.CLogger import CLogger as Logger
from Common.EEnums import ETorrentSource as TorrentSource
from Common.Helpers.ApiSettings import get_api_settings
from Common.Helpers.HTML import parse_int
from Common.Helpers.System import get_mirrors_from_env
from Common.Helpers.Torrent import build_magnet
from Torrent.CCommonTorrent import CCommonTorrent, TorrentResult


class CScraperYts(CCommonTorrent):
    def __init__(
        self,
        logger:Logger
        )-> None:
        # Endpoints et trackers : Files/ExternalApiSettings.json, sauf si YTS_ENDPOINTS est définie
        settings = get_api_settings("Torrent")["Yts"]
        mirrors = get_mirrors_from_env("YTS_ENDPOINTS", settings["Endpoints"])
        super().__init__(mirrors, logger, settings["Trackers"])

    @property
    def source_name(self)-> str:
        return TorrentSource.YTS.value

    def search(self, query: str)-> list[TorrentResult]:
        encoded = urllib.parse.quote(query.strip())

        for endpoint in self.mirrors:
            url = f"{endpoint}?query_term={encoded}&sort_by=seeds&limit=20"
            try:
                res = self.session.get(url, doh_url=self.doh_url, timeout=8)
                if res.status_code != 200:
                    self.logger.log_error(f"Erreur API YTS {endpoint}: HTTP {res.status_code}")
                    continue

                data = res.json()
                if data.get("status") != "ok":
                    self.logger.log_error(f"Erreur API YTS {endpoint}: {data.get('status_message', data.get('status'))}")
                    continue
                movies = data.get("data", {}).get("movies")
                if not movies:
                    # Réponse valide sans résultat : inutile d'interroger les autres endpoints
                    return []

                results = []
                for movie in movies:
                    title_long = movie.get("title_long", movie.get("title", ""))
                    movie_year = str(movie.get("year", "N/A"))

                    for t in movie.get("torrents", []):
                        quality_val = str(t.get("quality", "N/A"))
                        torrent_type = str(t.get("type", "")).lower()
                        hash_code = t.get("hash")
                        if not hash_code:
                            continue
                        magnet = build_magnet(hash_code, title_long, self.trackers)

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
