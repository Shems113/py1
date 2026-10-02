import threading
from collections.abc import Callable, Hashable
from concurrent.futures import Future
from typing import Any


class SingleFlightCache:
    """Cache mémoire thread-safe « à un seul vol ».

    Si plusieurs threads demandent la même clé en même temps (ex. les morceaux d'un
    même album dans un pool de workers), une seule exécution de fetch() a lieu :
    les autres threads attendent son résultat au lieu de relancer la requête.
    Une erreur n'est pas mise en cache (retentée au prochain appel) et est
    transmise à tous les threads qui attendaient cette clé.

        cache = SingleFlightCache()
        genre = cache.get(("genre", album_id), lambda: fetch_genre(album_id))
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._futures: dict[Hashable, Future] = {}

    def get(self, key: Hashable, fetch: Callable[[], Any]) -> Any:
        with self._lock:
            existing = self._futures.get(key)
            owner = existing is None
            future: Future = Future() if existing is None else existing
            if owner:
                self._futures[key] = future
        if owner:
            try:
                future.set_result(fetch())
            except BaseException as e:
                with self._lock:
                    self._futures.pop(key, None)
                future.set_exception(e)
        return future.result()

    def clear(self) -> None:
        with self._lock:
            self._futures.clear()
