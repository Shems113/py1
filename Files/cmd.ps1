[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# Apparence de la console
$Host.UI.RawUI.BackgroundColor = "White"
$Host.UI.RawUI.ForegroundColor = "DarkGreen"
Clear-Host

# Variables de chemins et paramètres
$globalEnv  = "D:\Projets\Python\global-env"
$pyPath     = "D:\Projets\Python\repos\py\Main.py"
$pyVPath    = "D:\Projets\Python\repos\py\VideoData.py"
$jsonPath   = "D:\Projets\Python\repos\py\Files\Settings.json"

$mediaType  = "Video"
$actionType = "Read"
# $actionType2 = "Write", "ReadAndMove", "Read"

# Titre de la console
$Host.UI.RawUI.WindowTitle = "mediaType : $mediaType | actionType : $actionType"

# Exécution du script Python via uv avec l'environnement virtuel global
# uv run --python "$globalEnv\Scripts\python.exe" $pyPath --jsonPath $jsonPath --mediaType $mediaType --actionType $actionType
# uv run --python "$globalEnv\Scripts\python.exe" $pyPath --jsonPath $jsonPath --mediaType $mediaType --actionType $actionType2
uv run --python "$globalEnv\Scripts\python.exe" $pyVPath

# --- Section .NET ---
# Publication et exécution si la publication réussit 
# $slnPath    = "D:\Projets\PokerNet\PokerNet.slnx"
# $publicationPath    = "D:\Projets\PokerNet\PokerOnWeb\bin\Release\net10.0\publish"
# $exePath   = "D:\Projets\PokerNet\PokerOnWeb\bin\Release\net10.0\publish\PokerOnWeb.exe"
# $pyPath    = "D:\Projets\Python\repos\PokerPy\Poker.py"

# $Host.UI.RawUI.WindowTitle = "Poker calculations"
# 1. On compile (on ne met pas les URLs ici, publish ne lance pas l'app)
# dotnet publish $slnPath -c Release -o $publicationPath
# 2. On ouvre le pare-feu (à ne faire qu'une seule fois en Admin)
# New-NetFirewallRule -DisplayName "Mon API .NET Poker" -Direction Inbound -LocalPort 5000 -Protocol TCP -Action Allow -ErrorAction SilentlyContinue
# 3. On LANÇANT l'exécutable, on lui passe l'argument de l'URL
# & $exePath --urls "http://0.0.0.0:5000"
# & $pyExePath $pyPath

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
