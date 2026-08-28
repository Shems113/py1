import subprocess
import sys
from pathlib import Path
import py_compile


def check_syntax(directory: Path) -> bool:
    """Vérifie la syntaxe de tous les fichiers .py du projet."""
    print("🔍 Analyse de la syntaxe Python en cours...")
    has_error = False
    
    for py_file in directory.rglob("*.py"):
        # Ignorer les environnements virtuels (.venv, venv, etc.)
        if any(part.startswith(".") or "venv" in part for part in py_file.parts):
            continue
            
        try:
            py_compile.compile(str(py_file), doraise=True)
        except py_compile.PyCompileError as e:
            print(f"❌ Erreur de syntaxe dans {py_file} :\n{e}\n")
            has_error = True

    if not has_error:
        print("✅ Aucun problème de syntaxe détecté !\n")
    return not has_error


def clean_unused_requirements(directory: Path) -> None:
    """Détecte et signale les dépendances installées ou déclarées mais non importées dans le code."""
    print("🧹 Recherche des dépendances inutilisées dans le projet...")

    # Utilisation de pip-extra-reqs (à installer via `pip install pip-extra-reqs`)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pipreqs.pipreqs", str(directory), "--print"],
            capture_output=True,
            text=True,
            check=False
        )
        if result.returncode == 0:
            print("📦 Packages réellement nécessaires détectés dans le code :")
            print(result.stdout)
        else:
            print("⚠️ Remarque : Pour une analyse automatique approfondie des requirements,")
            print("   tu peux installer et exécuter 'deptry' ou 'pip-extra-reqs' :")
            print("   -> pip install deptry && deptry .\n")
            
    except Exception as e:
        print(f"⚠️ Impossible d'exécuter l'analyseur automatique : {e}")


def find_unused_imports_with_ruff() -> None:
    """Utilise ruff pour repérer et nettoyer les imports inutilisés dans le code Python."""
    try:
        print("🛠️ Nettoyage des 'import' inutilisés dans les fichiers Python...")
        # Supprime directement les imports non utilisés dans les fichiers .py
        subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--select", "F401", "--fix", "."],
            check=False
        )
    except Exception:
        pass


if __name__ == "__main__":
    project_root = Path(".")

    # 1. Vérification de la syntaxe
    syntax_ok = check_syntax(project_root)

    # 2. Nettoyage et détection des paquets/imports inutiles
    if syntax_ok:
        find_unused_imports_with_ruff()
        clean_unused_requirements(project_root)
    else:
        print("❌ Nettoyage annulé : corrige d'abord les erreurs de syntaxe.")
        sys.exit(1)