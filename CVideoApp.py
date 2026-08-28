import threading
import webbrowser
from pathlib import Path
from typing import Dict, Union

from flask import Flask, jsonify, redirect, render_template, request

from MediaService.CBitport import CBitport
from MediaService.COpenSubtitles import COpenSubtitles
from MediaService.CTmdb import CTmdb
from Torrent.CTorrentManager import CTorrentManager

class CVideoApp:
    """Application web Flask pour la gestion et le traitement de médias via CSV."""

    def __init__(self, input_dir: Union[str, Path] = r"D:\Result\Film"):
        # Instanciation des services
        self.bitport = CBitport()
        self.opensubtitles = COpenSubtitles()
        self.tmdb = CTmdb()
        self.torrent_manager = CTorrentManager()

        # Configuration des chemins d'accès
        self.input_directory = Path(input_dir)
        self.input_directory.mkdir(parents=True, exist_ok=True)

        self.csv_paths: Dict[str, Path] = {
            "films": self.input_directory / "MesFilms.csv",
            "films_torrents": self.input_directory / "MesFilmsTorrents.csv",
            "films_subtitles": self.input_directory / "MesFilmsSubtitles.csv",
            "tv_shows": self.input_directory / "MesSeriesTV.csv",
            "output": self.input_directory / "Output.csv",
        }

        # Initialisation de l'application Flask
        self.app = Flask(__name__, template_folder="Templates")

        # Enregistrement des routes Flask
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Déclaration explicite de toutes les routes de l'application Flask."""
        @self.app.route("/")
        def home():
            return render_template(
                "index.html", bitport_auth_url=self.bitport.bitport_auth_url
            )

        @self.app.route("/callback")
        def callback():
            code = request.args.get("code")
            if not code:
                return "Code d'autorisation manquant.", 400
            return self.bitport.callback_bitport_access_token(code)

        def make_action(route_func, csv_keys=()):
            return lambda: (
                route_func(*(self.csv_paths[key] for key in csv_keys)),
                redirect("/")
            )[1]

         # ----------------------------------------------------------------------

        @self.app.route("/getTmdbTokenRequest", endpoint="get_tmdb_token_request")
        def get_tmdb_token_request():
            return self.tmdb.get_tmdb_token_request()

        @self.app.route("/searchTmdbMovies", endpoint="route_search_tmdb_movies")
        def route_search_tmdb_movies():
            return make_action(self.tmdb.route_search_tmdb_movies, ("films", "output"))()

        @self.app.route("/getTmdbMoviesAlternativeTitles", endpoint="route_get_tmdb_movies_alternative_titles")
        def route_get_tmdb_movies_alternative_titles():
            return make_action(self.tmdb.route_get_tmdb_movies_alternative_titles, ("films", "output"))()

        @self.app.route("/searchTmdbTvShows", endpoint="route_search_tmdb_tv_shows")
        def route_search_tmdb_tv_shows():
            return make_action(self.tmdb.route_search_tmdb_tv_shows, ("tv_shows", "output"))()

        @self.app.route("/searchTmdbTvShowsEpisodes", endpoint="route_search_tmdb_tv_episodes")
        def route_search_tmdb_tv_episodes():
            return make_action(self.tmdb.route_search_tmdb_tv_episodes, ("tv_shows", "output"))()

        # ----------------------------------------------------------------------

        @self.app.route("/searchFilmsTorrents", endpoint="route_search_films_torrents")
        def route_search_films_torrents():
            return make_action(self.torrent_manager.route_search_films_torrents, ("films", "films_torrents"))()

        # ----------------------------------------------------------------------

        @self.app.route("/discoverBitportCloudFolders")
        def discover_bitport_cloud_folders():
            return jsonify(self.bitport.discover_bitport_cloud_folders())

        @self.app.route("/discoverBitportCloudVideos")
        def discover_bitport_cloud_videos():
            return jsonify(self.bitport.discover_bitport_cloud_videos())

        @self.app.route("/createFilmFoldersInBitport", endpoint="create_film_folders_in_bitport")
        def create_film_folders_in_bitport():
            return make_action(self.bitport.create_film_folders_in_bitport, ("films",))()

        @self.app.route("/transferTorrentsToBitport", endpoint="transfer_torrents_to_bitport")
        def transfer_torrents_to_bitport():
            return make_action(self.bitport.transfer_torrents_to_bitport, ("films_torrents", "output"))()

        # ----------------------------------------------------------------------

        @self.app.route("/getFilmsSubtitles", endpoint="route_get_films_subtitles")
        def route_get_films_subtitles():
            return make_action(self.opensubtitles.route_get_films_subtitles, ("films", "output"))()

        @self.app.route("/downloadFilmsSubtitles", endpoint="route_download_films_subtitles")
        def route_download_films_subtitles():
            return make_action(self.opensubtitles.route_download_films_subtitles, ("films_subtitles", "output"))()

    def run(self, host: str = "127.0.0.1", port: int = 5000, debug: bool = False) -> None:
        """Démarre le serveur web Flask et ouvre automatiquement le navigateur."""
        url = f"http://{host}:{port}"
        print(f"Lancement de l'application sur {url}")

        # Ouvre le navigateur dans un thread pour laisser à Flask le temps de s'initialiser
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

        self.app.run(host=host, port=port, debug=debug)

if __name__ == "__main__":
    video_app = CVideoApp()
    video_app.run(port=5000)