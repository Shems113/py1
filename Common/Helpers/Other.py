
import regex as reg


def is_latin(texte: str)-> bool:
    # Autorise les caractères de script Latin, les chiffres, la ponctuation et les symboles typographiques
    # Rejette uniquement si un caractère alphabétique N'EST PAS du Latin (ex: Chinois, Arabe, Cyrillique)
    for char in texte:
        if char.isalpha() and not reg.match(r'\p{Script=Latin}', char):
            return False
    return True

def format_timestamp(seconds: float)-> str:
    """Convertit des secondes en format de timestamp SRT (HH:MM:SS,mmm)."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    milliseconds = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"

def time_to_seconds(t):
    """Convertit un objet pysrt time en secondes."""
    return t.hours * 3600 + t.minutes * 60 + t.seconds + t.milliseconds / 1000

