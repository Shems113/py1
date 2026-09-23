import re

import numpy as np
import pandas as pd


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

    # 3. Extraction du hash dans une nouvelle colonne temporaire
    df["magnet_hash"] = df["magnet"].apply(_extract_magnet_hash)

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

    # 7. Extraction générique des rangs 1 et 2 pour chaque critère
    for cols in criteria_cols:
        by_cols = ["tmdb_id", "rank_score", "quality_rank"] + cols
        df_criteria = df_agg.sort_values(
            by=by_cols, ascending=[ascending_cols[col] for col in by_cols]
        )

        # Rang 1
        idx_1 = set(df_criteria.groupby("tmdb_id", observed=True).head(1).index)
        # Rang 2 (après exclusion du rang 1)
        idx_2 = set(
            df_criteria[~df_criteria.index.isin(idx_1)]
            .groupby("tmdb_id", observed=True)
            .head(1)
            .index
        )
        idx_criteria.append((idx_1, idx_2))

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

def _concat_unique_data(series: pd.Series) -> str:
    """Concatène les valeurs uniques non nuls d'une série Pandas."""
    unique_vals = series.dropna().unique()
    return " | ".join(str(v) for v in unique_vals if v)

def _extract_magnet_hash(magnet_link: str) -> str | None:
    """Extrait le Info Hash (BTIH) d'un lien magnet et le met en minuscules."""
    if not isinstance(magnet_link, str):
        return None

    # Capture le hash hexadécimal (40 car.) ou Base32 (32 car.)
    match = re.search(
        r"xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet_link
    )
    return match.group(1).lower() if match else None

def is_same_magnet(
    link1: str,
    link2: str) -> bool:
    """Vérifie si deux liens magnet pointent vers le même torrent."""
    h1 = _extract_magnet_hash(link1)
    h2 = _extract_magnet_hash(link2)
    return h1 is not None and h1 == h2

def parse_quality_torrent(title: str) -> str:
    """Analyse le titre d'une release pour détecter sa qualité ou résolution (ex: 1080p, Bluray, WEB)."""
    patterns = [
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
    ]
    title_lower = title.lower()
    for pattern, label in patterns:
        if re.search(pattern, title_lower):
            return label
    return "N/A"



