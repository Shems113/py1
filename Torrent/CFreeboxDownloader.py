import base64
import sys
import time
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any

from Common.Helpers.File import clean_directory_name
from Common.Helpers.Video import get_video_extensions
from MediaService.CFreebox import call, connect, http

CHUNK_SIZE = 1024 * 1024  # 1 Mo

# États considérés comme terminés (l'erreur aussi, pour ne pas boucler indéfiniment)
FINISHED_STATUSES = {"done", "seeding", "error"}


def _b64(path: str) -> str:
    """L'API Freebox attend les chemins encodés en base64."""
    return base64.b64encode(path.encode("utf-8")).decode("ascii")


class CFreeboxDownloader:
    """Téléchargement parallèle de liens magnet par la Freebox (API Freebox OS)."""

    def __init__(
        self,
        magnet_data: dict[str, str | int] | None = None,
        output_dir: str = "/Freebox/Téléchargements") -> None:
        self.magnet_data = magnet_data or {}
        self.base_save_path = output_dir.rstrip("/")
        self.video_extensions = get_video_extensions()
        self.base_url, self.session_token = connect()

    def _api(self, method: str, path: str, **kwargs: Any) -> Any:
        return call(method, f"{self.base_url}{path}", self.session_token, **kwargs)

    def _fetch_downloads(self) -> dict[int, dict[str, Any]]:
        """Toutes les tâches du gestionnaire de téléchargements, indexées par id."""
        return {d["id"]: d for d in self._api("GET", "/downloads/") or []}

    def _select_downloads(self, include_finished: bool) -> list[dict[str, Any]]:
        """Tâches brutes de la box, par défaut uniquement celles en cours."""
        return [
            t for t in self._fetch_downloads().values()
            if include_finished or t["status"] not in FINISHED_STATUSES
        ]

    @staticmethod
    def _folder(task: dict[str, Any]) -> str:
        """Dossier de destination d'une tâche sur la box (décodé du base64)."""
        return base64.b64decode(task.get("download_dir", "")).decode("utf-8")

    def get_downloads(self, include_finished: bool = False) -> list[dict[str, Any]]:
        """État des téléchargements de la Freebox (par défaut, uniquement ceux en cours)."""
        return [
            {
                "id": t["id"],
                "name": t["name"],
                "status": t["status"],
                "progress_pct": t["rx_pct"] / 100,  # rx_pct est multiplié par 100
                "size_bytes": t["size"],
                "dl_rate_bps": t["rx_rate"],
                "ul_rate_bps": t.get("tx_rate", 0),
                "eta_s": t["eta"],
                "folder": self._folder(t),
                "error": t.get("error"),
            }
            for t in self._select_downloads(include_finished)
        ]

    def print_downloads(self, include_finished: bool = False) -> None:
        """Affiche en console les téléchargements de la Freebox (par défaut, ceux en cours)."""
        downloads = self._select_downloads(include_finished)
        if not downloads:
            print("Aucun téléchargement en cours sur la Freebox.")
            return

        total_dl = sum(t.get("rx_rate", 0) for t in downloads)
        total_ul = sum(t.get("tx_rate", 0) for t in downloads)
        print(f"Total DL: {total_dl / 1048576:.2f} Mo/s | Total UL: {total_ul / 1048576:.2f} Mo/s")
        for i, t in enumerate(downloads, start=1):
            print(f"  #{t['id']:<4} {self._format_status(i, PurePosixPath(self._folder(t)).name, t)}")

    # ------------------------------------------------------------------ #
    # Rapatriement des fichiers de la Freebox vers la machine locale
    # ------------------------------------------------------------------ #
    def _list_files(self, folder: str, relative: str = "") -> list[tuple[str, dict[str, Any]]]:
        """Fichiers d'un dossier de la box (sous-dossiers compris) : [(chemin relatif, infos)]."""
        files = []
        for entry in self._api("GET", f"/fs/ls/{_b64(folder)}", params={"removeHidden": 1}) or []:
            if entry["name"] in (".", ".."):
                continue
            rel = f"{relative}/{entry['name']}" if relative else entry["name"]
            if entry["type"] == "dir":
                files += self._list_files(f"{folder}/{entry['name']}", rel)
            else:
                files.append((rel, entry))
        return files

    def _download_file(self, remote_path_b64: str, size: int, dest: Path) -> None:
        """Télécharge un fichier de la box, avec reprise si un fichier partiel existe déjà."""
        done = dest.stat().st_size if dest.exists() else 0
        if done >= size:
            print(f"  = {dest.name} (déjà présent)")
            return

        dest.parent.mkdir(parents=True, exist_ok=True)
        url = f"{self.base_url}/dl/{remote_path_b64}"
        headers = {"X-Fbx-App-Auth": self.session_token, "Range": f"bytes={done}-"}
        with http(url).get(url, headers=headers, stream=True, timeout=30) as response:
            response.raise_for_status()
            if response.status_code != 206:  # reprise non supportée : on repart de zéro
                done = 0
            start = time.monotonic()
            received = 0
            with open(dest, "ab" if done else "wb") as f:
                for chunk in response.iter_content(CHUNK_SIZE):
                    f.write(chunk)
                    received += len(chunk)
                    speed = received / max(time.monotonic() - start, 1e-6) / 1048576
                    pct = (done + received) / size * 100 if size else 100
                    sys.stdout.write(f"\r  ↓ {dest.name[:40]:<40} | {pct:6.2f}% | {speed:6.2f} Mo/s\033[K")
                    sys.stdout.flush()
        print()

    def fetch_task(self, task_id: int, local_dir: Path | str, video_only: bool = True) -> Path:
        """Rapatrie sur cette machine les fichiers d'un téléchargement de la box.

        Recrée le sous-dossier de la tâche (ex. « SAINT SEIYA ») dans local_dir.
        Débit limité par l'upload de la connexion de la Freebox.
        """
        task = self._fetch_downloads()[task_id]
        folder = self._folder(task).rstrip("/")
        target = Path(local_dir) / PurePosixPath(folder).name
        files = self._list_files(folder)
        if video_only:
            files = [(rel, e) for rel, e in files if PurePosixPath(rel).suffix.lower() in self.video_extensions]

        print(f"Rapatriement de {len(files)} fichier(s) de « {task['name']} » vers {target}")
        for rel, entry in files:
            self._download_file(entry["path"], entry["size"], target / rel)
        return target

    def _add_magnets(self) -> dict[int, str]:
        """Ajoute les magnets, chacun dans un sous-dossier nommé par son ID. Renvoie {id tâche: id film}."""
        tasks = {}
        print(f"Ajout de {len(self.magnet_data)} magnet(s) à la Freebox...")

        for idx, (magnet_link, movie_id) in enumerate(self.magnet_data.items(), start=1):
            folder = clean_directory_name(str(movie_id))
            with suppress(Exception):  # le dossier existe déjà
                self._api("POST", "/fs/mkdir/", json={"parent": _b64(self.base_save_path), "dirname": folder})

            task_id = self._api("POST", "/downloads/add", data={
                "download_url": magnet_link,
                "download_dir": _b64(f"{self.base_save_path}/{folder}"),
            })["id"]
            tasks[task_id] = str(movie_id)
            print(f"  -> Magnet #{idx} ajouté (ID: {movie_id})")

        return tasks

    def _apply_video_filter(self, task_id: int) -> None:
        """Active uniquement le téléchargement des fichiers vidéo."""
        for file in self._api("GET", f"/downloads/{task_id}/files") or []:
            if PurePosixPath(file["name"]).suffix.lower() not in self.video_extensions:
                self._api("PUT", f"/downloads/{task_id}/files/{file['id']}", json={"priority": "no_dl"})

    @staticmethod
    def _format_status(i: int, movie_id: str, task: dict[str, Any]) -> str:
        """Ligne de suivi d'une tâche."""
        prefix = f"[{i}] [{movie_id}]"
        if not task:
            return f"{prefix} Tâche supprimée de la Freebox"
        if task["status"] == "error":
            return f"{prefix} {task['name'][:25]:<25} | Erreur : {task.get('error')}"
        if not task["size"]:
            return f"{prefix} Magnet #{i:<18} | Métadonnées en attente..."

        progress = task["rx_pct"] / 100  # rx_pct est multiplié par 100 (123 = 1,23 %)
        dl_speed = task["rx_rate"] / 1024
        eta = task["eta"]
        return (f"{prefix} {task['name'][:25]:<25} | {progress:6.2f}% | DL: {dl_speed:7.1f} Ko/s "
                f"| ETA: {eta // 3600}h{eta % 3600 // 60:02d}")

    def download_from_magnets(self, local_dir: Path | str | None = None) -> None:
        """Boucle principale de suivi des téléchargements.

        Si local_dir est fourni, les fichiers terminés sont ensuite rapatriés sur cette machine.
        """
        tasks = self._add_magnets()
        downloads: dict[int, dict[str, Any]] = {}
        filtered: set[int] = set()
        print("\n--- Téléchargement en cours ---\n")

        sys.stdout.write("\033[?25l")  # Masquer le curseur
        sys.stdout.flush()

        try:
            while True:
                downloads = self._fetch_downloads()
                all_done = True
                status_lines = []
                total_dl, total_ul = 0, 0

                for i, (task_id, movie_id) in enumerate(tasks.items(), start=1):
                    task = downloads.get(task_id, {})
                    total_dl += task.get("rx_rate", 0)
                    total_ul += task.get("tx_rate", 0)

                    if task and task["status"] not in FINISHED_STATUSES:
                        all_done = False

                    # Métadonnées reçues (taille connue) : on filtre une seule fois
                    if task.get("size") and task_id not in filtered:
                        self._apply_video_filter(task_id)
                        filtered.add(task_id)

                    status_lines.append(self._format_status(i, movie_id, task))

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

        if local_dir:
            for task_id in tasks:
                if downloads.get(task_id, {}).get("status") in {"done", "seeding"}:
                    self.fetch_task(task_id, local_dir)


if __name__ == "__main__":
    url = "magnet:?xt=urn:btih:12eb9d0f0cac75c9ff2c2477cef1ede0885bf89e&dn=%5BTsundere-Raws%5D%20Saint%20Seiya%20-%20Knights%20of%20the%20Zodiac%20%281986%29%20-%20Int%C3%A9gral%20VOSTFR%20%5BWEB%201080p%20x264%20AAC%5D&tr=http%3A%2F%2Fnyaa.tracker.wf%3A7777%2Fannounce&tr=udp%3A%2F%2Fopen.stealth.si%3A80%2Fannounce&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce&tr=udp%3A%2F%2Fexodus.desync.com%3A6969%2Fannounce&tr=udp%3A%2F%2Ftracker.torrent.eu.org%3A451%2Fannounce"
    MAGNET_DATA = {
        url: "SAINT SEIYA",
    }

    OUTPUT_FOLDER = "/Freebox/Téléchargements/Torrent"

    try:
        # CFreeboxDownloader(magnet_data=MAGNET_DATA, output_dir=OUTPUT_FOLDER).download_from_magnets()
        CFreeboxDownloader().print_downloads()
    except KeyboardInterrupt:
        print("\nSuivi interrompu : les téléchargements continuent sur la Freebox.")