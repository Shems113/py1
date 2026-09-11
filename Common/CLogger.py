import sys
import time
from loguru import logger

class CLogger:
    def __init__(self,logPaths):
        self.logPaths = logPaths
        # 1. On retire la configuration par défaut
        logFormat='<green>{time:HH:mm:ss.SSSS}</green> | <level>{level: <3}</level> | {message}'
        logger.remove()
        # 2. On ajoute un format simplifié (seulement l'heure, le niveau et le message)
        logger.add(sys.stderr,format=logFormat)
        for f in self.logPaths:
            open(f,'w').close()
            logger.add(f,rotation='500 MB',encoding='utf-8',format=logFormat)

    def log(self,*values):
        logger.info(*values)

    def logError(self,*values):
        logger.error(*values)

    def log_file_progress(self, index, total, file_path):
        """Log the progress of processed files with index/total and a short percent."""
        try:
            percent = (index / total) * 100 if total else 0
            self.log(f'Processed {index}/{total} ({percent:.1f}%) : {file_path}')
        except Exception:
            # Fallback to a simple message if anything goes wrong
            self.log(f'Processed {index}/{total} : {file_path}')

    def logElapsedTime(self,func,funcName):
        sep='-------------------------------------------------------------------'
        self.log(sep)
        self.log(f'Init : {funcName}')
        start_time = time.time()
        result=func()
        end_time = time.time()
        elapsed_time = end_time - start_time
        self.log(f'Time spent in {funcName} : {elapsed_time:.2f} seconds')
        self.log(sep)
        return result