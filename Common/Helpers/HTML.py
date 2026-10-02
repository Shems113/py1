import re

from Common.Helpers.Torrent import build_magnet

# Unités anglaises (KB, MiB…) et françaises (Ko, Mo, Go, To)
# Le \b après Ko/Mo/Go évite de lire « 3 Movies » comme « 3 Mo »
_UNIT = r"([KMGTkmgt]i?[Bb]|[KMGTkmgt][Oo]\b)"
# Séparateurs de milliers : point, virgule, espaces insécables. Pas l'espace normal,
# pour ne pas fusionner « Saison 2 700 MB » en 2700.
_NUMBER = r"(\d+(?:[\u00a0\u202f.,]\d+)*)"
_SIZE_KEYWORD = re.compile(rf"(?:Size|Taille|Length)\b[\s:]*{_NUMBER}\s*{_UNIT}", re.IGNORECASE)
_SIZE_ANY = re.compile(rf"{_NUMBER}\s*{_UNIT}")
_HASH_KEYWORD = re.compile(
    r"(?:hash|btih|torrent_hash|data-hash)[=:\s'\"]+([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})\b",
    re.IGNORECASE,
)
_HEX_HASH = re.compile(r"\b([a-fA-F0-9]{40})\b")
_NON_DIGITS = re.compile(r"[^\d]")
_THOUSANDS_SUFFIX = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([kKmM])\b")
_TPB_DATE = re.compile(r"Uploaded\s+([\d\-\:\s\w]+?),", re.IGNORECASE)


def _to_float(raw: str)-> float:
    """'1,5' -> 1.5 ; '1.234,5' / '1,234.5' / '1 234,5' -> 1234.5"""
    raw = re.sub(r"[\s\u00a0\u202f]", "", raw)
    if "," in raw and "." in raw:
        # Le séparateur le plus à droite est la décimale, l'autre les milliers
        decimal = "," if raw.rfind(",") > raw.rfind(".") else "."
        raw = raw.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    else:
        raw = raw.replace(",", ".")
    return float(raw)

def _format_size(raw_val: str, unit: str)-> str:
    unit = unit.upper().replace("IB", "iB").replace("O", "B")
    try:
        return f"{_to_float(raw_val):g} {unit}"
    except ValueError:
        return f"{raw_val} {unit}"

def parse_size(text: str)-> str:
    """Extrait et normalise la taille d'un fichier depuis du texte brut HTML.

    Gère les suffixes d'unités (KB, MB, GB, TiB..., et Ko, Mo, Go, To) ainsi que
    les préfixes de mots-clés (Size, Taille). Résultat en unités anglaises (ex. « 1.4 GB »).
    """
    if not text or text == "N/A":
        return "N/A"

    match = _SIZE_KEYWORD.search(text) or _SIZE_ANY.search(text)
    return _format_size(*match.groups()) if match else "N/A"

def extract_magnet_from_html(
    html_str: str,
    title: str = "",
    trackers: list[str] | None = None
    )-> str:
    """Recherche un InfoHash ou un hash hexadécimal dans du code HTML et reconstruit un lien magnet complet."""
    if not html_str:
        return "N/A"
    hash_match = _HASH_KEYWORD.search(html_str) or _HEX_HASH.search(html_str)
    return build_magnet(hash_match.group(1), title, trackers) if hash_match else "N/A"

def parse_int(value: str)-> int:
    """Extrait un entier depuis une chaîne (ex: '1 250 seeds' -> 1250, '1.2k' -> 1200).

    Retourne 0 si l'extraction échoue.
    """
    text = str(value)
    suffix = _THOUSANDS_SUFFIX.match(text)
    if suffix:
        number, unit = suffix.groups()
        return int(float(number.replace(",", ".")) * (1_000 if unit in "kK" else 1_000_000))
    digits = _NON_DIGITS.sub("", text)
    return int(digits) if digits else 0

def parse_tpb_date(text: str)-> str:
    """Extrait la date de publication spécifique aux éléments de The Pirate Bay."""
    match = _TPB_DATE.search(text or "")
    return match.group(1).strip() if match else "N/A"
