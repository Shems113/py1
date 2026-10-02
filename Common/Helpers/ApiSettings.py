"""Paramètres des API externes (URL, clés, jetons, certificats), centralisés dans
Files/ExternalApiSettings.json : une section par service, par ordre alphabétique.

Ce fichier contient des secrets : il est dans .gitignore et ne doit jamais être commité.
Files/ExternalApiSettings.example.json en donne la structure, sans les valeurs sensibles.

    tmdb = get_api_settings("Tmdb")
    tmdb["BaseUrl"], tmdb["AccessToken"]
"""
import copy
import threading
from typing import Any

from Common.Helpers.File import PROJECT_ROOT
from Common.Helpers.Json import json_from_file, json_to_file

EXTERNAL_API_SETTINGS_FILE = PROJECT_ROOT / "Files" / "ExternalApiSettings.json"

_lock = threading.RLock()
_settings: dict[str, Any] | None = None


def _load()-> dict[str, Any]:
    global _settings
    if _settings is None:
        if not EXTERNAL_API_SETTINGS_FILE.is_file():
            raise FileNotFoundError(
                f"Paramètres des API introuvables : {EXTERNAL_API_SETTINGS_FILE} "
                "(modèle : Files/ExternalApiSettings.example.json)"
            )
        _settings = json_from_file(EXTERNAL_API_SETTINGS_FILE)
    return _settings

def get_api_settings(section: str)-> dict[str, Any]:
    """Copie de la section d'un service (ex. « Tmdb »). Le fichier est lu une seule fois."""
    with _lock:
        settings = _load()
        if section not in settings:
            raise KeyError(f"Section « {section} » absente de {EXTERNAL_API_SETTINGS_FILE.name}")
        return copy.deepcopy(settings[section])

def update_api_settings(section: str, values: dict[str, Any])-> None:
    """Met à jour des clés d'une section (ex. nouveau jeton) et réécrit le fichier,
    de façon atomique, en conservant toutes les autres sections."""
    with _lock:
        settings = _load()
        settings.setdefault(section, {}).update(copy.deepcopy(values))
        json_to_file(EXTERNAL_API_SETTINGS_FILE, settings)
