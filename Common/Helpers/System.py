import os
import platform
import sys

CURRENT_MACHINE = platform.node()
MY_GPU_MACHINE = "TX16PRO"
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform == "linux"
IS_MY_GPU_MACHINE = IS_WINDOWS and CURRENT_MACHINE.upper() == MY_GPU_MACHINE


def _cpu_count()-> int:
    """Nombre de workers : variable PY1_CPU_COUNT si définie, sinon 7 (valeur historique).
    Permet d'ajuster par machine (ex. serveur vast.ai) sans modifier le code."""
    try:
        value = int(os.getenv("PY1_CPU_COUNT", "0"))
    except ValueError:
        value = 0
    return value if value > 0 else 7

CPU_COUNT = _cpu_count()

def check_required_env_vars(
    service_name: str,
    var_names: list[str]
    )-> list[str]:
    """Vérifie que les variables d'environnement requises sont bien définies.

    Affiche un avertissement si certaines sont manquantes et retourne la liste
    des variables non renseignées.
    """
    missing = [name for name in var_names if not os.getenv(name)]
    if missing:
        print(
            f"⚠️  [{service_name}] Variable(s) d'environnement manquante(s) : {', '.join(missing)}."
        )
    return missing

def get_mirrors_from_env(
    env_var: str,
    defaults: list[str]
    )-> list[str]:
    """Récupère une liste de miroirs depuis une variable d'environnement (séparés par des virgules).
    Si la variable est absente ou vide, retourne la liste de miroirs par défaut.
    """
    override = os.getenv(env_var)
    if override:
        mirrors = [m.strip() for m in override.split(",") if m.strip()]
        if mirrors:
            return mirrors
    return list(defaults)  # copie : l'appelant peut la modifier sans altérer la constante
