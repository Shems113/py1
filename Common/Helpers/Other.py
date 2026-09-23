import numbers
import os
import re
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import regex as reg
import requests


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

def handle_response(
    response: requests.Response,
    include_json: bool = False
    ) -> dict:
    """Affiche le statut HTTP de la réponse et retourne son contenu JSON.

    Si la requête a échoué ou ne contient pas de JSON valide, retourne un dictionnaire vide.
    """
    status = "OK" if response.ok else "KO"
    print(f"{status} : {response.status_code}")
    data = response.json() if response.ok else {}
    if include_json and response.ok:
        print(data)
    return data

def build_absolute_url(
    base_url: str,
    path: str
    ) -> str:
    """Convertit un lien relatif (ex: '/torrent/123') en URL absolue avec le domaine racine de base_url."""
    if not path or path.startswith("http"):
        return path or "N/A"
    base_domain = "/".join(base_url.split("/")[:3])
    return f"{base_domain}{path}" if path.startswith("/") else f"{base_domain}/{path}"

def clean_query(query: str) -> str:
    """Nettoie une chaîne de recherche en supprimant la ponctuation gênante (deux-points, tirets, etc.)."""
    q = re.sub(r'[:\'\-_]', ' ', query)
    return ' '.join(q.split())

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

def compute_carrefour_bill_values(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Calcule le résumé de facturation Carrefour."""
    df_calc = df.assign(priceWithTva=df["price"] * df["tva"] / 100)
    bill_date = datetime.now().astimezone() + timedelta(days=-1, hours=-5)
    total = df_calc["price"].sum()
    tva_20 = df_calc[df_calc["tva"] == 20]["price"].sum()
    tva_5_5 = df_calc[df_calc["tva"] == 5.5]["price"].sum()

    values = {
        "billdDateTime1": bill_date.strftime("%d/%m/%Y à %Hh%M"),
        "billdDateTime2": (bill_date + timedelta(minutes=2)).strftime(
            "%d.%m.%Y %H:%M"
        ),
        "total": total,
        "montantCBPayé": total,
        "totalTVA20": tva_20,
        "totalTVA5_5": tva_5_5,
        "totalTVA": tva_20 + tva_5_5,
        "tva": df_calc["priceWithTva"].sum(),
        "tva20": df_calc[df_calc["tva"] == 20]["priceWithTva"].sum(),
        "tva5_5": df_calc[df_calc["tva"] == 5.5]["priceWithTva"].sum(),
    }

    df_res = df_calc.drop(columns=["priceWithTva"]).assign(
        tva=df_calc["tva"].astype(str) + "%"
    )
    formatted = {
        k: f"{round(v, 2)}€" if isinstance(v, numbers.Number) else v
        for k, v in values.items()
    }
    return df_res, formatted

