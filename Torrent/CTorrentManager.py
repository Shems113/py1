from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from Common.CLogger import CLogger as Logger
from Common.EEnums import ETorrentSource as TorrentSource
from Common.Helpers.Dataframe import data_frame_from_csv, data_frame_to_csv
from Common.Helpers.Torrent import (
    select_best_quality_torrent,
    select_torrents_to_download,
)
from Torrent.CCommonTorrent import CCommonTorrent, TorrentResult
from Torrent.CFreeboxDownloader import CFreeboxDownloader
from Torrent.CScraperLimeTorrents import CScraperLimeTorrents
from Torrent.CScraperThePirateBay import CScraperThePirateBay
from Torrent.CScraperTorznab import CScraperTorznab
from Torrent.CScraperYts import CScraperYts
from Torrent.CTorrentDownloader import CTorrentDownloader

DEFAULT_LOCAL_OUTPUT_DIR = r"C:\Users\SEDOUD\Videos\New\Torrent"


class CTorrentManager:
    def __init__(
        self,
        logs_path: Path | str
        )-> None:
        # Enregistrement dynamique de l'ensemble des scrapers
        self.logger = Logger([str(logs_path)])
        self.scrapers: dict[TorrentSource, CCommonTorrent] = {
            TorrentSource.YTS: CScraperYts(self.logger),
            TorrentSource.LIMETORRENTS: CScraperLimeTorrents(self.logger),
            TorrentSource.THEPIRATEBAY: CScraperThePirateBay(self.logger),
            TorrentSource.TORZNAB: CScraperTorznab(self.logger),
        }

    def check_sources_health(
        self,
        timeout: int = 6
        )-> dict[str, dict[str, object]]:
        """Contrôle l'état de santé de l'ensemble des sources enregistrées."""
        results = {}
        with ThreadPoolExecutor(max_workers=len(self.scrapers)) as executor:
            future_to_source = {
                executor.submit(scraper.check_health, timeout): source
                for source, scraper in self.scrapers.items()
            }
            for future in as_completed(future_to_source):
                source = future_to_source[future]
                try:
                    res = future.result()
                    results[source.value] = res
                    self.logger.log(f"[{source.value}] {res['healthy']}/{res['total']} miroir(s)/endpoint(s) accessible(s)")
                except Exception as e:  # noqa: BLE001
                    self.logger.log_error(f"Erreur lors du health check de {source.value}: {e}")
        return results

    def search_torrents(
        self,
        query: str,
        sources: TorrentSource | list[TorrentSource] = TorrentSource.ALL,
        export_csv: bool = False,
        csv_path: str | Path | None = None,
        max_workers: int = 5
        )-> pd.DataFrame:
        """Lance une recherche parallélisée sur les sources sélectionnées."""
        if isinstance(sources, TorrentSource):
            selected = list(self.scrapers.keys()) if sources == TorrentSource.ALL else [sources]
        else:
            selected = [s for s in sources if s != TorrentSource.ALL]

        all_results: list[TorrentResult] = []

        with ThreadPoolExecutor(max_workers=min(max_workers, len(selected) or 1)) as executor:
            future_to_src = {
                executor.submit(self.scrapers[src].search, query): src
                for src in selected
                if src in self.scrapers
            }

            for future in as_completed(future_to_src):
                src = future_to_src[future]
                try:
                    results = future.result()
                    all_results.extend(results)
                    self.logger.log(f"[{src.value}] Résultats récupérés : {len(results)}")
                except Exception as e:  # noqa: BLE001
                    self.logger.log_error(f"Erreur sur {src.value}: {e}")

        if not all_results:
            return pd.DataFrame()

        df = pd.DataFrame([asdict(r) for r in all_results]).reset_index(drop=True)

        if export_csv and csv_path:
            data_frame_to_csv(df, csv_path, ";")
            self.logger.log(f"Export CSV réalisé : {csv_path} ({len(df)} entrées)")

        return df

    def search_film_torrent(
        self,
        name: str,
        year: int | str | None = None
        )-> pd.DataFrame:
        """Formate la requête puis recherche sur l'ensemble des sources."""
        str_year = str(year).strip() if year is not None and str(year).strip().isdigit() else ""
        query = f"{name.strip()} {str_year}".strip()
        try:
            self.logger.log(f"Search torrents with query: {query}")
            return self.search_torrents(query, TorrentSource.ALL , export_csv=False)
        except Exception as e:  # noqa: BLE001
            self.logger.log_error(f"Error {e} for {query}")
            return pd.DataFrame([{"error": str(e)}])

    def search_film_torrents(
        self,
        tmdb_id: int,
        name: str,
        year: int | str | None = None
        )-> pd.DataFrame:
        """Tente une recherche avec l'année puis retente sans l'année en cas d'échec."""
        for y in [year, None]:
            df = self.search_film_torrent(name, y)
            if not df.empty and "error" not in df.columns:
                df.insert(0, "tmdb_id", tmdb_id)
                return df
        return pd.DataFrame()

    def route_search_films_torrents(
        self,
        input_file: str | Path,
        output_file: str | Path,
        max_workers: int = 2
        )-> bool:
        """Traite une liste de films issue d'un fichier CSV en parallèle.

        max_workers limité à 2 : chaque film interroge déjà 4 sources en parallèle, dont
        certaines avec leurs propres threads (jusqu'à ~400 requêtes simultanées avec 4 films,
        de quoi se faire bloquer par les sites)."""
        df_input = data_frame_from_csv(input_file, ";")
        if df_input.empty:
            return False

        results = []

        def process_row(row):
            return self.search_film_torrents(
                row.get("tmdb_id", 0), row.get("name", ""), row.get("year", None)
            )

        rows = [row for _, row in df_input.iterrows()]
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_row, row) for row in rows]
            for future in as_completed(futures):
                try:
                    res_df = future.result()
                    if not res_df.empty:
                        results.append(res_df)
                except Exception as e:  # noqa: BLE001
                    self.logger.log_error(f"Erreur lors du traitement d'un film: {e}")

        if results:
            final_df = pd.concat(results, ignore_index=True)
            final_df = final_df.sort_values(by=["tmdb_id", "seeds"], ascending=[True, False])
            data_frame_to_csv(final_df, output_file, ";")
            return True

        return False

    def route_download_films(
        self,
        input_file: str | Path,
        output_file: str | Path
        )-> bool:
        return self._download_films_from_magnets(input_file, output_file, False)

    def route_download_films_from_freebox(
        self,
        input_file: str | Path,
        output_file: str | Path
        )-> bool:
        return self._download_films_from_magnets(input_file, output_file, True)

    def _download_films_from_magnets(
        self,
        input_file: str | Path,
        output_file: str | Path,
        is_from_freebox: bool
        )-> bool:
        """Sélectionne les meilleurs torrents et lance leurs transferts via CTorrentDownloader ou la Freebox."""
        df_input = data_frame_from_csv(input_file, ";")
        df_best = select_best_quality_torrent(df_input)

        if df_best.empty:
            self.logger.log("Aucun torrent sélectionné pour le transfert.")
            return False

        data_frame_to_csv(df_best, output_file, ";")
        magnet_data = {
            str(row.magnet): f"{row.tmdb_id if pd.notna(row.tmdb_id) else '0'}_{row.name}"
            for row in select_torrents_to_download(df_best).itertuples()
        }

        if not magnet_data:
            self.logger.log("Aucun lien magnet valide trouvé dans la sélection.")
            return False

        # Freebox : dossier de téléchargement par défaut de la box (disque dur / clé USB).
        # Local : dossier Vidéos habituel.
        # (Auparavant, le chemin Windows était aussi passé à la Freebox, qui ne le connaît pas.)
        try:
            # Dans le try : la connexion à la Freebox peut échouer dès la création
            downloader: CFreeboxDownloader | CTorrentDownloader = (
                CFreeboxDownloader(magnet_data=magnet_data) if is_from_freebox
                else CTorrentDownloader(
                    magnet_data=magnet_data,
                    output_dir=DEFAULT_LOCAL_OUTPUT_DIR,
                )
            )
            downloader.download_from_magnets()
        except KeyboardInterrupt:
            self.logger.log("\nTéléchargement interrompu par l'utilisateur.")
            return False
        except Exception as e:  # noqa: BLE001
            self.logger.log_error(f"\nErreur durant le téléchargement : {e}")
            return False

        return True

if __name__ == "__main__":
    manager = CTorrentManager("")

    # Test de santé des sources
    manager.check_sources_health()

    # Test de recherche globale sur toutes les sources
    manager.search_torrents(
        "Star Wars 1977",
        sources=TorrentSource.ALL,
        csv_path="torrentResult.csv",
        export_csv=True,
    )
