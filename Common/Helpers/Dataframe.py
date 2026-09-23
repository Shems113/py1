import locale
import sys
from pathlib import Path
from typing import IO

import charset_normalizer
import pandas as pd

_DECIMAL_SEPARATOR: str | None = None

def data_frame_from_csv(
    file_path: str | Path,
    separator: str
) -> pd.DataFrame:
    """Lit un CSV avec détection d'encodage."""
    with open(file_path, "rb") as f:
        encoding = (
            charset_normalizer.detect(f.read(100_000)).get("encoding")
            or "utf-8"
        )
    return pd.read_csv(
        file_path,
        sep=separator,
        encoding=encoding,
        decimal=_get_decimal_separator(),
    )

def data_frame_to_csv(
    df: pd.DataFrame,
    target: str | Path | IO[str],
    separator: str,
    fields: list[str] | None = None,
) -> None:
    """Exporte un DataFrame vers un CSV ou un flux stdout sans rompre la structure."""
    if isinstance(target, (str, Path)):
        path_obj = Path(target)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        target = path_obj

    if fields:
        valid_fields = [col for col in fields if col in df.columns]
        df = df[valid_fields]

    is_path = isinstance(target, Path)
    extra_kwargs = {}

    if is_path:
        extra_kwargs["encoding"] = "utf-8-sig"
    else:
        if target is sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")

        extra_kwargs["lineterminator"] = "\n"

    df.to_csv(
        target,
        sep=separator,
        decimal=_get_decimal_separator(),
        index=False,
        **extra_kwargs,
    )

def _get_decimal_separator() -> str:
    """Initialise la locale et retourne le séparateur décimal (mis en cache)."""
    global _DECIMAL_SEPARATOR
    if _DECIMAL_SEPARATOR is None:
        try:
            locale.setlocale(locale.LC_ALL, "")
        except locale.Error:
            pass
        _DECIMAL_SEPARATOR = locale.localeconv().get("decimal_point", ".")
    return _DECIMAL_SEPARATOR
