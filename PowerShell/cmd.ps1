# Encodage de la console en UTF-8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding           = [System.Text.Encoding]::UTF8

# Apparence de la console
$Host.UI.RawUI.BackgroundColor = "White"
$Host.UI.RawUI.ForegroundColor = "DarkGreen"
Clear-Host

# --- Configuration & Variables ---
$globalEnv   = "D:\Projets\Python\global-env"
$pyExe       = "$globalEnv\Scripts\python.exe"
$pyPath      = "D:\Projets\Python\repos\py1\Main.py"
$pyVPath      = "D:\Projets\Python\repos\py1\CVideoApp.py"
$isStreaming = $false

# Active l'optimisation -OO pour Python via la variable d'environnement
$env:PYTHONOPTIMIZE = 2
	
# Arguments transmis au script Python Main.py
$pythonCommonArgs = @(
    "--mediaType", "Video",
    "--actionType", "ReadAndCheck"
)

# --- Exécution ---
if ($isStreaming) {
    $streamingScript = "streaming_audio.ps1"
    $inputFile       = "${env:USERPROFILE}\Videos\New\Windows\video\La norme d'un vecteur.mp4"
    $outputFile      = "D:\Result\Subtitle\La norme d'un vecteur.csv"
    # Utilisation des paramètres génériques :
    # -ExecutorExe   : le binaire local (python.exe)
    # -ExecutorFlags : le flag d'optimisation (-OO) placé AVANT le script .py
    # -ScriptPath    : le chemin du script Main.py
    # -AppArgs       : la liste des arguments transmis à Main.py
	
    & $streamingScript -InputFile $inputFile -OutputFile $outputFile -ExecutorExe $pyExe -ExecutorFlags "-OO" -ScriptPath $pyPath -AppArgs $pythonCommonArgs
} else {
    uv run --python $globalEnv $pyPath @pythonCommonArgs
	# 2. Exécution de l'application vidéo seule (CVideoApp.py)
	# uv run --python $globalEnv $pyVPath
}

# --- Section .NET ---
# Publication et exécution si la publication réussit 
# $slnPath        = "D:\Projets\PokerNet\PokerNet.slnx"
# $publicationPath    = "D:\Projets\PokerNet\PokerOnWeb\bin\Release\net10.0\publish"
# $exePath       = "D:\Projets\PokerNet\PokerOnWeb\bin\Release\net10.0\publish\PokerOnWeb.exe"
# $pyPath        = "D:\Projets\Python\repos\PokerPy\Poker.py"

# $Host.UI.RawUI.WindowTitle = "Poker calculations"
# 1. On compile (on ne met pas les URLs ici, publish ne lance pas l'app)
# dotnet publish $slnPath -c Release -o $publicationPath
# 2. On ouvre le pare-feu (à ne faire qu'une seule fois en Admin)
# New-NetFirewallRule -DisplayName "Mon API .NET Poker" -Direction Inbound -LocalPort 5000 -Protocol TCP -Action Allow -ErrorAction SilentlyContinue
# 3. On LANÇANT l'exécutable, on lui passe l'argument de l'URL
# & $exePath --urls "http://0.0.0.0:5000"
# & $pyExePath $pyPath

# ---------------------

# Nettoyage en profondeur des fichiers système et composants obsolètes WinSxS & vidage automatique des caches temporaires
# .\Dism.exe /online /Cleanup-Image /StartComponentCleanup /ResetBase
# Remove-Item -Path "$env:TEMP\*" -Recurse -Force -ErrorAction SilentlyContinue

# C:\Windows\System32\robocopy.exe "C:\Users\SEDOUD\Videos\Farid" "F:\" *.mp4 /J /E /REG

# C:\Windows\System32\robocopy.exe "E:\Video\FR\Luc Besson" "F:\" *.mp4 *.srt /J /E /REG
# C:\Windows\System32\robocopy.exe "E:\Video\FR\Gérard Lanvin" "F:\" *.mp4 *.srt /J /E /REG
# C:\Windows\System32\robocopy.exe "E:\Video\FR\Gérard Depardieu" "F:\" *.mp4 *.srt /J /E /REG
# C:\Windows\System32\robocopy.exe "E:\Video\FR\Olivier Marchal" "F:\" *.mp4 *.srt /J /E /REG
# C:\Windows\System32\robocopy.exe "E:\Video\FR\Jacques Audiard" "F:\" *.mp4 *.srt /J /E /REG
# C:\Windows\System32\robocopy.exe "E:\Video\FR\Jean-Pierre Bacri" "F:\" *.mp4 *.srt /J /E /REG
# C:\Windows\System32\robocopy.exe "E:\Video\FR\Fernandel" "F:\" *.mp4 *.srt /J /E /REG
# C:\Windows\System32\robocopy.exe "E:\Video\FR\Dieudonne" "F:\" *.mp4 *.srt /J /E /REG
# C:\Windows\System32\robocopy.exe "E:\Video\FR\Other" "F:\" *.mp4 *.srt /J /E /REG

# Équivalent du pause 
Read-Host "`nAppuyez sur Entrée pour continuer..."