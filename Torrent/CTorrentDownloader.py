import sys
import time
from pathlib import Path

import libtorrent as lt

from Common.Helpers.File import clean_directory_name
from Common.Helpers.Video import get_video_extensions


class CTorrentDownloader:
    """Gestionnaire de téléchargement parallèle de liens magnet via libtorrent."""

    def __init__(
        self,
        magnet_data: dict[str, str | int],
        output_dir: Path | str) -> None:
        self.magnet_data = magnet_data
        self.base_save_path = Path(output_dir).resolve()
        self.base_save_path.mkdir(parents=True, exist_ok=True)

        self.session = lt.session({
            "listen_interfaces": "0.0.0.0:6881",
            "enable_dht": True,
            "active_downloads": -1,
        })

    def _add_magnets(self) -> list[lt.torrent_handle]:
        """Ajoute les magnets et configure les dossiers cibles par ID."""
        handles = []
        video_exts = get_video_extensions()
        print(f"Ajout de {len(self.magnet_data)} magnet(s) à la session...")

        for idx, (magnet_link, movie_id) in enumerate(self.magnet_data.items(), start=1):
            target_folder = self.base_save_path / clean_directory_name(str(movie_id))
            target_folder.mkdir(parents=True, exist_ok=True)

            params = lt.parse_magnet_uri(magnet_link)
            params.save_path = str(target_folder)

            handle = self.session.add_torrent(params)
            handle.movie_id = str(movie_id)
            handle.filtered = False
            handle.video_extensions = video_exts

            handles.append(handle)
            print(f"  -> Magnet #{idx} ajouté (ID: {movie_id})")

        return handles

    def _apply_video_filter(self, handle: lt.torrent_handle, tf: lt.torrent_info) -> None:
        """Active uniquement le téléchargement des fichiers vidéo."""
        priorities = [
            1 if Path(tf.files().file_path(i)).suffix.lower() in handle.video_extensions else 0
            for i in range(tf.num_files())
        ]
        handle.prioritize_files(priorities)
        handle.filtered = True

    def download_from_magnets(self) -> None:
        """Boucle principale de suivi des téléchargements."""
        handles = self._add_magnets()
        print("\n--- Téléchargement en cours ---\n")

        sys.stdout.write("\033[?25l")  # Masquer le curseur
        sys.stdout.flush()

        try:
            while True:
                all_done = True
                status_lines = []
                total_dl, total_ul = 0.0, 0.0

                for i, h in enumerate(handles, start=1):
                    if not h.is_valid():
                        continue

                    s = h.status()
                    total_dl += s.download_rate
                    total_ul += s.upload_rate

                    if not s.is_seeding:
                        all_done = False

                    if s.has_metadata:
                        tf = h.torrent_file()
                        if not getattr(h, "filtered", False) and tf:
                            self._apply_video_filter(h, tf)

                        name = tf.name() if tf else f"Magnet #{i}"
                        progress = s.progress * 100
                        dl_speed = s.download_rate / 1024
                        status_str = f"[{i}] [{h.movie_id}] {name[:25]:<25} | {progress:6.2f}% | DL: {dl_speed:7.1f} Ko/s | Peers: {s.num_peers:2d}"
                    else:
                        status_str = f"[{i}] [{h.movie_id}] Magnet #{i:<18} | Métadonnées en attente... | Peers: {s.num_peers:2d}"

                    status_lines.append(status_str)

                # Affichage console synthétique
                sys.stdout.write(f"\rTotal DL: {total_dl / 1048576:.2f} Mo/s | Total UL: {total_ul / 1048576:.2f} Mo/s\033[K\n")
                for line in status_lines:
                    sys.stdout.write(f"  {line}\033[K\n")
                sys.stdout.flush()

                if all_done:
                    break

                time.sleep(1)
                sys.stdout.write(f"\033[{len(status_lines) + 1}F")  # Remonter le curseur

        finally:
            sys.stdout.write("\033[?25h")  # Réafficher le curseur
            sys.stdout.flush()

        print(f"\nTéléchargements terminés dans : {self.base_save_path}")


if __name__ == "__main__":
    url='magnet:?xt=urn:btih:12eb9d0f0cac75c9ff2c2477cef1ede0885bf89e&dn=%5BTsundere-Raws%5D%20Saint%20Seiya%20-%20Knights%20of%20the%20Zodiac%20%281986%29%20-%20Int%C3%A9gral%20VOSTFR%20%5BWEB%201080p%20x264%20AAC%5D&tr=http%3A%2F%2Fnyaa.tracker.wf%3A7777%2Fannounce&tr=udp%3A%2F%2Fopen.stealth.si%3A80%2Fannounce&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce&tr=udp%3A%2F%2Fexodus.desync.com%3A6969%2Fannounce&tr=udp%3A%2F%2Ftracker.torrent.eu.org%3A451%2Fannounce'
    MAGNET_DATA = {
        url: 'SAINT SEIYA',
        }

    OUTPUT_FOLDER = r"C:\Users\SEDOUD\Videos\New\Torrent"

    downloader = CTorrentDownloader(magnet_data=MAGNET_DATA, output_dir=OUTPUT_FOLDER)
    try:
        downloader.download_from_magnets()
    except KeyboardInterrupt:
        print("\nTéléchargements interrompus.")
