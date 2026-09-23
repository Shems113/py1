import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from bs4 import BeautifulSoup

from Common.CLogger import CLogger as Logger
from Common.EEnums import ETorrentSource as TorrentSource
from Common.Helpers.HTML import parse_int
from Common.Helpers.Other import get_mirrors_from_env
from Common.Helpers.Torrent import parse_quality_torrent
from Common.Helpers.Web import build_absolute_url, clean_query
from Torrent.CCommonTorrent import CCommonTorrent, TorrentResult


class CScraperLimeTorrents(CCommonTorrent):
    def __init__(self,logger:Logger):
        mirrors = get_mirrors_from_env(
            "LIMETORRENTS_MIRRORS",
            [
                "https://www.limetorrents.lol",
                "https://www.limetorrents.asia",
                "https://www.limetorrents.co",
            ],
        )
        super().__init__(mirrors,logger=logger)

    @property
    def source_name(self) -> str:
        return TorrentSource.LIMETORRENTS.value

    def search(self, query: str, fetch_magnets: bool = True) -> list[TorrentResult]:
        encoded = urllib.parse.quote(clean_query(query))
        urls = [f"{mirror}/search/all/{encoded}/" for mirror in self.mirrors]

        def _parse(soup: BeautifulSoup, base_url: str) -> list[TorrentResult]:
            raw_results = []
            for row in soup.select("table.table2 tr"):
                if row.find("th"):
                    continue
                tds = row.find_all("td")
                if len(tds) < 5:
                    continue

                try:
                    title_elem = (
                        tds[0].select_one("div.tt-name a:nth-of-type(2)")
                        or tds[0].select_one("a[href*='-torrent-']")
                        or tds[0].select_one("a[href$='.html']")
                    )
                    if not title_elem:
                        continue

                    title_text = title_elem.text.strip()
                    detail_link = build_absolute_url(base_url, title_elem.get("href", ""))

                    magnet_elem = row.select_one("a[href^='magnet:']")
                    magnet_url = magnet_elem["href"] if magnet_elem and magnet_elem.get("href") else "N/A"

                    if magnet_url == "N/A":
                        hash_match = re.search(r"\b([a-fA-F0-9]{40})\b", str(row) + " " + detail_link)
                        if hash_match:
                            infohash = hash_match.group(1)
                            trackers_str = "".join(f"&tr={urllib.parse.quote(t)}" for t in self.trackers)
                            magnet_url = f"magnet:?xt=urn:btih:{infohash}&dn={urllib.parse.quote(title_text)}{trackers_str}"

                    # Extraction sécurisée des seeds / leechs (tds[.td-seed] et tds[.td-leech] si présents, sinon index)
                    seed_elem = row.select_one("td.tdnormal:nth-of-type(4), td.green, td.tdseed")
                    leech_elem = row.select_one("td.tdnormal:nth-of-type(5), td.red, td.tdleech")

                    seeds_raw = seed_elem.text if seed_elem else tds[-2].text
                    leechs_raw = leech_elem.text if leech_elem else tds[-1].text

                    s_parsed = parse_int(seeds_raw.replace(",", "").strip())
                    l_parsed = parse_int(leechs_raw.replace(",", "").strip())

                    # Extraction propre de la date et taille
                    date_raw = tds[1].text.split("-")[0].strip() if len(tds) > 1 and tds[1].text else "N/A"
                    size_raw = tds[2].text.strip() if len(tds) > 2 else "N/A"

                    raw_results.append({
                        "source": self.source_name,
                        "title": title_text,
                        "quality": parse_quality_torrent(title_text),
                        "seeds": s_parsed if s_parsed is not None else 0,
                        "leechs": l_parsed if l_parsed is not None else 0,
                        "size": size_raw,
                        "date": date_raw,
                        "magnet": magnet_url,
                        "detail_url": detail_link,
                    })
                except Exception as e:  # noqa: BLE001
                    self.logger.logError(f"Erreur parsing Limetorrents: {e}")
                    continue

            if not raw_results:
                return []

            if fetch_magnets:
                pending_items = [r for r in raw_results if r["magnet"] == "N/A" and r["detail_url"] != "N/A"]
                if pending_items:
                    with ThreadPoolExecutor(max_workers=min(10, len(pending_items))) as executor:
                        future_to_item = {
                            executor.submit(self.get_magnet_from_detail, item["detail_url"]): item
                            for item in pending_items
                        }
                        for future in as_completed(future_to_item):
                            item = future_to_item[future]
                            try:
                                item["magnet"] = future.result()
                            except Exception as e:  # noqa: BLE001
                                self.logger.logError(f"Erreur fetch detail magnet: {e}")

            return [TorrentResult(**r) for r in raw_results]

        return self.fetch_with_fallback(urls, _parse)
