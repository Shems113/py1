import os
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from Common.CLogger import CLogger as Logger
from Common.EEnums import ETorrentSource as TorrentSource
from Common.Helpers.HTML import parse_size
from Common.Helpers.Torrent import parse_quality_torrent
from Torrent.CCommonTorrent import CCommonTorrent, TorrentResult


class CScraperTorznab(CCommonTorrent):
    """Scraper Torznab parallélisé pour Prowlarr."""

    DEFAULT_API_URL = "http://127.0.0.1:9696"
    DEFAULT_API_KEY = "f1d2663b9a944fdfaf211814b83ca131"
    INDEXER_IDS = {  # noqa: RUF012
        1: "The Pirate Bay",
        2: "YTS",
        # 3: "LimeTorrents",
        # 4: "Knaben",
        # 5: "BigFANGroup",
        6: "Zamunda RIP",
        # 7: "Torrent9",
        # 8: "TorrentDownload",
        # 9: "World-torrent",
        10: "Torrent Downloads"
    }

    def __init__(
        self,
        logger:Logger,
        api_url: str | None = None,
        api_key: str | None = None,
        cat: str = "2000,2010,2020,2030,2040,2045,2050,2060",
        max_workers: int = 10
    ):
        raw_url = (
            api_url
            or os.getenv("PROWLARR_API_URL")
            or os.getenv("TORZNAB_API_URL")
            or self.DEFAULT_API_URL
        )
        parsed = urllib.parse.urlparse(raw_url)
        self.api_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else raw_url.rstrip("/")

        self.api_key = (
            api_key
            or os.getenv("PROWLARR_API_KEY")
            or os.getenv("TORZNAB_API_KEY")
            or self.DEFAULT_API_KEY
        )

        self.cat = cat
        self.max_workers = max_workers
        super().__init__(mirrors=[self.api_url],logger=logger)

    @property
    def source_name(self) -> str:
        return TorrentSource.TORZNAB.value

    def _build_search_url_for_indexer(self, query: str, indexer_id: int) -> str:
        endpoint = f"{self.api_url}/{indexer_id}/api"
        params = {
            "apikey": self.api_key,
            "t": "search",
            "q": query,
            "cat": self.cat,
        }
        return f"{endpoint}?{urllib.parse.urlencode(params)}"

    def _search_single_indexer(self, query: str, indexer_id: int) -> list[TorrentResult]:
        """Interroge un seul indexeur Prowlarr et formate correctement la taille."""
        url = self._build_search_url_for_indexer(query, indexer_id)
        results: list[TorrentResult] = []

        try:
            res = requests.get(url, timeout=8)
            if res.status_code != 200:
                return []

            content = res.content.strip()
            if not content.startswith(b"<?xml") and not content.startswith(b"<rss"):
                return []

            root = ET.fromstring(content)
            channel = root.find("channel")
            if channel is None:
                return []

            for item in channel.findall("item"):
                title = item.findtext("title", "N/A")
                detail_url = item.findtext("link", "N/A")
                pub_date = item.findtext("pubDate", "N/A")
                description = item.findtext("description", "")

                size_bytes = 0
                raw_size_text = ""
                seeds = 0
                leechs = 0
                magnet = "N/A"
                infohash = ""

                # 1. Extraction directe via <size> ou <enclosure length="...">
                size_elem = item.find("size")
                if size_elem is not None and size_elem.text:
                    if size_elem.text.isdigit():
                        size_bytes = int(size_elem.text)
                    else:
                        raw_size_text = size_elem.text

                enclosure = item.find("enclosure")
                enclosure_url = ""
                if enclosure is not None:
                    enclosure_url = enclosure.attrib.get("url", "")
                    if size_bytes == 0 and not raw_size_text:
                        length_val = enclosure.attrib.get("length")
                        if length_val and length_val.isdigit():
                            size_bytes = int(length_val)

                # Lien Magnet direct
                guid_elem = item.find("guid")
                if guid_elem is not None and guid_elem.text and guid_elem.text.startswith("magnet:"):
                    magnet = guid_elem.text
                elif enclosure_url.startswith("magnet:"):
                    magnet = enclosure_url

                # 2. Balayage des attributs torznab/jackett
                for child in item:
                    tag_name = child.tag.split("}")[-1].lower() if "}" in child.tag else child.tag.lower()

                    if tag_name == "attr":
                        attr_name = child.attrib.get("name", "").lower()
                        attr_val = child.attrib.get("value", "")

                        if attr_name in ("seeders", "seeds"):
                            seeds = int(attr_val) if attr_val.isdigit() else 0
                        elif attr_name in ("leechers", "leechs", "peers"):
                            leechs = int(attr_val) if attr_val.isdigit() else 0
                        elif attr_name in ("size", "bytes") and size_bytes == 0 and not raw_size_text:
                            if attr_val.isdigit():
                                size_bytes = int(attr_val)
                            else:
                                raw_size_text = attr_val
                        elif attr_name in ("magneturl", "downloadvolumefactor") and attr_val.startswith("magnet:"):
                            magnet = attr_val
                        elif attr_name == "infohash":
                            infohash = attr_val

                    elif tag_name in ("seeders", "seeds") and child.text and child.text.isdigit():
                        seeds = int(child.text)
                    elif tag_name in ("leechers", "leechs") and child.text and child.text.isdigit():
                        leechs = int(child.text)

                # 3. Formattage de la taille
                size_str = "N/A"
                if size_bytes > 0:
                    gb = size_bytes / (1024 ** 3)
                    if gb >= 1:
                        size_str = f"{gb:.2f} GB"
                    else:
                        mb = size_bytes / (1024 ** 2)
                        size_str = f"{mb:.2f} MB"
                elif raw_size_text:
                    # Ajout du préfixe "Size: " pour être accepté par parse_size
                    parsed = parse_size(f"Size: {raw_size_text}")
                    size_str = parsed if parsed != "N/A" else raw_size_text
                elif description:
                    parsed = parse_size(description)
                    if parsed != "N/A":
                        size_str = parsed

                # 4. Fallbacks Magnet
                if magnet == "N/A":
                    if infohash:
                        magnet = f"magnet:?xt=urn:btih:{infohash}&dn={urllib.parse.quote(title)}"
                    elif enclosure_url:
                        magnet = enclosure_url
                    elif detail_url != "N/A":
                        magnet = detail_url

                # Nom du tracker d'origine
                origin_indexer = f"Indexer-{indexer_id}"
                for child in item:
                    tag_name = child.tag.split("}")[-1].lower() if "}" in child.tag else child.tag.lower()
                    if tag_name in ("prowlarrindexer", "jackettindexer") and child.text:
                        origin_indexer = child.text
                        break

                results.append(
                    TorrentResult(
                        source=f"Torznab:{origin_indexer}",
                        title=title,
                        quality=parse_quality_torrent(title),
                        seeds=seeds,
                        leechs=leechs,
                        size=size_str,
                        date=pub_date,
                        magnet=magnet,
                        detail_url=detail_url,
                    )
                )

        except Exception as e:  # noqa: BLE001
            self.logger.logError(f"[{self.source_name}] Erreur indexeur {indexer_id} : {e}")

        return results

    def search(self, query: str) -> list[TorrentResult]:
        if not self.api_key:
            self.logger.logError(f"[{self.source_name}] Clé API manquante.")
            return []

        all_results: list[TorrentResult] = []

        # Exécution en parallèle de la recherche sur les 10 indexeurs
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_id = {
                executor.submit(self._search_single_indexer, query, idx_id): idx_id
                for idx_id in self.INDEXER_IDS
            }

            for future in as_completed(future_to_id):
                try:
                    res = future.result()
                    if res:
                        all_results.extend(res)
                except Exception as e:  # noqa: BLE001
                    idx_id = future_to_id[future]
                    self.logger.logError(f"[{self.source_name}] Exception worker pour indexeur {idx_id} : {e}")

        return all_results
