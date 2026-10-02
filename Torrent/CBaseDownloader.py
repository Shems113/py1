import sys
import time
from collections.abc import Callable

# (lignes de suivi, débit descendant total en o/s, débit montant total en o/s, tout est terminé)
ProgressSnapshot = tuple[list[str], float, float, bool]


class CBaseDownloader:
    """Affichage console commun aux téléchargeurs (libtorrent local, Freebox)."""

    POLL_INTERVAL = 1.0  # secondes entre deux rafraîchissements

    @classmethod
    def _run_progress_loop(cls, poll: Callable[[], ProgressSnapshot])-> None:
        """Rafraîchit sur place un bloc « total + une ligne par tâche » jusqu'à ce que
        poll() indique que tout est terminé."""
        sys.stdout.write("\033[?25l")  # Masquer le curseur
        sys.stdout.flush()
        try:
            while True:
                status_lines, total_dl, total_ul, all_done = poll()
                sys.stdout.write(
                    f"\rTotal DL: {total_dl / 1048576:.2f} Mo/s | Total UL: {total_ul / 1048576:.2f} Mo/s\033[K\n")
                for line in status_lines:
                    sys.stdout.write(f"  {line}\033[K\n")
                sys.stdout.flush()

                if all_done:
                    break

                time.sleep(cls.POLL_INTERVAL)
                sys.stdout.write(f"\033[{len(status_lines) + 1}F")  # Remonter le curseur
        finally:
            sys.stdout.write("\033[?25h")  # Réafficher le curseur
            sys.stdout.flush()
