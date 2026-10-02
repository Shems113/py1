import functools
import json
import locale
import sys
import threading
from pathlib import Path
from typing import IO

import charset_normalizer
import pandas as pd

_LOCALE_LOCK = threading.Lock()
_UTF8_BOM = b"\xef\xbb\xbf"
_MISSING = {"", "N/A", "n/a"}

# Colonnes qui ressemblent à des nombres sans en être (ex. channel_layout = "5.1")
_NUMERIC_EXCLUDE = frozenset({"channel_layout", "codec_tag_string", "profile"})


def _is_text_column(s: pd.Series) -> bool:
    """Vrai pour une colonne texte, quel que soit son type :
    `object` (pandas 2) ou `str` (type texte par défaut depuis pandas 3)."""
    return pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s)


def _data_frame_coerce_numeric(
    df: pd.DataFrame,
    exclude: frozenset[str] | set[str] = _NUMERIC_EXCLUDE,
) -> pd.DataFrame:
    """Convertit en numérique les colonnes texte dont toutes les valeurs sont des nombres.

    - "N/A" et "" deviennent NaN.
    - Les colonnes entières passent en Int64 (nullable) : pas de "83429,0" en sortie.
    - `exclude` protège les colonnes qui ressemblent à des nombres sans en être.
    """
    out = df.copy()
    for col in out.columns:
        s = out[col]
        if col in exclude or not _is_text_column(s):
            continue
        s = s.astype(object).map(
            lambda v: None if isinstance(v, str) and v.strip() in _MISSING else v
        )
        try:
            conv = pd.to_numeric(s, errors="coerce")
        except (TypeError, ValueError):      # listes, dicts...
            continue
        # Ne convertir que si AUCUNE valeur non vide n'a été perdue
        if conv.notna().sum() == 0 or not conv.notna().equals(s.notna()):
            continue
        if (conv.dropna() % 1 == 0).all():
            conv = conv.astype("Int64")
        out[col] = conv
    return out


def _detect_encoding(file_path: str | Path) -> str:
    """Encodage d'un fichier texte : BOM d'abord (fiable et immédiat), sinon détection."""
    with open(file_path, "rb") as f:
        sample = f.read(100_000)
    if sample.startswith(_UTF8_BOM):
        return "utf-8-sig"  # sinon le BOM se retrouve collé au nom de la 1re colonne
    encoding = (charset_normalizer.detect(sample).get("encoding") or "utf-8").lower()
    # « ascii » n'est vrai que pour l'échantillon : un accent plus loin ferait planter
    # la lecture. UTF-8 est un sur-ensemble strict d'ASCII, donc sans risque.
    return "utf-8" if encoding in ("ascii", "us-ascii") else encoding


def data_frame_from_csv(
    file_path: str | Path,
    separator: str,
) -> pd.DataFrame:
    """Lit un CSV avec détection d'encodage."""
    return pd.read_csv(
        file_path,
        sep=separator,
        encoding=_detect_encoding(file_path),
        decimal=_get_decimal_separator(),
    )


def data_frame_to_csv(
    df: pd.DataFrame,
    target: str | Path | IO[str],
    separator: str,
    fields: list[str] | None = None,
    numeric_exclude: frozenset[str] | set[str] = _NUMERIC_EXCLUDE,
) -> None:
    """Exporte un DataFrame vers un CSV ou un flux stdout sans rompre la structure.

    Les colonnes texte entièrement numériques (ex. valeurs JSON de ffprobe) sont
    converties avant l'export, pour que le séparateur décimal de la locale s'applique.
    """
    if isinstance(target, (str, Path)):
        path_obj = Path(target)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        target = path_obj

    if fields:
        valid_fields = [col for col in fields if col in df.columns]
        df = df[valid_fields]

    extra_kwargs = {}

    if isinstance(target, Path):
        extra_kwargs["encoding"] = "utf-8-sig"
    else:
        if target is sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")

        extra_kwargs["lineterminator"] = "\n"

    df = _data_frame_coerce_numeric(df, exclude=numeric_exclude)

    df.to_csv(
        target,
        sep=separator,
        decimal=_get_decimal_separator(),
        index=False,
        **extra_kwargs,
    )


def data_frame_column_hashable(col: pd.Series) -> pd.Series:
    """Rend une colonne comptable : listes éclatées, dicts sérialisés."""
    # Seules les colonnes `object` peuvent contenir des listes ou des dicts ;
    # numériques, dates et colonnes `str` (pandas 3) : rien à faire, coût nul.
    if not pd.api.types.is_object_dtype(col):
        return col
    if col.map(lambda v: isinstance(v, (list, tuple, set))).any():
        col = col.explode()            # [] -> NaN, ignoré par value_counts
    return col.map(lambda v: json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
                   if isinstance(v, dict) else v)


@functools.cache
def _get_decimal_separator() -> str:
    """Séparateur décimal de la locale système (calculé une seule fois).

    Seule la catégorie LC_NUMERIC est modifiée, puis immédiatement restaurée :
    l'ancienne version passait tout le processus (LC_ALL) en locale système de
    façon permanente, ce qui changeait aussi le comportement d'autres modules."""
    with _LOCALE_LOCK:
        previous = locale.setlocale(locale.LC_NUMERIC)
        try:
            locale.setlocale(locale.LC_NUMERIC, "")
            return locale.localeconv().get("decimal_point") or "."
        except locale.Error:
            return "."
        finally:
            locale.setlocale(locale.LC_NUMERIC, previous)