from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

import pandas as pd
import requests

from Common.CLogger import CLogger as Logger
from Common.Helpers.ApiSettings import get_api_settings
from Common.Helpers.Dataframe import data_frame_from_csv, data_frame_to_csv
from Common.Helpers.Other import is_latin
from Common.Helpers.Web import RateLimiter, handle_response

T = TypeVar("T")

REQUEST_TIMEOUT = 30        # secondes : sans limite, une API muette bloquerait la route Flask
MAX_WORKERS = 8             # requêtes TMDB en parallèle
REQUESTS_PER_SECOND = 40    # limite TMDB : ~50 requêtes / seconde


class CTmdb:
    def __init__(
        self,
        logger: Logger
        )-> None:
        # URL et jeton : section « Tmdb » de Files/ExternalApiSettings.json
        settings = get_api_settings("Tmdb")
        self.tmdb_access_token = settings["AccessToken"]
        self.logger = logger
        self.tmdb_base_url = settings["BaseUrl"]
        self.tmdb_session = requests.Session()
        self.tmdb_session.headers.update(
            {
                "accept": "application/json",
                "Authorization": f"Bearer {self.tmdb_access_token}",
            }
        )
        self._rate_limiter = RateLimiter(1 / REQUESTS_PER_SECOND)

    # --- Outils ---

    def _get(self, path: str, params: dict[str, Any] | None = None)-> dict[str, Any]:
        """GET sur l'API TMDB, avec limite de débit (partagée entre threads) et délai maximal."""
        self._rate_limiter.wait()
        res = self.tmdb_session.get(f"{self.tmdb_base_url}{path}", params=params, timeout=REQUEST_TIMEOUT)
        return handle_response(res) or {}

    @staticmethod
    def _to_dataframe(records: Any, cols: list[str] | None = None)-> pd.DataFrame:
        """Résultats JSON -> DataFrame, restreint à `cols` si demandé.
        Sans résultat : une ligne vide avec ces colonnes (ou un DataFrame vide)."""
        if not records:
            return pd.DataFrame([dict.fromkeys(cols)]) if cols else pd.DataFrame()
        df = pd.json_normalize(records)
        existing_cols = [c for c in cols or [] if c in df.columns]
        return df[existing_cols] if existing_cols else df

    @staticmethod
    def _parallel(func: Callable[[Any], T], items: Iterable[Any])-> list[T]:
        """Applique func à chaque élément en parallèle, en conservant l'ordre."""
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            return list(executor.map(func, items))

    def _process_csv(
        self,
        input_file: str | Path,
        output_file: str | Path,
        input_cols: list[str],
        row_func: Callable[[Any], pd.DataFrame | None],
    )-> bool:
        """Lit le CSV d'entrée (colonnes input_cols, sans doublon), applique row_func à chaque
        ligne en parallèle et écrit la concaténation des résultats. False si aucun résultat."""
        df_input = data_frame_from_csv(input_file, ";")
        if df_input.empty:
            return False
        df_input = df_input[[c for c in input_cols if c in df_input.columns]].drop_duplicates()
        results = [df for df in self._parallel(row_func, df_input.itertuples()) if df is not None and not df.empty]
        if not results:
            return False
        data_frame_to_csv(pd.concat(results, ignore_index=True), output_file, ";")
        return True

    # --- Films ---

    def search_tmdb_movie(
        self, name: str, year: int | str | None = None, language: str | None = None
    )-> pd.DataFrame:
        """Recherche un film sur TMDB (langue originale par défaut)."""
        self.logger.log(f"Recherche du film '{name}' ({year or 'N/A'}) sur TMDB (VO)")
        params: dict[str, Any] = {"query": name, "page": 1}
        if language:
            params["language"] = language
        if year and str(year).isdigit():
            params["primary_release_year"] = int(year)

        data = self._get("/search/movie", params)
        self.logger.log(f"Nombre de résultats : {data.get('total_results', 0)}")
        return self._to_dataframe(data.get("results"))

    def get_tmdb_movie_detail(
        self,
        movie_id: int,
        language: str | None = None
    )-> pd.DataFrame:
        """Recherche des détails pour un film sur TMDB (langue originale par défaut)."""
        self.logger.log(f"Recherche des détails pour le film '{movie_id}' sur TMDB (VO)")
        data = self._get(f"/movie/{movie_id}", {"language": language} if language else None)
        return self._to_dataframe(data)

    def get_tmdb_movie_alternative_titles(
        self, movie_id: str, country: str | None = None
    )-> pd.DataFrame:
        """Recherche des titres alternatifs pour un film sur TMDB (langue originale par défaut)."""
        self.logger.log(f"Recherche des titres alternatifs pour le film '{movie_id}' sur TMDB (VO)")
        data = self._get(f"/movie/{movie_id}/alternative_titles", {"country": country} if country else None)
        titles = data.get("titles", [])
        self.logger.log(f"Nombre de titres alternatifs : {len(titles)}")
        return self._to_dataframe(titles)

    # --- Séries ---

    def search_tmdb_tv_show(
        self,
        name: str,
        year: int | str | None = None,
        cols: list[str] | None = None,
        language: str = "fr-FR",
    )-> pd.DataFrame:
        """Recherche une série TV sur TMDB (en Français par défaut)."""
        self.logger.log(f"Recherche de la série '{name}' ({year or 'N/A'}) sur TMDB (FR)")
        params: dict[str, Any] = {"query": name, "page": 1, "language": language}
        if year and str(year).isdigit():
            params["first_air_date_year"] = int(year)

        data = self._get("/search/tv", params)
        self.logger.log(f"Nombre de résultats : {data.get('total_results', 0)}")
        return self._to_dataframe(data.get("results"), cols)

    def get_tmdb_tv_detail(
        self, show_id: int, cols: list[str] | None = None, language: str = "fr-FR"
    )-> pd.DataFrame:
        data = self._get(f"/tv/{show_id}", {"language": language})
        if data:
            self.logger.log(f"Nombre de saisons : {data.get('number_of_seasons', 0)}")
            self.logger.log(f"Nombre total d'épisodes : {data.get('number_of_episodes', 0)}")
        return self._to_dataframe(data, cols)

    def get_tmdb_tv_season_detail(
        self, show_id: int, season_number: int, cols: list[str] | None = None, language: str = "fr-FR"
    )-> pd.DataFrame:
        episodes = self._get(f"/tv/{show_id}/season/{season_number}", {"language": language}).get("episodes", [])
        self.logger.log(f"Nombre d'épisodes dans la saison : {len(episodes)}")
        return self._to_dataframe(episodes, cols)

    # --- Endpoints / Routes Flask ---

    def get_tmdb_token_request(self)-> str:
        """Génère un request token OAuth v3."""
        data = self._get("/authentication/token/new")
        return (
            f"Request token : {data.get('request_token')}<br>"
            f"Expires at : {data.get('expires_at')}"
        )

    def route_search_tmdb_movies(
        self, input_file: str | Path, output_file: str | Path
    )-> bool:
        """Recherche une liste de films en V.O. depuis un CSV."""
        return self._process_csv(
            input_file, output_file, ["name", "year"],
            lambda row: self._search_row(self.search_tmdb_movie, row),
        )

    def route_search_tmdb_tv_shows(
        self, input_file: str | Path, output_file: str | Path
    )-> bool:
        """Recherche une liste de séries en Français depuis un CSV."""
        return self._process_csv(
            input_file, output_file, ["name", "year"],
            lambda row: self._search_row(self.search_tmdb_tv_show, row),
        )

    @staticmethod
    def _search_row(search: Callable[..., pd.DataFrame], row: Any)-> pd.DataFrame | None:
        name = getattr(row, "name", "")
        return search(name, getattr(row, "year", None)) if name else None

    def route_get_tmdb_movies_alternative_titles(
        self, input_file: str | Path, output_file: str | Path
    )-> bool:
        """Recherche une liste de titres alternatifs pour des films depuis un CSV."""
        return self._process_csv(input_file, output_file, ["tmdb_id", "year"], self._alternative_titles_of_row)

    def _alternative_titles_of_row(self, row: Any)-> pd.DataFrame | None:
        """Titres (original, principal et alternatifs du pays d'origine ou US) en alphabet latin."""
        tmdb_id = getattr(row, "tmdb_id", None)
        if not tmdb_id:
            return None

        df_detail = self.get_tmdb_movie_detail(tmdb_id)
        df_titles = pd.DataFrame(columns=["title"])
        countries: set[str] = set()
        year = None
        if not df_detail.empty:
            detail = df_detail.iloc[0]
            # Titre original puis titre principal, sans doublon (ordre stable, contrairement à un set)
            df_titles = pd.DataFrame(
                list(dict.fromkeys(detail[c] for c in ["original_title", "title"] if c in detail.index)),
                columns=["title"])
            countries = set(detail.get("origin_country", [])) | {"US"}
            year = pd.to_datetime(detail.get("release_date"), errors="coerce").year

        df_alt = self.get_tmdb_movie_alternative_titles(tmdb_id)
        if not df_alt.empty:
            df_titles = pd.concat([df_titles, df_alt[df_alt["iso_3166_1"].isin(countries)][["title"]]])

        df_res = (df_titles[df_titles["title"].apply(is_latin)]
                  .drop_duplicates().reset_index(drop=True).rename(columns={"title": "name"}))
        df_res.insert(0, "tmdb_id", tmdb_id)
        return df_res.assign(year=year)

    def route_search_tmdb_tv_episodes(
        self, input_file: str | Path, output_file: str | Path
    )-> bool:
        """Recherche les séries, leurs détails et l'ensemble de leurs épisodes en Français."""
        df_input = data_frame_from_csv(input_file, ";")
        if df_input.empty:
            return False
        df_input = df_input[[c for c in ["name", "year"] if c in df_input.columns]].drop_duplicates()

        # 1. Séries
        shows_list = [
            df.dropna(subset=["id"])
            for df in self._parallel(
                lambda r: self.search_tmdb_tv_show(r.name, getattr(r, "year", None), cols=["id", "name", "first_air_date"])
                if getattr(r, "name", "") else pd.DataFrame(),
                df_input.itertuples(),
            )
            if not df.empty and "id" in df.columns
        ]
        if not shows_list:
            return False
        shows = pd.concat(shows_list, ignore_index=True)

        # 2. Nombre de saisons de chaque série
        show_ids = list(shows["id"].dropna().unique())
        details_list = [
            df.assign(id=s_id)
            for s_id, df in zip(show_ids, self._parallel(
                lambda s_id: self.get_tmdb_tv_detail(int(s_id), cols=["number_of_seasons", "number_of_episodes"]),
                show_ids,
            ), strict=True)
            if not df.empty
        ]
        if not details_list:
            return False
        df_merged = shows.merge(pd.concat(details_list, ignore_index=True), on="id", how="left")

        # 3. Épisodes de chaque saison (saison 0 = épisodes spéciaux)
        df_merged["season_number"] = df_merged["number_of_seasons"].fillna(0).apply(lambda m: list(range(int(m) + 1)))
        df_exploded = df_merged.explode("season_number").rename(columns={"id": "show_id"})
        seasons_to_fetch = [
            (r.name, int(r.show_id), int(r.season_number))
            for r in df_exploded.itertuples()
            if pd.notna(r.show_id) and pd.notna(r.season_number)
        ]

        def fetch_season(item: tuple[str, int, int])-> pd.DataFrame:
            name_show, show_id, season_num = item
            self.logger.log(f"Détails de la saison {season_num} pour la série : {name_show} ({show_id})")
            df = self.get_tmdb_tv_season_detail(show_id, season_num)
            return df if df.empty else df.assign(show_id=show_id, season_number=season_num)

        seasons_list = [df for df in self._parallel(fetch_season, seasons_to_fetch) if not df.empty]
        if not seasons_list:
            return False

        seasons = pd.concat(seasons_list, ignore_index=True)
        final_df = df_exploded.merge(seasons, on=["show_id", "season_number"], suffixes=("_show", ""))
        data_frame_to_csv(final_df, output_file, ";")
        return True
