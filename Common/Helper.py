import numpy as np
import os
import re
import regex as reg
import urllib.parse
from typing import List, Optional
import pandas as pd
import requests


def check_required_env_vars(service_name: str, var_names: List[str]) -> List[str]:
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


def handle_response(response: requests.Response, include_json: bool = False) -> dict:
    """Affiche le statut HTTP de la réponse et retourne son contenu JSON.

    Si la requête a échoué ou ne contient pas de JSON valide, retourne un dictionnaire vide.
    """
    status = "OK" if response.ok else "KO"
    print(f"{status} : {response.status_code}")
    data = response.json() if response.ok else {}
    if include_json and response.ok:
        print(data)
    return data


def _concat_unique_data(series: pd.Series) -> str:
    """Joint les valeurs uniques non nuls d'une Série avec un séparateur pipe."""
    return " | ".join(series.dropna().astype(str).unique())


def select_best_quality_torrent(df_torrents: pd.DataFrame) -> pd.DataFrame:
    """Filtre et classe un DataFrame de torrents pour sélectionner la meilleure option par film.

    Évalue la résolution (480p à 2160p), convertit la taille en Go et privilégie
    les releases <= 5 Go ayant le plus grand nombre de seeders.
    """
    if df_torrents.empty:
        return df_torrents.copy()

    df = df_torrents.dropna(subset=["quality"]).copy()
    df["quality_clean"] = df["quality"].astype(str).str.strip().str.upper()
    df["size_clean"] = df["size"].astype(str).str.strip().str.upper()

    # 1. Extraction et conversion vectorisée de la taille en Go (via regex capturante)
    # Match le nombre (ex: 4.5) et l'unité (ex: GIB, MB, KB)
    pattern = r"([\d.,]+)\s*([A-Z]+)?"
    extracted = df["size_clean"].str.extract(pattern)
    numeric_sizes = pd.to_numeric(
        extracted[0].str.replace(",", "."), errors="coerce"
    ).fillna(np.inf)
    units = extracted[1].fillna("GB")

    unit_multipliers = {
        "GB": 1.0,
        "GIB": 1.0,
        "MB": 1.0 / 1024.0,
        "MIB": 1.0 / 1024.0,
        "KB": 1.0 / (1024.0**2),
        "KIB": 1.0 / (1024.0**2),
        "B": 1.0 / (1024.0**3),
    }

    df["size_gb"] = numeric_sizes * units.map(unit_multipliers).fillna(1.0)

    # 2. Ranking de la qualité (0: 480p -> 4: 2160p)
    qualities = ["480P", "BLURAY", "720P", "1080P", "2160P"]
    df["quality_rank"] = -1
    for rank, qual in enumerate(qualities):
        mask = df["quality_clean"].str.contains(qual, regex=False)
        df.loc[mask, "quality_rank"] = rank

    # 3. Agrégation par version de torrent
    df_agg = df.groupby(
        ["tmdb_id", "name", "magnet"], as_index=False
    ).agg(
        {
            "source": _concat_unique_data,
            "title": _concat_unique_data,
            "quality": _concat_unique_data,
            "seeds": "sum",
            "leechs": "sum",
            "size_gb": "mean",
            "quality_rank": "max",
        }
    )

    # 4. Calcul du score/rang d'éligibilité (taille <= 5 Go et au moins 1 seeder)
    is_valid = (
        (df_agg["size_gb"] <= 5.0)
        & (df_agg["size_gb"] > 0)
        & (df_agg["quality_rank"] >= 0)
        & (df_agg["seeds"] > 0)
    )
    df_agg["rank_score"] = df_agg["quality_rank"].where(is_valid, -1)

    # 5. Tri et sélection du meilleur torrent par film (tmdb_id)
    df_sorted = df_agg.sort_values(
        by=["tmdb_id", "rank_score", "quality_rank", "size_gb", "seeds"],
        ascending=[True, False, False, True, False],
    )
    df_sorted=df_sorted[["tmdb_id","name","source","title","quality","seeds","leechs","size_gb","quality_rank","rank_score","magnet"]]

    # Identification du meilleur enregistrement
    df_sorted["is_best"] = False
    best_indices = df_sorted.groupby("tmdb_id", as_index=False).head(1).index
    df_sorted.loc[best_indices, "is_best"] = True

    return df_sorted

def parse_int(value: str) -> int:
    """Extrait un entier depuis une chaîne (ex: '1 250 seeds' -> 1250).

    Retourne 0 si l'extraction échoue.
    """
    try:
        return int(re.sub(r"[^\d]", "", str(value)))
    except (ValueError, TypeError):
        return 0


def parse_size(text: str) -> str:
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


def parse_quality(title: str) -> str:
    """Analyse le titre d'une release pour détecter sa qualité ou résolution (ex: 1080p, Bluray, WEB)."""
    patterns = [
        (r"\b(2160p)\b", "2160p"),
        (r"\b(1080p)\b", "1080p"),
        (r"\b(720p)\b", "720p"),
        (r"\b(480p|360p)\b", "480p"),
        (r"\b(4k|uhd)\b", "2160p"),
        (r"\b(fhd)\b", "1080p"),
        (r"\b(hd)\b", "720p"),
        (r"\b(sd)\b", "480p"),
        (r"\b(bluray|bdrip|brrip)\b", "Bluray"),
        (r"\b(web-dl|webrip|web)\b", "WEB"),
        (r"\b(dvdrip|dvd)\b", "DVD"),
        (r"\b(hdrip|hdtv)\b", "HDTV"),
        (r"\b(cam|hdcam|camrip)\b", "CAM"),
        (r"\b(ts|hdts|telesync)\b", "TS"),
    ]
    title_lower = title.lower()
    for pattern, label in patterns:
        if re.search(pattern, title_lower):
            return label
    return "N/A"


def parse_tpb_date(text: str) -> str:
    """Extrait la date de publication spécifique aux éléments de The Pirate Bay."""
    match = re.search(r"Uploaded\s+([\d\-\:\s\w]+?),", text, re.IGNORECASE)
    return match.group(1).strip() if match else "N/A"


def build_absolute_url(base_url: str, path: str) -> str:
    """Convertit un lien relatif (ex: '/torrent/123') en URL absolue avec le domaine racine de base_url."""
    if not path or path.startswith("http"):
        return path or "N/A"
    base_domain = "/".join(base_url.split("/")[:3])
    return f"{base_domain}{path}" if path.startswith("/") else f"{base_domain}/{path}"


def extract_magnet_from_html(html_str: str, title: str = "", trackers: Optional[List[str]] = None) -> str:
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


def clean_query(query: str) -> str:
    """Nettoie une chaîne de recherche en supprimant la ponctuation gênante (deux-points, tirets, etc.)."""
    q = re.sub(r'[:\'\-_]', ' ', query)
    return ' '.join(q.split())


def get_mirrors_from_env(env_var: str, defaults: List[str]) -> List[str]:
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