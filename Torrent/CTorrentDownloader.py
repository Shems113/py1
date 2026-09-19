import sys
import time
from pathlib import Path
from typing import Dict, List, Union
import libtorrent as lt
from Common.CUtils import get_video_extensions,clean_directory_name


class CTorrentDownloader:
    """Gestionnaire de téléchargement parallèle de liens magnet via libtorrent."""

    def __init__(self, magnet_data: Dict[str, Union[str, int]], output_dir: Union[Path, str]) -> None:
        self.magnet_data = magnet_data
        self.base_save_path = Path(output_dir).resolve()
        self.base_save_path.mkdir(parents=True, exist_ok=True)

        self.session = lt.session({
            "listen_interfaces": "0.0.0.0:6881",
            "enable_dht": True,
            "active_downloads": -1,
        })

    def _add_magnets(self) -> List[lt.torrent_handle]:
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
    MAGNET_DATA = {
        'magnet:?xt=urn:btih:8B5A846FF78640D324AA0A01F3E180854468E083&dn=Blade.Runner.2049.2017.WEB-DL.1080p.6ch.x265.10bit.%5bS-Less%5d': '335984',
        'magnet:?xt=urn:btih:ADE12F74D9B74DEA26AD7C88B2C42FE02399CB59&dn=Dune%202021%201080p%2010bit%20BluRay%208CH%20x265%20HEVC-PSA': '438631',
        'magnet:?xt=urn:btih:d36586c7ac43f22b1dd5566b79dc7b8bc04f5c21&dn=Arrival (2016) x 1608 (2160p) HDR 5.1 x265 10bit Phun Psyz.mkv': '329865'
    }

    OUTPUT_FOLDER = r"C:\Users\SEDOUD\Videos\New\Torrent"

    downloader = CTorrentDownloader(magnet_data=MAGNET_DATA, output_dir=OUTPUT_FOLDER)
    try:
        downloader.download_from_magnets()
    except KeyboardInterrupt:
        print("\nTéléchargements interrompus.")