import base64
import binascii
import re
import urllib.parse
from collections.abc import Iterable

import numpy as np
import pandas as pd

# Conversion vers Go (unités anglaises et françaises)
_UNIT_TO_GB = {
    "TB": 1024.0, "TIB": 1024.0, "TO": 1024.0,
    "GB": 1.0, "GIB": 1.0, "GO": 1.0,
    "MB": 1.0 / 1024.0, "MIB": 1.0 / 1024.0, "MO": 1.0 / 1024.0,
    "KB": 1.0 / (1024.0**2), "KIB": 1.0 / (1024.0**2), "KO": 1.0 / (1024.0**2),
    "B": 1.0 / (1024.0**3), "O": 1.0 / (1024.0**3),
}
_MAGNET_HASH = re.compile(r"xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})(?![a-zA-Z0-9])")
_QUALITY_PATTERNS = [(re.compile(p), label) for p, label in [
    # 1. Résolutions explicites précises (conservées intactes)
    (r"\b(2160p)\b", "2160p"),
    (r"\b(1080p)\b", "1080p"),
    (r"\b(720p)\b", "720p"),
    (r"\b(480p|360p)\b", "480p"),
    # 2. Equivalents textuels stricts
    (r"\b(fhd)\b", "1080p"),
    (r"\b(sd)\b", "480p"),
    # 3. Termes génériques/marketing (évalués après les résolutions explicites)
    (r"\b(4k|uhd|hdr|hdr10|dv|dolby\s*vision)\b", "2160p"),
    (r"\b(hd)\b", "720p"),
    # 4. Sources et formats
    (r"\b(bluray|bdrip|brrip|remux)\b", "Bluray"),
    (r"\b(web-dl|webrip|web)\b", "WEB"),
    (r"\b(dvdrip|dvd)\b", "DVD"),
    (r"\b(hdrip|hdtv)\b", "HDTV"),
    (r"\b(cam|hdcam|camrip)\b", "CAM"),
    (r"\b(ts|hdts|telesync)\b", "TS"),
]]


def _size_to_gb(sizes: pd.Series)-> pd.Series:
    """'4.5 GiB', '700 Mo', '1,234.5 MB', '1.2 TB' -> taille en Go (inf si illisible)."""
    extracted = sizes.astype(str).str.strip().str.upper().str.extract(
        r"(\d+(?:[\s.,]\d+)*)\s*([A-Z]+)?")
    raw = extracted[0].fillna("").str.replace(r"\s", "", regex=True)
    both = raw.str.contains(",") & raw.str.contains(r"\.")
    comma_is_decimal = raw.str.rfind(",") > raw.str.rfind(".")
    # « 1,234.5 » -> 1234.5 ; « 1.234,5 » -> 1234.5 ; « 1,5 » -> 1.5
    raw = raw.where(~(both & ~comma_is_decimal), raw.str.replace(",", "", regex=False))
    raw = raw.where(~(both & comma_is_decimal), raw.str.replace(".", "", regex=False))
    numeric = pd.to_numeric(raw.str.replace(",", ".", regex=False), errors="coerce").fillna(np.inf)
    multiplier = extracted[1].fillna("GB").map(_UNIT_TO_GB)
    # Unité inconnue : taille considérée illisible (auparavant comptée comme des Go)
    return numeric * multiplier.fillna(np.inf)

def select_best_quality_torrent(df_torrents: pd.DataFrame)-> pd.DataFrame:
    """Filtre et classe un DataFrame de torrents pour sélectionner la meilleure option par film.

    Évalue la résolution (480p à 2160p), convertit la taille en Go et privilégie
    les releases <= 5 Go ayant le plus grand nombre de seeders.
    """
    if df_torrents.empty:
        return df_torrents.copy()

    df = df_torrents.dropna(subset=["quality"]).copy()
    df["quality_clean"] = df["quality"].astype(str).str.strip().str.upper()

    # 1. Conversion vectorisée de la taille en Go
    df["size_gb"] = _size_to_gb(df["size"])

    # 2. Ranking de la qualité (0: 480p -> 4: 2160p) ; le rang le plus élevé trouvé l'emporte
    qualities = ["480P", "BLURAY", "720P", "1080P", "2160P"]
    df["quality_rank"] = -1
    for rank, qual in enumerate(qualities):
        mask = df["quality_clean"].str.contains(qual, regex=False)
        df.loc[mask, "quality_rank"] = rank

    # 3. Extraction du hash ; sans hash exploitable, chaque ligne reste un torrent distinct
    #    (auparavant ces lignes disparaissaient du groupby, qui ignore les clés nulles)
    hashes = df["magnet"].map(_extract_magnet_hash)
    df["magnet_hash"] = hashes.fillna(pd.Series("row_" + df.index.astype(str), index=df.index))

    # 4. Agrégation par version de torrent
    df_agg = df.groupby(
        ["tmdb_id", "name", "magnet_hash"], as_index=False
    ).agg(
        {
            "source": _concat_unique_data,
            "title": _concat_unique_data,
            "quality": _concat_unique_data,
            "seeds": "sum",
            "leechs": "sum",
            "size_gb": "first",
            "quality_rank": "max",
            "magnet": "first",
        }
    )

    # 5. Calcul vectorisé du score d'éligibilité (taille <= 5 Go et au moins 1 seeder)
    is_valid = (
        (df_agg["size_gb"] <= 5.0)
        & (df_agg["size_gb"] > 0)
        & (df_agg["quality_rank"] >= 0)
        & (df_agg["seeds"] > 0)
    )
    df_agg["rank_score"] = df_agg["quality_rank"].where(is_valid, -1)

    # 6. Dictionnaire de sens de tri pour chaque colonne
    ascending_cols = {
        "tmdb_id": True,
        "rank_score": False,
        "quality_rank": False,
        "size_gb": True,   # Plus petite taille en premier
        "seeds": False,    # Plus grand nombre de seeders en premier
    }

    # Liste des critères secondaires (1: Taille puis Seeds, 2: Seeds puis Taille)
    criteria_cols = [["size_gb", "seeds"], ["seeds", "size_gb"]]
    idx_criteria = []

    # 7. Rangs 1 et 2 pour chaque critère : cumcount donne la position dans le groupe trié
    for cols in criteria_cols:
        by_cols = ["tmdb_id", "rank_score", "quality_rank", *cols]
        df_criteria = df_agg.sort_values(
            by=by_cols, ascending=[ascending_cols[col] for col in by_cols], kind="stable"
        )
        position = df_criteria.groupby("tmdb_id", sort=False).cumcount()
        idx_criteria.append((position.index[position == 0], position.index[position == 1]))

    # 8. Masques booléens vectorisés
    idx_all = df_agg.index

    # Critère 0 (Taille prioritaire)
    is_s1 = idx_all.isin(idx_criteria[0][0])
    is_s2 = idx_all.isin(idx_criteria[0][1])

    # Critère 1 (Seeds prioritaires)
    is_d1 = idx_all.isin(idx_criteria[1][0])
    is_d2 = idx_all.isin(idx_criteria[1][1])

    # 9. Application des règles de priorité
    conditions = [
        is_s1 & is_d1,  # #1_best
        is_s1,          # #2_best_quality_lower_size
        is_d1,          # #3_best_quality_best_seeds
        is_s2 & is_d2,  # #4_best_2
        is_s2,          # #5_best_quality_lower_size_2
        is_d2,          # #6_best_quality_best_seeds_2
    ]

    choices = [
        "#1_best",
        "#2_best_quality_lower_size",
        "#3_best_quality_best_seeds",
        "#4_best_2",
        "#5_best_quality_lower_size_2",
        "#6_best_quality_best_seeds_2",
    ]

    df_agg = (
        df_agg.assign(selection_status= np.select(conditions, choices, default="#7_other"))
        .sort_values(by=["tmdb_id","selection_status"], ascending=[True,True])
        )

    # 10. Re-tri final sur la vue principale et sélection des colonnes
    output_cols = [
        "tmdb_id",
        "name",
        "source",
        "title",
        "quality",
        "seeds",
        "leechs",
        "size_gb",
        "quality_rank",
        "rank_score",
        "selection_status",
        "magnet",
    ]

    return df_agg[output_cols]

def _concat_unique_data(series: pd.Series)-> str:
    """Concatène les valeurs uniques non nuls d'une série Pandas."""
    unique_vals = series.dropna().unique()
    return " | ".join(str(v) for v in unique_vals if v)

def _extract_magnet_hash(magnet_link: str)-> str | None:
    """Extrait le Info Hash (BTIH) d'un lien magnet, toujours en hexadécimal minuscule.

    Un même torrent peut être annoncé en hexadécimal (40 car.) ou en Base32 (32 car.) :
    le Base32 est converti pour que les deux formes soient reconnues comme identiques."""
    if not isinstance(magnet_link, str):
        return None

    match = _MAGNET_HASH.search(magnet_link)
    if not match:
        return None
    info_hash = match.group(1)
    if len(info_hash) == 32:
        try:
            return base64.b32decode(info_hash.upper()).hex()
        except (binascii.Error, ValueError):
            return info_hash.lower()
    return info_hash.lower()

def is_same_magnet(
    link1: str,
    link2: str)-> bool:
    """Vérifie si deux liens magnet pointent vers le même torrent."""
    h1 = _extract_magnet_hash(link1)
    h2 = _extract_magnet_hash(link2)
    return h1 is not None and h1 == h2

def parse_quality_torrent(title: str)-> str:
    """Analyse le titre d'une release pour détecter sa qualité ou résolution (ex: 1080p, Bluray, WEB)."""
    if not title:
        return "N/A"
    title_lower = str(title).lower()
    for pattern, label in _QUALITY_PATTERNS:
        if pattern.search(title_lower):
            return label
    return "N/A"

# Statuts retenus pour lancer un téléchargement après select_best_quality_torrent
DOWNLOAD_STATUSES = ("#1_best", "#2_best_quality_lower_size")

def select_torrents_to_download(df_best: pd.DataFrame)-> pd.DataFrame:
    """Lignes de select_best_quality_torrent à télécharger : les deux meilleurs choix par film,
    avec un lien exploitable (magnet, ou URL d'un fichier .torrent)."""
    if df_best.empty:
        return df_best
    selected = df_best[df_best["selection_status"].isin(DOWNLOAD_STATUSES)]
    return selected[selected["magnet"].astype(str).str.startswith(("magnet:?", "http://", "https://"))]

def build_magnet(
    info_hash: str,
    title: str = "",
    trackers: Iterable[str] | None = None,
    )-> str:
    """Lien magnet à partir d'un Info Hash, avec nom affiché et trackers optionnels."""
    name = f"&dn={urllib.parse.quote(title)}" if title else ""
    trackers_str = "".join(f"&tr={urllib.parse.quote(t, safe='')}" for t in trackers or [])
    return f"magnet:?xt=urn:btih:{info_hash}{name}{trackers_str}"

def format_bytes(size_in_bytes: float)-> str:
    """1536 -> '1.50 KB' ; 0 ou négatif -> 'N/A'."""
    if not size_in_bytes or size_in_bytes <= 0:
        return "N/A"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_in_bytes < 1024.0:
            return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024.0
    return f"{size_in_bytes:.2f} PB"
