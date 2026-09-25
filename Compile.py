"""Contrôle qualité du projet : à lancer avant chaque commit ou livraison.

Usage :
    python Compile.py            # tous les contrôles, résumé des erreurs mypy
    python Compile.py --details  # affiche aussi le détail complet de mypy

Code de sortie : 0 si aucun contrôle bloquant n'échoue, 1 sinon.
Dépendances : python -m pip install ruff deptry mypy
"""

import argparse
import importlib
import os
import py_compile
import subprocess
import sys
import traceback
from collections import Counter
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from deptry.cli import cli as deptry_cli
from mypy import api as mypy_api
from packaging.requirements import InvalidRequirement, Requirement
from ruff.__main__ import find_ruff_bin

from Common.Helpers.File import PROJECT_ROOT

REQUIREMENTS_FILE = PROJECT_ROOT / "requirements.txt"
THIS_FILE = Path(__file__).resolve()

# Statuts d'un contrôle
OK, WARN, ERROR = "✅", "⚠️", "❌"


# --------------------------------------------------------------------------- #
# Utilitaires
# --------------------------------------------------------------------------- #
def iter_project_files() -> list[Path]:
    """Liste les .py du projet, hors dossiers cachés, environnements virtuels et __pycache__."""
    files = []
    for py_file in sorted(PROJECT_ROOT.rglob("*.py")):
        parts = py_file.relative_to(PROJECT_ROOT).parts
        if any(p.startswith((".", "__")) or "venv" in p for p in parts[:-1]):
            continue
        files.append(py_file)
    return files


def indent(text: str) -> str:
    """Indente chaque ligne d'une sortie d'outil pour la lisibilité."""
    return "\n".join(f"   {line}" for line in text.strip().splitlines())


def ruff_summary(output: str) -> str:
    """Extrait la ligne « Found N errors » de la sortie Ruff."""
    return next((line for line in output.splitlines() if line.startswith("Found")), output.strip())


def run_ruff(*args: str) -> subprocess.CompletedProcess:
    """Lance l'exécutable Ruff de l'environnement courant (Ruff n'a pas d'API Python)."""
    return subprocess.run(
        [find_ruff_bin(), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=PROJECT_ROOT,
    )


# --------------------------------------------------------------------------- #
# Contrôles : chacun renvoie (statut, résumé d'une ligne)
# --------------------------------------------------------------------------- #
def check_syntax() -> tuple[str, str]:
    """Compile chaque fichier pour détecter les erreurs de syntaxe."""
    errors = 0
    for py_file in iter_project_files():
        try:
            py_compile.compile(str(py_file), doraise=True)
        except py_compile.PyCompileError as e:
            print(f"   {py_file.relative_to(PROJECT_ROOT)} :\n{e}\n")
            errors += 1
    return (ERROR, f"{errors} fichier(s) en erreur") if errors else (OK, "aucune erreur de syntaxe")


def fix_unused_imports() -> tuple[str, str]:
    """Supprime automatiquement les imports inutilisés (F401)."""
    result = run_ruff("check", "--select", "F401", "--fix", "--output-format", "concise", ".")
    output = result.stdout or result.stderr
    print(indent(output))
    return (OK, "imports inutilisés nettoyés") if result.returncode == 0 else (WARN, ruff_summary(output))


def check_ruff_lint() -> tuple[str, str]:
    """Lint Ruff avec la configuration du projet (ruff.toml) : noms non définis, imports mal placés..."""
    result = run_ruff("check", "--output-format", "concise", ".")
    output = result.stdout or result.stderr
    if result.returncode == 0:
        return OK, "aucun problème"
    print(indent(output))
    return ERROR, ruff_summary(output)


def check_module_imports() -> tuple[str, str]:
    """Importe chaque module : détecte les erreurs qui n'apparaissent qu'au chargement.

    Exemples : NameError (Optional non importé), TypeError ([Optional][str]),
    argument sans valeur par défaut après un argument qui en a une.
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    failures = 0
    total = 0
    for py_file in iter_project_files():
        if py_file == THIS_FILE:
            continue
        total += 1
        module = ".".join(py_file.relative_to(PROJECT_ROOT).with_suffix("").parts)
        try:
            importlib.import_module(module)
        except Exception as e:  # noqa: BLE001 - on veut toutes les erreurs de chargement
            failures += 1
            where = traceback.extract_tb(e.__traceback__)[-1]
            print(f"   {module} : {type(e).__name__}: {e}  ({Path(where.filename).name}:{where.lineno})")
    if failures:
        return ERROR, f"{failures}/{total} module(s) ne se chargent pas"
    return OK, f"{total} modules chargés"


def check_types(show_details: bool) -> tuple[str, str]:
    """Vérification de types avec mypy (non bloquant : beaucoup de remarques sont des annotations)."""
    targets = [str(p.relative_to(PROJECT_ROOT)) for p in iter_project_files() if p != THIS_FILE]
    stdout, stderr, status = mypy_api.run(
        ["--ignore-missing-imports", "--explicit-package-bases", "--no-error-summary", *targets]
    )
    if status == 2:  # mypy lui-même n'a pas pu tourner
        print(indent(stderr))
        return ERROR, "mypy n'a pas pu s'exécuter"

    errors = [line for line in stdout.splitlines() if ": error:" in line]
    if not errors:
        return OK, "aucune erreur de typage"

    if show_details:
        print("\n".join(f"   {line}" for line in errors))
    else:
        by_code = Counter(line.rsplit("[", 1)[-1].rstrip("]") for line in errors if "[" in line)
        for code, count in by_code.most_common():
            print(f"   {count:4d}  {code}")
        print("   (détail complet : python Compile.py --details)")
    return WARN, f"{len(errors)} remarque(s) de typage"


def check_hardcoded_secrets() -> tuple[str, str]:
    """Repère les mots de passe / tokens écrits en dur (règles Bandit S105-S107 de Ruff)."""
    result = run_ruff("check", "--select", "S105,S106,S107", "--output-format", "concise", "--no-fix", ".")
    if result.returncode == 0:
        return OK, "aucun secret détecté"
    findings = [line for line in result.stdout.splitlines() if " S10" in line]
    print("\n".join(f"   {line}" for line in findings))
    return WARN, f"{len(findings)} secret(s) probable(s) dans le code"


def check_requirements_installed() -> tuple[str, str]:
    """Vérifie que chaque paquet de requirements.txt est installé dans l'environnement courant."""
    if not REQUIREMENTS_FILE.is_file():
        return WARN, "requirements.txt introuvable"

    problems = 0
    for raw_line in REQUIREMENTS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement:
            print(f"   ligne invalide : {raw_line}")
            problems += 1
            continue
        if req.marker and not req.marker.evaluate():
            continue  # ex. pywin32 ; sys_platform == 'win32' sous Linux
        try:
            installed = version(req.name)
        except PackageNotFoundError:
            print(f"   {req.name} : non installé")
            problems += 1
            continue
        if req.specifier and not req.specifier.contains(installed, prereleases=True):
            print(f"   {req.name} : version {installed} installée, attendu {req.specifier}")
            problems += 1
    if problems:
        return ERROR, f"{problems} paquet(s) manquant(s) ou incompatible(s)"
    return OK, "tous les paquets requis sont installés"


def check_unused_requirements() -> tuple[str, str]:
    """Paquets déclarés mais non importés, ou importés mais non déclarés (deptry)."""
    args = [
        str(PROJECT_ROOT),
        "--requirements-files", str(REQUIREMENTS_FILE),
        # pywin32 est importé dynamiquement (__import__("win32com.client")) dans CWord.py
        "--per-rule-ignores", "DEP002=pywin32",
        # ce script d'outillage importe ruff, deptry et mypy : on l'exclut de l'analyse
        "--extend-exclude", r".*Compile\.py$",
    ]
    try:
        deptry_cli.main(args, standalone_mode=False)
    except SystemExit as e:
        if e.code not in (0, None):
            return ERROR, "dépendances à corriger (voir ci-dessus)"
    return OK, "requirements.txt cohérent avec le code"


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="Contrôle qualité du projet.")
    parser.add_argument("--details", action="store_true", help="affiche le détail complet de mypy")
    args = parser.parse_args()

    os.chdir(PROJECT_ROOT)  # Ruff, mypy et deptry lisent leur configuration depuis la racine
    print(f"🐍 Interpréteur : {sys.executable}")
    print(f"📁 Projet       : {PROJECT_ROOT}\n")

    checks: list[tuple[str, Callable[[], tuple[str, str]]]] = [
        ("Syntaxe", check_syntax),
        ("Imports inutilisés (Ruff --fix)", fix_unused_imports),
        ("Lint Ruff", check_ruff_lint),
        ("Chargement des modules", check_module_imports),
        ("Typage (mypy)", lambda: check_types(args.details)),
        ("Secrets en dur", check_hardcoded_secrets),
        ("Paquets installés", check_requirements_installed),
        ("Dépendances inutiles (deptry)", check_unused_requirements),
    ]

    summary: list[tuple[str, str, str]] = []
    for name, check in checks:
        print(f"── {name} " + "─" * max(0, 60 - len(name)))
        status, message = check()
        summary.append((status, name, message))
        print(f"{status} {message}\n")
        if name == "Syntaxe" and status == ERROR:
            print("❌ Contrôles suivants annulés : corrige d'abord les erreurs de syntaxe.")
            return 1

    print("═" * 64)
    print("RÉSUMÉ")
    for status, name, message in summary:
        print(f"{status}  {name:<34} {message}")
    print("═" * 64)

    return 1 if any(status == ERROR for status, _, _ in summary) else 0


if __name__ == "__main__":
    sys.exit(main())