import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pandas as pd
import requests

from Common.CUtils import (
    data_frame_from_csv,
    data_frame_to_csv,
)

from Common.Helper import (
    check_required_env_vars, 
    handle_response,
    is_latin
)


class CTmdb:
    def __init__(self):
        check_required_env_vars("TMDB", ["TMDB_ACCESS_TOKEN"])
        tmbd_api_key='1fe4af3a73a742bcd684711b0612dcf0'
        tmbd_access_token='eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiIxZmU0YWYzYTczYTc0MmJjZDY4NDcxMWIwNjEyZGNmMCIsIm5iZiI6MTc1NjUzOTM5NC4yMTgsInN1YiI6IjY4YjJhYTAyYjNiYWZhMTY0MTIwMDM1NyIsInNjb3BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ.ImX3034tDo2klGFmv-Bv2Ek5BCqTGEgLuq-UqXSTulA'

        # Sécurisation : pas de clé API/JWT en dur dans le code source
        self.tmdb_access_token = os.getenv("TMDB_ACCESS_TOKEN", tmbd_access_token)
        self.tmdb_base_url = "https://api.themoviedb.org/3"
        self.tmdb_session = requests.Session()
        self.tmdb_session.headers.update(
            {
                "accept": "application/json",
                "Authorization": f"Bearer {self.tmdb_access_token}",
            }
        )

    def search_tmdb_movie(
        self, name: str, year: Optional[Union[int, str]] = None, language: Optional[str] = None
    ) -> pd.DataFrame:
        """Recherche un film sur TMDB (langue originale par défaut)."""
        print(f"Recherche du film '{name}' ({year or 'N/A'}) sur TMDB (VO)")
        url = f"{self.tmdb_base_url}/search/movie"
        params: Dict[str, Any] = {
            "query": name,
            "page": 1,
        }

        if language:
            params["language"] = language

        if year and str(year).isdigit():
            params["primary_release_year"] = int(year)

        data = handle_response(self.tmdb_session.get(url, params=params)) or {}
        results = data.get("results", [])
        print(f"Nombre de résultats : {data.get('total_results', 0)}")

        if not results:
            return pd.DataFrame()

        return pd.json_normalize(results)

    def get_tmdb_movie_detail(
        self, movie_id: int,  language: str = None
    ) -> pd.DataFrame:
        """Recherche des détails pour un film sur TMDB (langue originale par défaut)."""
        print(f"Recherche des détails pour le film '{movie_id}' sur TMDB (VO)")
        url = f"{self.tmdb_base_url}/movie/{movie_id}"
        params: Dict[str, Any] = {}

        if language:
            params["language"] = language

        data = handle_response(self.tmdb_session.get(url, params=params)) or {}
        results = data
        print(f"Nombre de résultats : {len(results)}")

        if not results:
            return pd.DataFrame()
       
        return pd.json_normalize(results)

    def get_tmdb_movie_alternative_titles(
        self, movie_id: str, country: Optional[str] = None
    ) -> pd.DataFrame:
        """Recherche des titres alternatifs pour un film sur TMDB (langue originale par défaut)."""
        print(f"Recherche des titres alternatifs pour le film '{movie_id}' sur TMDB (VO)")
        url = f"{self.tmdb_base_url}/movie/{movie_id}/alternative_titles"
        params: Dict[str, Any] = {}

        if country:
            params["country"] = country

        data = handle_response(self.tmdb_session.get(url, params=params)) or {}
        results = data.get("titles", [])
        print(f"Nombre de titres alternatifs : {len(results)}")

        if not results:
            return pd.DataFrame()
       
        return pd.json_normalize(results)

    def search_tmdb_tv_show(
        self,
        name: str,
        year: Optional[Union[int, str]] = None,
        cols: Optional[List[str]] = None,
        language: str = "fr-FR",
    ) -> pd.DataFrame:
        """Recherche une série TV sur TMDB (en Français par défaut)."""
        print(f"Recherche de la série '{name}' ({year or 'N/A'}) sur TMDB (FR)")
        url = f"{self.tmdb_base_url}/search/tv"
        params: Dict[str, Any] = {
            "query": name,
            "page": 1,
            "language": language,
        }

        if year and str(year).isdigit():
            params["first_air_date_year"] = int(year)

        data = handle_response(self.tmdb_session.get(url, params=params)) or {}
        results = data.get("results", [])
        total = data.get("total_results", 0)
        print(f"Nombre de résultats : {total}")

        if not results:
            return (
                pd.DataFrame([{c: None for c in cols}])
                if cols
                else pd.DataFrame()
            )

        df = pd.json_normalize(results)
        if cols:
            existing_cols = [c for c in cols if c in df.columns]
            df = df[existing_cols] if existing_cols else df

        return df

    def get_tmdb_tv_detail(
        self, show_id: int, cols: Optional[List[str]] = None, language: str = "fr-FR"
    ) -> pd.DataFrame:
        url = f"{self.tmdb_base_url}/tv/{show_id}"
        data = handle_response(
            self.tmdb_session.get(url, params={"language": language})
        ) or {}

        if not data:
            return (
                pd.DataFrame([{c: None for c in cols}])
                if cols
                else pd.DataFrame()
            )

        episodes_cnt = data.get("number_of_episodes", 0)
        print(f"Nombre de saisons : {data.get('number_of_seasons', 0)}")
        print(f"Nombre total d'épisodes : {episodes_cnt}")

        df = pd.json_normalize(data)
        if cols:
            existing_cols = [c for c in cols if c in df.columns]
            df = df[existing_cols] if existing_cols else df

        return df

    def get_tmdb_tv_season_detail(
        self, show_id: int, season_number: int, cols: Optional[List[str]] = None, language: str = "fr-FR"
    ) -> pd.DataFrame:
        url = f"{self.tmdb_base_url}/tv/{show_id}/season/{season_number}"
        data = handle_response(
            self.tmdb_session.get(url, params={"language": language})
        ) or {}

        episodes = data.get("episodes", [])
        print(f"Nombre d'épisodes dans la saison : {len(episodes)}")

        if not episodes:
            return (
                pd.DataFrame([{c: None for c in cols}])
                if cols
                else pd.DataFrame()
            )

        df = pd.json_normalize(episodes)
        if cols:
            existing_cols = [c for c in cols if c in df.columns]
            df = df[existing_cols] if existing_cols else df

        return df

    # --- Endpoints / Routes Flask ---

    def get_tmdb_token_request(self) -> str:
        """Génère un request token OAuth v3."""
        url = f"{self.tmdb_base_url}/authentication/token/new"
        data = handle_response(self.tmdb_session.get(url)) or {}
        return (
            f"Request token : {data.get('request_token')}<br>"
            f"Expires at : {data.get('expires_at')}"
        )

    def route_search_tmdb_movies(
        self, inputFile: Union[str, Path], outputFile: Union[str, Path]
    ) -> bool:
        """Recherche une liste de films en V.O. depuis un CSV."""
        df_input = data_frame_from_csv(inputFile, ";")
        if df_input.empty:
            return False

        cols_to_keep = [c for c in ["name", "year"] if c in df_input.columns]
        df_input = df_input[cols_to_keep].drop_duplicates()

        results: List[pd.DataFrame] = []
        for row in df_input.itertuples():
            name = getattr(row, "name", getattr(row, "title", ""))
            year = getattr(row, "year", None)

            if not name:
                continue

            df_res = self.search_tmdb_movie(name, year)
            if not df_res.empty:
                results.append(df_res)
            
            time.sleep(0.05)  # Légère pause pour respecter la limite de taux de l'API

        if results:
            final_df = pd.concat(results, ignore_index=True)
            data_frame_to_csv(final_df, outputFile, ";")
            return True

        return False
    
    def route_get_tmdb_movies_alternative_titles(
        self, inputFile: Union[str, Path], outputFile: Union[str, Path]
    ) -> bool:
        """Recherche une liste de titres alternatifs pour des films depuis un CSV."""
        df_input = data_frame_from_csv(inputFile, ";")
        if df_input.empty:
            return False

        cols_to_keep = [c for c in ["tmdb_id", "year"] if c in df_input.columns]
        df_input = df_input[cols_to_keep].drop_duplicates()

        results: List[pd.DataFrame] = []
        for row in df_input.itertuples():
            tmdb_id = getattr(row, "tmdb_id", None)

            if not tmdb_id:
                continue

            df_res = self.get_tmdb_movie_detail(tmdb_id)
            df_res_titles=pd.DataFrame()
            countries=set()
            year=None

            if not df_res.empty:
                titles=set([df_res.iat[0, df_res.columns.get_loc(col)] for col in ["original_title", "title"] if col in df_res.columns])
                df_res_titles=pd.DataFrame(titles, columns=["title"])
                countries=set(df_res.iat[0, df_res.columns.get_loc("origin_country")] + ["US"])
                year=pd.to_datetime(df_res.iat[0, df_res.columns.get_loc("release_date")]).year
             
            df_res = self.get_tmdb_movie_alternative_titles(tmdb_id)
            if not df_res.empty:
                df_res=df_res[df_res["iso_3166_1"].isin(countries)]
                df_res=pd.concat([df_res_titles,df_res[["title"]]])
            else:
                df_res=df_res_titles
                 
            df_res=df_res[df_res["title"].apply(is_latin)]
            df_res=df_res.drop_duplicates().reset_index(drop=True).rename(columns={"title": "name"})
            df_res.insert(0, "tmdb_id", tmdb_id)
            df_res=df_res.assign(year=year)
            results.append(df_res)

            time.sleep(0.05)  # Légère pause pour respecter la limite de taux de l'API

        if results:
            final_df = pd.concat(results, ignore_index=True)
            data_frame_to_csv(final_df, outputFile, ";")
            return True

        return False

    def route_search_tmdb_tv_shows(
        self, inputFile: Union[str, Path], outputFile: Union[str, Path]
    ) -> bool:
        """Recherche une liste de séries en Français depuis un CSV."""
        df_input = data_frame_from_csv(inputFile, ";")
        if df_input.empty:
            return False

        cols_to_keep = [c for c in ["name", "year"] if c in df_input.columns]
        df_input = df_input[cols_to_keep].drop_duplicates()

        results: List[pd.DataFrame] = []
        for row in df_input.itertuples():
            name = getattr(row, "name", getattr(row, "title", ""))
            year = getattr(row, "year", None)

            if not name:
                continue

            df_res = self.search_tmdb_tv_show(name, year)
            if not df_res.empty:
                results.append(df_res)

            time.sleep(0.05)

        if results:
            final_df = pd.concat(results, ignore_index=True)
            data_frame_to_csv(final_df, outputFile, ";")
            return True

        return False

    def route_search_tmdb_tv_episodes(
        self, inputFile: Union[str, Path], outputFile: Union[str, Path]
    ) -> bool:
        """Recherche les séries, leurs détails et l'ensemble de leurs épisodes en Français."""
        df_input = data_frame_from_csv(inputFile, ";")
        if df_input.empty:
            return False

        cols_to_keep = [c for c in ["name", "year"] if c in df_input.columns]
        df_input = df_input[cols_to_keep].drop_duplicates()

        shows_list: List[pd.DataFrame] = []
        for r in df_input.itertuples():
            name = getattr(r, "name", "")
            year = getattr(r, "year", None)
            if not name:
                continue

            df_show = self.search_tmdb_tv_show(
                name, year, cols=["id", "name", "first_air_date"]
            )
            if not df_show.empty and "id" in df_show.columns:
                df_show = df_show.dropna(subset=["id"])
                shows_list.append(df_show)

        if not shows_list:
            return False

        shows = pd.concat(shows_list, ignore_index=True)

        details_list: List[pd.DataFrame] = []
        for s_id in shows["id"].dropna().unique():
            """Récupère les détails complets d'une série en Français."""
            print(f"Récupération des détails pour la série ID : {s_id}")
            df_det = self.get_tmdb_tv_detail(
                int(s_id), cols=["number_of_seasons", "number_of_episodes"]
            )
            if not df_det.empty:
                details_list.append(df_det.assign(id=s_id))
            time.sleep(0.05)

        if not details_list:
            return False

        details = pd.concat(details_list, ignore_index=True)
        df_merged = shows.merge(details, on="id", how="left")

        df_merged["season_number"] = df_merged["number_of_seasons"].fillna(0).apply(
            lambda m: list(range(int(m) + 1))
        )
        df_exploded = df_merged.explode("season_number").rename(columns={"id": "show_id"})

        seasons_list: List[pd.DataFrame] = []
        for r in df_exploded.itertuples():
            name_show = getattr(r, "name", None)
            show_id = getattr(r, "show_id", None)
            season_num = getattr(r, "season_number", None)

            if pd.isna(show_id) or pd.isna(season_num):
                continue

            """Récupère la liste des épisodes pour une saison donnée en Français."""
            print(f"Détails de la saison {season_num} pour la série : {name_show} ({show_id})")
            df_seas = self.get_tmdb_tv_season_detail(int(show_id), int(season_num))
            
            if not df_seas.empty:
                seasons_list.append(
                    df_seas.assign(show_id=show_id, season_number=season_num)
                )
            time.sleep(0.05)

        if seasons_list:
            seasons = pd.concat(seasons_list, ignore_index=True)
            final_df = df_exploded.merge(
                seasons, on=["show_id", "season_number"], suffixes=("_show", "")
            )
            data_frame_to_csv(final_df, outputFile, ";")
            return True

        return False