import re
import urllib.parse


def parse_size(text: str)-> str:
    """Extrait et normalise la taille d'un fichier depuis du texte brut HTML.

    Gère les suffixes d'unités (KB, MB, GB, TiB...) et les préfixes de mots-clés (Size, Taille).
    """
    if not text or text == "N/A":
        return "N/A"

    size_pattern = re.search(
        r'(?:Size|Taille|Length)\b[\s:]*([\d\,\.]+)\s*([KMGTkmgt]i?[Bb])',
        text,
        re.IGNORECASE
    )
    if size_pattern:
        raw_val, unit = size_pattern.groups()
        try:
            val = float(raw_val.replace(',', '.'))
            return f"{val:g} {unit.upper().replace('IB', 'iB')}"
        except ValueError:
            return f"{raw_val} {unit}"

    fallback_pattern = re.search(r'(\d+(?:[\,\.]\d+)?)\s*([KMGTkmgt]i?[Bb])', text)
    if fallback_pattern:
        raw_val, unit = fallback_pattern.groups()
        try:
            val = float(raw_val.replace(',', '.'))
            return f"{val:g} {unit.upper().replace('IB', 'iB')}"
        except ValueError:
            return f"{raw_val} {unit}"

    return "N/A"

def extract_magnet_from_html(
    html_str: str,
    title: str = "",
    trackers: list[str] | None = None
    )-> str:
    """Recherche un InfoHash ou un hash hexadécimal dans du code HTML et reconstruit un lien magnet complet."""
    trackers_str = "".join(f"&tr={urllib.parse.quote(t)}" for t in (trackers or []))
    title_str = f"&dn={urllib.parse.quote(title)}" if title else ""

    hash_match = re.search(
        r"(?:hash|btih|torrent_hash|data-hash)[=:\s'\"]+([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})",
        html_str,
        re.IGNORECASE,
    )
    if hash_match:
        return f"magnet:?xt=urn:btih:{hash_match.group(1)}{title_str}{trackers_str}"

    hex_match = re.search(r"\b([a-fA-F0-9]{40})\b", html_str)
    if hex_match:
        return f"magnet:?xt=urn:btih:{hex_match.group(1)}{title_str}{trackers_str}"

    return "N/A"

def parse_int(value: str)-> int:
    """Extrait un entier depuis une chaîne (ex: '1 250 seeds' -> 1250).

    Retourne 0 si l'extraction échoue.
    """
    try:
        return int(re.sub(r"[^\d]", "", str(value)))
    except (ValueError, TypeError):
        return 0

def parse_tpb_date(text: str)-> str:
    """Extrait la date de publication spécifique aux éléments de The Pirate Bay."""
    match = re.search(r"Uploaded\s+([\d\-\:\s\w]+?),", text, re.IGNORECASE)
    return match.group(1).strip() if match else "N/A"
