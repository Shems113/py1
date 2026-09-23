import os

import regex as reg


def check_required_env_vars(
    service_name: str,
    var_names: list[str]
    ) -> list[str]:
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
    ) -> list[str]:
    """Récupère une liste de miroirs depuis une variable d'environnement (séparés par des virgules).
    Si la variable est absente ou vide, retourne la liste de miroirs par défaut.
    """
    override = os.getenv(env_var)
    if override:
        mirrors = [m.strip() for m in override.split(",") if m.strip()]
        if mirrors:
            return mirrors
    return defaults

def is_latin(texte: str) -> bool:
    # Autorise les caractères de script Latin, les chiffres, la ponctuation et les symboles typographiques
    # Rejette uniquement si un caractère alphabétique N'EST PAS du Latin (ex: Chinois, Arabe, Cyrillique)
    for char in texte:
        if char.isalpha() and not reg.match(r'\p{Script=Latin}', char):
            return False
    return True

def format_timestamp(seconds: float) -> str:
    """Convertit des secondes en format de timestamp SRT (HH:MM:SS,mmm)."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    milliseconds = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"

def time_to_seconds(t):
    """Convertit un objet pysrt time en secondes."""
    return t.hours * 3600 + t.minutes * 60 + t.seconds + t.milliseconds / 1000

