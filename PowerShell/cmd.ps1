# Encodage de la console en UTF-8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding           = [System.Text.Encoding]::UTF8

# Apparence de la console
$Host.UI.RawUI.BackgroundColor = "White"
$Host.UI.RawUI.ForegroundColor = "DarkGreen"
Clear-Host

# --- Section Python ---
# --- Configuration & Variables ---
$globalEnv   = "D:\Projets\Python\global-env"
# $pyExe       = "$globalEnv\Scripts\python.exe"
$pyPath      = "D:\Projets\Python\repos\py1\Main.py"
$pyVPath      = "D:\Projets\Python\repos\py1\CVideoApp.py"
$isStreaming = $false

# Active l'optimisation -OO pour Python via la variable d'environnement
$env:PYTHONOPTIMIZE = 2
	
# --- Exécution ---
if ($isStreaming) {
    $inputFile       = "${env:USERPROFILE}\Videos\New\Windows\video\La norme d'un vecteur.mp4"
    $outputFile      = "D:\Result\Video\video_check_frames.csv"
	$pythonCommonArgs = @("--mediaType","Video","--actionType","ReadAndCheck")
	
    ffmpeg -nostdin -loglevel error -i $inputFile -map 0:v:0 -c copy -f matroska pipe:1 | uv run --python $globalEnv $pyPath @pythonCommonArgs > $outputFile
	
} else {
	$pythonCommonArgs = @("--mediaType","Video","--actionType","Read")

    uv run --python $globalEnv $pyPath @pythonCommonArgs
	# 2. Exécution de l'application vidéo seule (CVideoApp.py)
	# uv run --python $globalEnv $pyVPath
}

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


# Get-ChildItem "D:\Result\Video\check_frames" -Filter *.csv | ForEach-Object { $f = $_.FullName; Import-Csv -LiteralPath $f -Delimiter ';' -Encoding UTF8 | Select-Object @{n='path'; e={$f}}, * } | Export-Csv "D:\Result\Video\check_frames_all.csv" -Delimiter ';' -NoTypeInformation -Encoding utf8BOM

#uv pip install --upgrade -r requirements.txt

#python -m pip check

# Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned; & D:\Projets\Python\global-env\Scripts\Activate.ps1

# $linkDir = "D:\Result\Music\Input"
# New-Item -ItemType Directory -Force -Path $linkDir | Out-Null

# Exemple : à partir de ton CSV (colonne "path")
# Import-Csv -LiteralPath "D:\Result\Music\musicResult.csv" -Delimiter ';' | Where-Object { $_.path } | ForEach-Object {
    # $link = Join-Path $linkDir (Split-Path $_.path -Leaf)
    # if (-not (Test-Path -LiteralPath $link)) {
        # New-Item -ItemType SymbolicLink -Path $link -Target $_.path | Out-Null
    # }
# }

# Équivalent du pause 
Read-Host "`nAppuyez sur Entrée pour continuer..."
