from typing import Any

import regex as reg

# Une lettre (catégorie Unicode L, comme str.isalpha) qui n'est PAS de script latin
_NON_LATIN_LETTER = reg.compile(r"(?=\p{L})\P{Script=Latin}")


def is_latin(texte: str)-> bool:
    # Autorise les caractères de script Latin, les chiffres, la ponctuation et les symboles typographiques
    # Rejette uniquement si un caractère alphabétique N'EST PAS du Latin (ex: Chinois, Arabe, Cyrillique)
    # Une seule recherche compilée au lieu d'un appel regex par caractère
    return _NON_LATIN_LETTER.search(texte) is None

def format_timestamp(seconds: float)-> str:
    """Convertit des secondes en format de timestamp SRT (HH:MM:SS,mmm)."""
    # Calcul en millisecondes entières : évite les erreurs d'arrondi flottant
    # (ex. 1.999 donnait « 00:00:01,998 ») et les valeurs négatives
    total_ms = max(0, round(seconds * 1000))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, milliseconds = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"

def time_to_seconds(t: Any)-> float:
    """Convertit un objet pysrt time en secondes."""
    return t.hours * 3600 + t.minutes * 60 + t.seconds + t.milliseconds / 1000
