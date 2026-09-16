import sys
import time
from typing import Callable, Any, List
from loguru import logger


class CLogger:
    def __init__(self, log_paths: List[str]):
        self.log_paths = log_paths
        
        # Format épuré : Heure | Niveau | Message
        log_format = "<green>{time:HH:mm:ss.SSSS}</green> | <level>{level: <3}</level> | {message}"
        
        # 1. Suppression du handler par défaut
        logger.remove()
        
        # 2. Handler console (sys.stderr) avec flush immédiat pour PowerShell
        logger.add(sys.stderr, format=log_format, colorize=True)
        
        # 3. Handlers fichiers (réinitialisation et écriture asynchrone)
        for f in self.log_paths:
            open(f, "w", encoding="utf-8").close()
            logger.add(
                f,
                rotation="500 MB",
                encoding="utf-8",
                format=log_format,
                enqueue=True,  # Évite les conflits d'accès concurrents aux fichiers
            )

    def log(self, *values: Any) -> None:
        """Log un message avec le niveau INFO (concatène les arguments par un espace)."""
        message = " ".join(str(v) for v in values)
        logger.info(message)

    def logError(self, *values: Any) -> None:
        """Log un message avec le niveau ERROR."""
        message = " ".join(str(v) for v in values)
        logger.error(message)

    def log_file_progress(self, index: int, total: int, file_path: str) -> None:
        """Affiche la progression du traitement des fichiers avec pourcentage."""
        try:
            percent = (index / total) * 100 if total else 0
            self.log(f"Processed {index}/{total} ({percent:.1f}%) : {file_path}")
        except Exception:
            self.log(f"Processed {index}/{total} : {file_path}")

    def logElapsedTime(self, func: Callable[[], Any], func_name: str) -> Any:
        """Mesure et log le temps d'exécution d'une fonction."""
        sep = "-------------------------------------------------------------------"
        self.log(sep)
        self.log(f"Init : {func_name}")
        
        start_time = time.time()
        result = func()
        elapsed_time = time.time() - start_time
        
        self.log(f"Time spent in {func_name} : {elapsed_time:.2f} seconds")
        self.log(sep)
        return result