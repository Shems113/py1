from pathlib import Path

import libtorrent as lt

from Common.Helpers.File import clean_directory_name
from Common.Helpers.Video import get_video_extensions
from Torrent.CBaseDownloader import CBaseDownloader, ProgressSnapshot


class CTorrentDownloader(CBaseDownloader):
    """Gestionnaire de téléchargement parallèle de liens magnet via libtorrent."""

    def __init__(
        self,
        magnet_data: dict[str, str | int],
        output_dir: Path | str
        )-> None:
        self.magnet_data = magnet_data
        self.base_save_path = Path(output_dir).resolve()
        self.base_save_path.mkdir(parents=True, exist_ok=True)
        self.video_extensions = get_video_extensions()

        self.session = lt.session({
            "listen_interfaces": "0.0.0.0:6881",
            "enable_dht": True,
            "active_downloads": -1})

    def _add_magnets(self)-> list[tuple[lt.torrent_handle, str]]:
        """Ajoute les magnets, chacun dans un sous-dossier nommé par son ID : [(handle, id)]."""
        torrents = []
        print(f"Ajout de {len(self.magnet_data)} magnet(s) à la session...")

        for idx, (magnet_link, movie_id) in enumerate(self.magnet_data.items(), start=1):
            if not magnet_link.startswith("magnet:?"):
                print(f"  -> #{idx} ignoré (ID: {movie_id}) : lien .torrent non géré ici, seulement les magnets")
                continue
            target_folder = self.base_save_path / clean_directory_name(str(movie_id))
            target_folder.mkdir(parents=True, exist_ok=True)

            params = lt.parse_magnet_uri(magnet_link)
            params.save_path = str(target_folder)
            torrents.append((self.session.add_torrent(params), str(movie_id)))
            print(f"  -> Magnet #{idx} ajouté (ID: {movie_id})")

        return torrents

    def _apply_video_filter(self, handle: lt.torrent_handle, tf: lt.torrent_info)-> None:
        """Active uniquement le téléchargement des fichiers vidéo."""
        files = tf.files()
        handle.prioritize_files([
            1 if Path(files.file_path(i)).suffix.lower() in self.video_extensions else 0
            for i in range(tf.num_files())
        ])

    def download_from_magnets(self)-> None:
        """Boucle principale de suivi des téléchargements."""
        torrents = self._add_magnets()
        filtered: set[int] = set()  # index des torrents dont le filtre vidéo est appliqué
        print("\n--- Téléchargement en cours ---\n")

        def poll()-> ProgressSnapshot:
            status_lines = []
            total_dl = total_ul = 0.0
            all_done = True

            for i, (h, movie_id) in enumerate(torrents, start=1):
                if not h.is_valid():
                    continue
                s = h.status()
                total_dl += s.download_rate
                total_ul += s.upload_rate
                # Avec le filtre vidéo, un torrent complet passe à l'état « finished » et
                # jamais à « seeding » : tester is_seeding seul faisait boucler à l'infini
                if not (s.is_finished or s.is_seeding):
                    all_done = False

                if s.has_metadata:
                    tf = h.torrent_file()
                    if tf and i not in filtered:
                        self._apply_video_filter(h, tf)
                        filtered.add(i)
                    name = tf.name() if tf else f"Magnet #{i}"
                    status_lines.append(
                        f"[{i}] [{movie_id}] {name[:25]:<25} | {s.progress * 100:6.2f}% "
                        f"| DL: {s.download_rate / 1024:7.1f} Ko/s | Peers: {s.num_peers:2d}")
                else:
                    status_lines.append(
                        f"[{i}] [{movie_id}] Magnet #{i:<18} | Métadonnées en attente... | Peers: {s.num_peers:2d}")

            return status_lines, total_dl, total_ul, all_done

        self._run_progress_loop(poll)
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
