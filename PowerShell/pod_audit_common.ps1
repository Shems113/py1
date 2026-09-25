<#
.SYNOPSIS
    Fonctions communes à get_run_pod_info.ps1 et get_vast_pod_info.ps1 (mode Audit).
    Ce fichier est dot-sourcé ; il doit se trouver dans le même dossier que ces deux scripts.

.DESCRIPTION
    - Invoke-PodAudit   : exécute un script bash d'audit sur le pod (via SSH) et renvoie une hashtable CLE=VALEUR.
    - Measure-PodLink   : mesure la latence et le débit réel PC <-> pod (SCP), c.-à-d. le goulot du streaming.
    - Get-PodVerdict    : interprète l'audit (AV1 NVENC utilisable ? rapide ? contournement nécessaire ?).
    - Write-PodReport   : affiche le rapport complet.
#>

# ---------------------------------------------------------------------------------------------
# Script exécuté sur le pod. Sortie : une ligne CLE=VALEUR par information.
# Variables : NETTEST, BENCH, INSTALL, FIXCOMPAT (0/1).
# ---------------------------------------------------------------------------------------------
$script:PodAuditBash = @'
#!/usr/bin/env bash
export LC_ALL=C DEBIAN_FRONTEND=noninteractive
NETTEST="${NETTEST:-1}"; BENCH="${BENCH:-1}"; INSTALL="${INSTALL:-1}"; FIXCOMPAT="${FIXCOMPAT:-0}"

kv()    { printf '%s=%s\n' "$1" "$(printf '%s' "$2" | tr '\r\n\t' '   ' | sed 's/  */ /g;s/^ //;s/ $//')"; }
have()  { command -v "$1" >/dev/null 2>&1; }
isnum() { case "$1" in ''|*[!0-9.]*) return 1;; *) return 0;; esac; }
jget()  { printf '%s' "$1" | grep -o "\"$2\": *\"[^\"]*\"" | head -1 | cut -d'"' -f4; }
fetch() { if have curl; then curl -fsSL "$1"; elif have wget; then wget -qO- "$1"; else return 1; fi; }
gq()    { nvidia-smi -i 0 --query-gpu="$1" --format=csv,noheader,nounits 2>/dev/null | head -1 | sed 's/^ *//;s/ *$//'; }

kv SSH OK

# ================= Système
[ -r /etc/os-release ] && . /etc/os-release
kv OS "${PRETTY_NAME:-inconnu}"
kv KERNEL "$(uname -r)"

# ================= CPU
kv CPU_MODEL "$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2-)"
kv CPU_HOST_THREADS "$(grep -c '^processor' /proc/cpuinfo)"
kv CPU_NPROC "$(nproc 2>/dev/null)"
quota=""
if [ -r /sys/fs/cgroup/cpu.max ]; then
  read -r qa qb < /sys/fs/cgroup/cpu.max
  [ "$qa" != "max" ] && quota=$(awk -v a="$qa" -v b="$qb" 'BEGIN{printf "%.1f", a/b}')
elif [ -r /sys/fs/cgroup/cpu/cpu.cfs_quota_us ]; then
  qa=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us); qb=$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us)
  [ "${qa:-0}" -gt 0 ] 2>/dev/null && quota=$(awk -v a="$qa" -v b="$qb" 'BEGIN{printf "%.1f", a/b}')
fi
kv CPU_QUOTA "${quota:-aucun}"
kv CPU_AVX512 "$(grep -qm1 avx512f /proc/cpuinfo && echo oui || echo non)"
kv CPU_LOAD "$(cut -d' ' -f1-3 /proc/loadavg)"

# ================= Mémoire / disque
kv RAM_HOST_GB "$(awk '/^MemTotal/{printf "%.0f", $2/1048576}' /proc/meminfo)"
lim=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null)
if isnum "$lim" && [ "${#lim}" -lt 16 ]; then kv RAM_LIMIT_GB "$(awk -v l="$lim" 'BEGIN{printf "%.0f", l/1073741824}')"; else kv RAM_LIMIT_GB "aucune"; fi

WORK=/workspace; { [ -d "$WORK" ] && [ -w "$WORK" ]; } || WORK=/tmp
kv WORKDIR "$WORK"
kv DISK_ROOT "$(df -hP / | awk 'NR==2{print $4" libres / "$2}')"
[ -d /workspace ] && kv DISK_WORKSPACE "$(df -hTP /workspace | awk 'NR==2{print $5" libres / "$3" ("$2")"}')"
kv DISK_WRITE "$(dd if=/dev/zero of="$WORK/.pod_audit_dd" bs=1M count=256 conv=fdatasync 2>&1 | tail -1 | awk -F', ' '{print $NF}')"
rm -f "$WORK/.pod_audit_dd"

# ================= GPU
HOSTLIB=""; AV1="NOTEST"
if have nvidia-smi && nvidia-smi -L >/dev/null 2>&1; then
  kv GPU_PRESENT oui
  G_NAME=$(gq name); G_DRV=$(gq driver_version); G_CC=$(gq compute_cap)
  kv GPU_COUNT "$(nvidia-smi -L | grep -c '^GPU')"
  kv GPU_NAME "$G_NAME"
  kv GPU_DRIVER "$G_DRV"
  kv GPU_CUDA_MAX "$(nvidia-smi 2>/dev/null | grep -o 'CUDA Version: [0-9.]*' | awk '{print $3}')"
  kv GPU_CC "$G_CC"
  kv GPU_VRAM_MB "$(gq memory.total)"
  kv GPU_VRAM_USED_MB "$(gq memory.used)"
  kv GPU_PCIE "Gen$(gq pcie.link.gen.max) x$(gq pcie.link.width.max) (actuel Gen$(gq pcie.link.gen.current) x$(gq pcie.link.width.current))"
  kv GPU_POWER_LIMIT_W "$(gq power.limit)"
  kv GPU_CLOCK_MAX_MHZ "$(gq clocks.max.graphics)"
  kv GPU_TEMP_C "$(gq temperature.gpu)"
  kv GPU_UTIL "$(gq utilization.gpu)"
  kv GPU_ENC_SESSIONS "$(gq encoder.stats.sessionCount)"
  kv GPU_DEVNODES "$(ls /dev/nvidia[0-9]* 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ')"

  # Génération : AV1 NVENC = Ada (cc 8.9) ou Blackwell grand public (cc >= 12)
  ARCH=inconnu
  case "$G_NAME" in *A100*|*A800*|*A30*|*H100*|*H200*|*H800*|*V100*|*P100*|*T4*) ARCH=non;; esac
  if [ "$ARCH" = inconnu ] && isnum "$G_CC"; then
    ARCH=$(awk -v c="$G_CC" 'BEGIN{ if (c==8.9 || c>=12.0) print "oui"; else if (c<8.9 || c==9.0) print "non"; else print "inconnu" }')
  fi
  kv GPU_ARCH_AV1 "$ARCH"

  # Module UVM (indispensable à cuInit)
  if [ -e /dev/nvidia-uvm ]; then ( : <> /dev/nvidia-uvm ) 2>/dev/null && UVM=OK || UVM=KO; else UVM=ABSENT; fi
  kv UVM "$UVM"

  # libcuda : celle de l'hôte (même version que le driver) vs paquet "compat" de l'image
  for f in /usr/lib/x86_64-linux-gnu/libcuda.so."$G_DRV" /lib/x86_64-linux-gnu/libcuda.so."$G_DRV" /usr/lib64/libcuda.so."$G_DRV"; do
    [ -f "$f" ] && { HOSTLIB=$f; break; }
  done
  kv LIBCUDA_HOST "${HOSTLIB:-introuvable}"
  COMPAT=""
  for d in /usr/local/cuda*/compat; do [ -e "$d/libcuda.so.1" ] && COMPAT="$COMPAT $(readlink -f "$d/libcuda.so.1")"; done
  COMPAT=$(printf '%s\n' $COMPAT | sort -u | tr '\n' ' ')
  kv LIBCUDA_COMPAT "${COMPAT:-aucune}"

  if [ "$FIXCOMPAT" = 1 ] && [ -n "$COMPAT" ]; then
    for d in /usr/local/cuda*/compat; do [ -d "$d" ] && mv "$d" "$d.off" 2>/dev/null; done
    for f in $(grep -l compat /etc/ld.so.conf.d/*.conf 2>/dev/null); do mv "$f" "$f.off"; done
    ldconfig 2>/dev/null
    kv COMPAT_FIXED oui
  fi

  cuda_test() {
    LD_PRELOAD="$1" python3 - <<'PY' 2>/dev/null
import ctypes
try:
    c = ctypes.CDLL("libcuda.so.1")
except OSError:
    print("LOADFAIL -"); raise SystemExit
lib = [l.split()[-1] for l in open("/proc/self/maps") if "libcuda.so" in l]
print(c.cuInit(0), lib[0] if lib else "?")
PY
  }
  if have python3; then
    read -r CI_RC CI_LIB <<< "$(cuda_test "")"
    kv CUINIT "${CI_RC:-?}"
    kv LIBCUDA_LOADED "${CI_LIB:-?}"
    if [ "$CI_RC" != "0" ] && [ -n "$HOSTLIB" ]; then
      read -r CP_RC CP_LIB <<< "$(cuda_test "$HOSTLIB")"
      kv CUINIT_PRELOAD "${CP_RC:-?}"
    fi
  else
    kv CUINIT "python3 absent"
  fi
else
  kv GPU_PRESENT non
fi

# ================= ffmpeg
ff_av1() { "$1" -hide_banner -encoders 2>/dev/null | grep -q ' av1_nvenc '; }
FF=$(command -v ffmpeg 2>/dev/null)
if [ "$INSTALL" = 1 ] && { [ -z "$FF" ] || ! ff_av1 "$FF"; }; then
  if [ -z "$FF" ] && have apt-get; then
    (apt-get update -qq && apt-get install -y -qq ffmpeg) >/dev/null 2>&1
    FF=$(command -v ffmpeg 2>/dev/null); [ -n "$FF" ] && kv FFMPEG_INSTALLED "apt"
  fi
  if [ -z "$FF" ] || ! ff_av1 "$FF"; then
    mkdir -p /opt/ffmpeg
    fetch "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz" \
      | tar -xJ -C /opt/ffmpeg --strip-components=1 2>/dev/null
    if [ -x /opt/ffmpeg/bin/ffmpeg ]; then
      ln -sf /opt/ffmpeg/bin/ffmpeg /usr/local/bin/ffmpeg; ln -sf /opt/ffmpeg/bin/ffprobe /usr/local/bin/ffprobe
      FF=/usr/local/bin/ffmpeg; kv FFMPEG_INSTALLED "statique BtbN (/opt/ffmpeg)"
    fi
  fi
fi
kv FFMPEG "${FF:-absent}"
kv FFPROBE "$(command -v ffprobe 2>/dev/null || echo absent)"

if [ -n "$FF" ]; then
  kv FFMPEG_VERSION "$("$FF" -hide_banner -version 2>/dev/null | head -1 | awk '{print $3}')"
  ENCS=$("$FF" -hide_banner -encoders 2>/dev/null)
  for e in av1_nvenc hevc_nvenc libsvtav1; do
    printf '%s\n' "$ENCS" | grep -q " $e " && kv "ENC_$e" oui || kv "ENC_$e" non
  done
  "$FF" -hide_banner -hwaccels 2>/dev/null | grep -qx cuda && kv HWACCEL_CUDA oui || kv HWACCEL_CUDA non

  if [ "$(printf '%s\n' "$ENCS" | grep -c ' av1_nvenc ')" -gt 0 ] && [ -n "$G_NAME" ]; then
    av1_try() {
      LD_PRELOAD="$1" timeout 60 "$FF" -hide_banner -nostdin -loglevel error \
        -f lavfi -i "testsrc2=s=1280x720:r=30:d=1" $2 -c:v av1_nvenc -f null - 2>&1
    }
    errline() { printf '%s\n' "$1" | grep -iE 'error|fail|cannot|unsupported|capable|driver' | head -2; }
    OUT=$(av1_try "" ""); RC=$?
    if [ $RC -eq 0 ]; then
      AV1=NATIVE
    else
      kv AV1_ERR_NATIVE "$(errline "$OUT")"
      AV1=KO
      if [ -n "$HOSTLIB" ]; then
        OUT2=$(av1_try "$HOSTLIB" ""); RC2=$?
        if [ $RC2 -eq 0 ]; then AV1=PRELOAD; else kv AV1_ERR_PRELOAD "$(errline "$OUT2")"; fi
      fi
    fi
    PRE=""; [ "$AV1" = PRELOAD ] && PRE="$HOSTLIB"
    if [ "$AV1" = NATIVE ] || [ "$AV1" = PRELOAD ]; then
      av1_try "$PRE" "-vf format=p010le" >/dev/null 2>&1 && kv AV1_10BIT oui || kv AV1_10BIT non
    fi
  elif [ -n "$G_NAME" ]; then
    AV1=NOENC
  fi
fi
kv AV1_TEST "$AV1"

# ================= Benchmark : source 4K HEVC 10 bits -> AV1 NVENC (pipeline tout GPU)
if [ "$BENCH" = 1 ] && { [ "$AV1" = NATIVE ] || [ "$AV1" = PRELOAD ]; }; then
  SRC="$WORK/.pod_audit_src.mkv"
  LD_PRELOAD="$PRE" "$FF" -hide_banner -nostdin -loglevel error -y -f lavfi -i "testsrc2=s=3840x2160:r=24:d=2" \
    -vf format=p010le -c:v hevc_nvenc -profile:v main10 -preset p1 "$SRC" 2>/dev/null
  if [ -s "$SRC" ]; then
    LOOPS=9; FRAMES=$(( (LOOPS + 1) * 48 ))
    bench() {
      t0=$(date +%s.%N)
      LD_PRELOAD="$PRE" timeout 300 "$FF" -hide_banner -nostdin -loglevel error -stream_loop $LOOPS $1 -i "$SRC" \
        -c:v av1_nvenc -preset p5 -tune hq -rc vbr -cq 28 -b:v 0 -f null - >/dev/null 2>&1 || return 1
      t1=$(date +%s.%N)
      awk -v f="$FRAMES" -v a="$t0" -v b="$t1" 'BEGIN{printf "%.0f", f/(b-a)}'
    }
    if FPS=$(bench "-hwaccel cuda -hwaccel_output_format cuda"); then
      kv BENCH_4K_FPS "$FPS"; kv BENCH_PIPELINE "NVDEC -> NVENC (tout GPU)"
    elif FPS=$(bench ""); then
      kv BENCH_4K_FPS "$FPS"; kv BENCH_PIPELINE "décodage CPU -> NVENC"
    else
      kv BENCH_4K_FPS "échec"
    fi
  else
    kv BENCH_4K_FPS "échec (création de la source HEVC)"
  fi
  rm -f "$SRC"
fi

# ================= Réseau du pod (vers Internet)
if [ "$NETTEST" = 1 ] && have curl; then
  INFO=$(curl -s --max-time 5 https://ipinfo.io/json)
  kv NET_PUBLIC_IP "$(jget "$INFO" ip)"
  kv NET_LOCATION "$(jget "$INFO" city), $(jget "$INFO" region), $(jget "$INFO" country)"
  kv NET_ORG "$(jget "$INFO" org)"
  D=$(curl -s -o /dev/null --max-time 12 -w '%{speed_download}' 'https://speed.cloudflare.com/__down?bytes=300000000')
  kv NET_DOWN_MBPS "$(awk -v s="${D:-0}" 'BEGIN{printf "%.0f", s*8/1e6}')"
  U=$(head -c 40000000 /dev/urandom | curl -s -o /dev/null --max-time 12 -w '%{speed_upload}' \
      -X POST -H 'Content-Type: application/octet-stream' --data-binary @- 'https://speed.cloudflare.com/__up')
  kv NET_UP_MBPS "$(awk -v s="${U:-0}" 'BEGIN{printf "%.0f", s*8/1e6}')"
fi

kv AUDIT_END OK
'@

# ---------------------------------------------------------------------------------------------
function Get-PodSshOptions {
    param ([string]$Port, [string]$KeyPath, [switch]$Scp)
    $portFlag = if ($Scp) { "-P" } else { "-p" }
    $opts = @(
        $portFlag, $Port, "-i", $KeyPath,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10"
    )
    if (-not $Scp) { $opts = @("-T") + $opts }
    return $opts
}

function Invoke-PodAudit {
    param (
        [string]$PodIP, [string]$PodPort,
        [string]$SSHUser, [string]$KeyPath, [string]$SSHExe,
        [bool]$NetTest = $true, [bool]$Bench = $true, [bool]$Install = $true, [bool]$FixCompat = $false
    )
    $bash   = $script:PodAuditBash -replace "`r", ""
    $b64    = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($bash))
    $envs   = "NETTEST=$([int]$NetTest) BENCH=$([int]$Bench) INSTALL=$([int]$Install) FIXCOMPAT=$([int]$FixCompat)"
    $remote = "echo $b64 | base64 -d > /tmp/pod_audit.sh && $envs bash /tmp/pod_audit.sh"

    $opts = Get-PodSshOptions -Port $PodPort -KeyPath $KeyPath
    $raw  = & $SSHExe @opts "${SSHUser}@${PodIP}" $remote 2>&1

    $h = @{}
    foreach ($line in $raw) {
        if ("$line" -match '^([A-Za-z0-9_]+)=(.*)$') { $h[$Matches[1]] = $Matches[2].Trim() }
    }
    if ($h['SSH'] -ne 'OK') { $h['SSH_ERROR'] = (($raw | Out-String).Trim()) }
    return $h
}

function Measure-PodLink {
    param (
        [string]$PodIP, [string]$PodPort,
        [string]$SSHUser, [string]$KeyPath, [string]$SSHExe,
        [int]$SizeMB = 64
    )
    $res = [ordered]@{ LatencyMs = $null; UpMbps = $null; DownMbps = $null; Error = $null }

    # Latence : temps d'ouverture TCP vers le port SSH (l'ICMP est souvent filtré)
    $samples = foreach ($i in 1..4) {
        $c = [System.Net.Sockets.TcpClient]::new()
        try {
            $sw = [Diagnostics.Stopwatch]::StartNew()
            if ($c.ConnectAsync($PodIP, [int]$PodPort).Wait(3000)) { $sw.Elapsed.TotalMilliseconds }
        } catch { } finally { $c.Dispose() }
    }
    if ($samples) { $res.LatencyMs = [math]::Round(($samples | Measure-Object -Minimum).Minimum) }

    $scpExe = Join-Path (Split-Path $SSHExe) "scp.exe"
    if (-not (Test-Path -LiteralPath $scpExe)) { $res.Error = "scp.exe introuvable"; return $res }

    $sshOpts = Get-PodSshOptions -Port $PodPort -KeyPath $KeyPath
    $scpOpts = @("-q") + (Get-PodSshOptions -Port $PodPort -KeyPath $KeyPath -Scp)
    $target  = "${SSHUser}@${PodIP}"
    $name    = "pod_link_test_$PID.bin"

    Push-Location ([IO.Path]::GetTempPath())   # chemins relatifs : évite que scp prenne "C:" pour un hôte
    try {
        $buf = [byte[]]::new(1MB); $rng = [Random]::new()
        $fs  = [IO.File]::Create((Join-Path (Get-Location) $name))
        try { for ($i = 0; $i -lt $SizeMB; $i++) { $rng.NextBytes($buf); $fs.Write($buf, 0, $buf.Length) } }
        finally { $fs.Close() }

        # Coût fixe d'une connexion (handshake), retranché des transferts
        $sw = [Diagnostics.Stopwatch]::StartNew()
        & $SSHExe @sshOpts $target "true" 2>$null | Out-Null
        $handshake = $sw.Elapsed.TotalSeconds

        $mbits = $SizeMB * 8 * 1.048576

        $sw.Restart()
        & $scpExe @scpOpts $name "${target}:/tmp/$name" 2>$null
        $t = [math]::Max($sw.Elapsed.TotalSeconds - $handshake, 0.05)
        if ($LASTEXITCODE -eq 0) { $res.UpMbps = [math]::Round($mbits / $t) }

        $sw.Restart()
        & $scpExe @scpOpts "${target}:/tmp/$name" "$name.down" 2>$null
        $t = [math]::Max($sw.Elapsed.TotalSeconds - $handshake, 0.05)
        if ($LASTEXITCODE -eq 0) { $res.DownMbps = [math]::Round($mbits / $t) }

        & $SSHExe @sshOpts $target "rm -f /tmp/$name" 2>$null | Out-Null
    }
    catch { $res.Error = $_.Exception.Message }
    finally {
        Remove-Item -LiteralPath $name, "$name.down" -ErrorAction SilentlyContinue
        Pop-Location
    }
    return $res
}

# ---------------------------------------------------------------------------------------------
function Get-CuInitLabel {
    param ([string]$Code)
    switch ($Code) {
        '0'   { 'OK' }
        '100' { 'CUDA_ERROR_NO_DEVICE' }
        '35'  { 'CUDA_ERROR_INSUFFICIENT_DRIVER' }
        '803' { 'CUDA_ERROR_SYSTEM_DRIVER_MISMATCH' }
        '804' { 'CUDA_ERROR_COMPAT_NOT_SUPPORTED_ON_DEVICE' }
        '999' { 'CUDA_ERROR_UNKNOWN' }
        default { if ($Code) { "code $Code" } else { 'non testé' } }
    }
}

function Get-PodVerdict {
    param ([hashtable]$A)

    $notes   = [System.Collections.Generic.List[string]]::new()
    $actions = [System.Collections.Generic.List[string]]::new()
    $level   = 'KO'; $summary = ''; $av1Ok = $false; $preload = $false
    $gpu     = $A['GPU_NAME']

    if ($A['SSH'] -ne 'OK') {
        $summary = "Audit impossible : connexion SSH refusée ou coupée."
        $actions.Add("Vérifier que la clé publique est dans ~/.ssh/authorized_keys du pod (ou dans les paramètres du compte).")
    }
    elseif ($A['GPU_PRESENT'] -ne 'oui') {
        $summary = "Aucun GPU NVIDIA visible dans le conteneur."
    }
    elseif ($A['GPU_ARCH_AV1'] -eq 'non') {
        $summary = "$gpu n'a pas d'encodeur AV1 matériel (il faut une génération Ada ou plus récente)."
        $actions.Add("Choisir une RTX 4090, RTX 5000 Ada, L4, L40S ou RTX 50xx.")
    }
    elseif ($A['UVM'] -eq 'KO') {
        $summary = "Module nvidia-uvm défectueux sur l'hôte (EIO) : CUDA et NVENC ne peuvent pas démarrer."
        $actions.Add("Défaut de l'hôte, non réparable depuis le conteneur : changer de pod.")
    }
    else {
        switch ($A['AV1_TEST']) {
            'NATIVE' {
                $level = 'OK'; $av1Ok = $true
                $summary = "AV1 NVENC fonctionnel nativement."
            }
            'PRELOAD' {
                $level = 'WARN'; $av1Ok = $true; $preload = $true
                $summary = "AV1 NVENC fonctionnel, mais seulement en forçant la libcuda de l'hôte."
                $notes.Add("La libcuda 'compat' de l'image ($("$($A['LIBCUDA_COMPAT'])".Trim())) est chargée à la place de celle du driver $($A['GPU_DRIVER']) ; elle n'est pas supportée sur GeForce.")
                $actions.Add("Relancer l'audit avec -FixCompat (neutralise le dossier compat), ou préfixer ffmpeg par LD_PRELOAD=$($A['LIBCUDA_HOST']).")
            }
            'NOENC' {
                $level = 'WARN'
                $summary = "ffmpeg $($A['FFMPEG_VERSION']) ne propose pas l'encodeur av1_nvenc."
                $actions.Add("Relancer l'audit sans -NoFfmpegInstall (installe un ffmpeg statique BtbN avec NVENC).")
            }
            'KO' {
                $err = "$($A['AV1_ERR_NATIVE']) $($A['AV1_ERR_PRELOAD'])"
                if ($err -match 'API version|minimum required') {
                    $summary = "Le driver de l'hôte ($($A['GPU_DRIVER'])) est trop ancien pour ce ffmpeg."
                    $actions.Add("Utiliser un ffmpeg plus ancien (apt) ou un hôte au driver plus récent.")
                } elseif ($err -match 'libnvidia-encode|Cannot load') {
                    $summary = "La bibliothèque NVENC (libnvidia-encode) n'est pas montée dans le conteneur."
                    $actions.Add("Redéployer avec la variable d'environnement NVIDIA_DRIVER_CAPABILITIES=all.")
                } elseif ($err -match 'unsupported device|No capable devices') {
                    $summary = "Le GPU refuse l'encodage AV1 (unsupported device)."
                } elseif ($err -match 'cuInit|CUDA_ERROR') {
                    $summary = "CUDA ne s'initialise pas : cuInit = $(Get-CuInitLabel $A['CUINIT'])."
                    $actions.Add("Changer de pod (hôte défaillant).")
                } elseif ($err -match 'OpenEncodeSessionEx|out of memory|client key') {
                    $summary = "Sessions NVENC refusées ou saturées par l'hôte."
                } else {
                    $summary = "Échec de l'encodage AV1 NVENC : $($err.Trim())"
                }
            }
            default {
                $level = 'WARN'
                $summary = "ffmpeg absent : encodage non testé (cuInit = $(Get-CuInitLabel $A['CUINIT']))."
                $actions.Add("Relancer l'audit sans -NoFfmpegInstall.")
            }
        }
    }

    # Performances
    $fps = 0
    if ([int]::TryParse("$($A['BENCH_4K_FPS'])", [ref]$fps) -and $fps -gt 0) {
        $rating = if ($fps -ge 120) { "excellent" } elseif ($fps -ge 60) { "bon" } elseif ($fps -ge 24) { "correct" } else { "lent" }
        $film   = [math]::Round(172800 / $fps / 60)   # film de 2 h à 24 i/s
        $notes.Add("Vitesse 4K 10 bits -> AV1 : $fps i/s ($rating), soit ~$film min pour un film de 2 h (hors transfert).")
        if ($A['BENCH_PIPELINE'] -notmatch 'NVDEC') { $notes.Add("Décodage matériel (NVDEC) indisponible : le CPU décode, ce qui peut limiter la vitesse.") }
    }

    if ($av1Ok -and $A['AV1_10BIT'] -eq 'non') { $notes.Add("L'encodage AV1 10 bits a échoué : les sources HDR seront un problème.") }
    if ($A['GPU_DEVNODES'] -and $A['GPU_DEVNODES'] -notmatch '\bnvidia0\b') { $notes.Add("Hôte multi-GPU (périphérique visible : $($A['GPU_DEVNODES'].Trim())).") }
    if ($A['LIBCUDA_COMPAT'] -and $A['LIBCUDA_COMPAT'] -ne 'aucune' -and $A['AV1_TEST'] -eq 'NATIVE') {
        $notes.Add("Une libcuda compat est présente mais n'a pas gêné l'encodage.")
    }
    if (-not $av1Ok -and $A['ENC_libsvtav1'] -eq 'oui') {
        $notes.Add("Repli possible : encodage logiciel libsvtav1 sur $($A['CPU_NPROC']) threads CPU (AVX-512 : $($A['CPU_AVX512'])).")
    }
    if ($A['GPU_ENC_SESSIONS'] -and $A['GPU_ENC_SESSIONS'] -ne '0') { $notes.Add("$($A['GPU_ENC_SESSIONS']) session(s) NVENC déjà active(s) sur le GPU.") }

    return [pscustomobject]@{
        Level = $level; Summary = $summary; Av1Ok = $av1Ok; NeedsPreload = $preload
        Notes = @($notes); Actions = @($actions)
    }
}

# Affichage par défaut compact de l'objet renvoyé (la hashtable Audit reste accessible : $r.Audit)
function Set-PodResultDisplay {
    param ($Obj)
    $set = [System.Management.Automation.PSPropertySet]::new('DefaultDisplayPropertySet',
        [string[]]@('Provider', 'PodId', 'PodIP', 'PodPort', 'Av1EncodeOk', 'Av1NeedsPreload', 'Bench4kFps', 'Verdict'))
    $Obj.PSObject.Members.Add([System.Management.Automation.PSMemberSet]::new('PSStandardMembers',
        [System.Management.Automation.PSMemberInfo[]]@($set)))
    return $Obj
}

# ---------------------------------------------------------------------------------------------
function Write-PodSection { param ([string]$Title) Write-Host ""; Write-Host " $Title" -ForegroundColor Cyan }

function Write-PodRow {
    param ([string]$Label, $Value, [ConsoleColor]$Color = [ConsoleColor]::Gray)
    if ($null -eq $Value -or "$Value".Trim() -eq '' -or "$Value" -match '^\s*,\s*,\s*$') { return }
    Write-Host ("   {0,-24}: " -f $Label) -NoNewline -ForegroundColor DarkGray
    Write-Host "$Value" -ForegroundColor $Color
}

function Format-Mbps { param ($v) if ($null -ne $v -and "$v" -ne '' -and "$v" -ne '0') { "$v Mbit/s" } else { $null } }

function Write-PodReport {
    param (
        [string]$Title,
        [System.Collections.Specialized.OrderedDictionary]$ApiInfo,
        [hashtable]$Audit,
        $Link,
        $Verdict
    )
    $A = $Audit
    Write-Host ""
    Write-Host ("═" * 78) -ForegroundColor DarkCyan
    Write-Host " $Title" -ForegroundColor White
    Write-Host ("═" * 78) -ForegroundColor DarkCyan

    Write-PodSection "Offre et hôte (API)"
    foreach ($k in $ApiInfo.Keys) { Write-PodRow $k $ApiInfo[$k] }

    if ($Link) {
        Write-PodSection "Liaison PC <-> pod (mesurée)"
        Write-PodRow "Latence TCP" $(if ($Link.LatencyMs) { "$($Link.LatencyMs) ms" })
        Write-PodRow "Débit PC -> pod" (Format-Mbps $Link.UpMbps) Yellow
        Write-PodRow "Débit pod -> PC" (Format-Mbps $Link.DownMbps) Yellow
        Write-PodRow "Erreur" $Link.Error Red
    }

    if (-not $A -or $A['SSH'] -ne 'OK') {
        if ($A -and $A['SSH_ERROR']) { Write-PodRow "SSH" $A['SSH_ERROR'] Red }
    }
    else {
        Write-PodSection "Système"
        Write-PodRow "OS" $A['OS']
        Write-PodRow "Noyau" $A['KERNEL']

        Write-PodSection "CPU"
        Write-PodRow "Modèle" $A['CPU_MODEL']
        Write-PodRow "Threads hôte" $A['CPU_HOST_THREADS']
        Write-PodRow "Threads visibles (nproc)" $A['CPU_NPROC']
        Write-PodRow "Quota cgroup (vCPU)" $A['CPU_QUOTA']
        Write-PodRow "AVX-512" $A['CPU_AVX512']
        Write-PodRow "Charge (1/5/15 min)" $A['CPU_LOAD']

        Write-PodSection "Mémoire et disque"
        Write-PodRow "RAM hôte" $(if ($A['RAM_HOST_GB']) { "$($A['RAM_HOST_GB']) Go" })
        Write-PodRow "Limite RAM conteneur" $(if ($A['RAM_LIMIT_GB'] -match '^\d') { "$($A['RAM_LIMIT_GB']) Go" } else { $A['RAM_LIMIT_GB'] })
        Write-PodRow "Disque /" $A['DISK_ROOT']
        Write-PodRow "Disque /workspace" $A['DISK_WORKSPACE']
        Write-PodRow "Écriture ($($A['WORKDIR']))" $A['DISK_WRITE']

        Write-PodSection "GPU"
        if ($A['GPU_PRESENT'] -eq 'oui') {
            Write-PodRow "Modèle" "$($A['GPU_NAME']) (x$($A['GPU_COUNT']))" White
            Write-PodRow "VRAM" $(if ($A['GPU_VRAM_MB']) { "$([math]::Round([double]$A['GPU_VRAM_MB'] / 1024, 1)) Go ($($A['GPU_VRAM_USED_MB']) Mo utilisés)" })
            Write-PodRow "Driver / CUDA max" $(if ($A['GPU_DRIVER']) { "$($A['GPU_DRIVER'])$(if ($A['GPU_CUDA_MAX']) { " / CUDA $($A['GPU_CUDA_MAX'])" })" })
            Write-PodRow "Compute capability" $A['GPU_CC']
            Write-PodRow "PCIe" $A['GPU_PCIE']
            Write-PodRow "Limite de puissance" $(if ($A['GPU_POWER_LIMIT_W']) { "$($A['GPU_POWER_LIMIT_W']) W" })
            Write-PodRow "Fréquence max" $(if ($A['GPU_CLOCK_MAX_MHZ']) { "$($A['GPU_CLOCK_MAX_MHZ']) MHz" })
            Write-PodRow "Température / charge" $(if ($A['GPU_TEMP_C']) { "$($A['GPU_TEMP_C']) °C / $($A['GPU_UTIL']) %" })
            Write-PodRow "Périphériques visibles" $A['GPU_DEVNODES']
        } else {
            Write-PodRow "GPU" "aucun GPU visible" Red
        }

        Write-PodSection "Réseau du pod (Internet)"
        Write-PodRow "IP vue d'Internet" $A['NET_PUBLIC_IP']
        Write-PodRow "Localisation" $A['NET_LOCATION']
        Write-PodRow "Opérateur" $A['NET_ORG']
        Write-PodRow "Descendant" (Format-Mbps $A['NET_DOWN_MBPS'])
        Write-PodRow "Montant" (Format-Mbps $A['NET_UP_MBPS'])

        Write-PodSection "Chaîne d'encodage"
        $ok = { param ($v) if ($v -in 'oui', 'OK', 'NATIVE') { [ConsoleColor]::Green } elseif ($v -in 'non', 'KO', 'absent') { [ConsoleColor]::Red } else { [ConsoleColor]::Yellow } }
        Write-PodRow "AV1 matériel (génération)" $A['GPU_ARCH_AV1'] (& $ok $A['GPU_ARCH_AV1'])
        Write-PodRow "/dev/nvidia-uvm" $A['UVM'] (& $ok $A['UVM'])
        Write-PodRow "cuInit" (Get-CuInitLabel $A['CUINIT']) $(if ($A['CUINIT'] -eq '0') { 'Green' } else { 'Red' })
        if ($A['CUINIT_PRELOAD']) { Write-PodRow "cuInit (libcuda hôte)" (Get-CuInitLabel $A['CUINIT_PRELOAD']) $(if ($A['CUINIT_PRELOAD'] -eq '0') { 'Green' } else { 'Red' }) }
        Write-PodRow "libcuda chargée" $A['LIBCUDA_LOADED']
        Write-PodRow "libcuda de l'hôte" $A['LIBCUDA_HOST']
        Write-PodRow "libcuda compat (image)" $A['LIBCUDA_COMPAT']
        Write-PodRow "Correctif compat appliqué" $A['COMPAT_FIXED'] Green
        Write-PodRow "ffmpeg" "$($A['FFMPEG']) $($A['FFMPEG_VERSION'])"
        Write-PodRow "ffmpeg installé par l'audit" $A['FFMPEG_INSTALLED']
        Write-PodRow "ffprobe" $A['FFPROBE']
        Write-PodRow "Encodeur av1_nvenc" $A['ENC_av1_nvenc'] (& $ok $A['ENC_av1_nvenc'])
        Write-PodRow "Décodage NVDEC (cuda)" $A['HWACCEL_CUDA'] (& $ok $A['HWACCEL_CUDA'])
        Write-PodRow "Test AV1 NVENC" $A['AV1_TEST'] (& $ok $A['AV1_TEST'])
        Write-PodRow "AV1 10 bits" $A['AV1_10BIT'] (& $ok $A['AV1_10BIT'])
        Write-PodRow "Erreur (natif)" $A['AV1_ERR_NATIVE'] DarkYellow
        Write-PodRow "Erreur (libcuda hôte)" $A['AV1_ERR_PRELOAD'] DarkYellow
        Write-PodRow "Bench 4K 10b -> AV1" $(if ($A['BENCH_4K_FPS']) { "$($A['BENCH_4K_FPS']) i/s  [$($A['BENCH_PIPELINE'])]" }) White
        Write-PodRow "Repli CPU libsvtav1" $A['ENC_libsvtav1']
    }

    if ($Verdict) {
        Write-PodSection "Verdict"
        $color = switch ($Verdict.Level) { 'OK' { 'Green' } 'WARN' { 'Yellow' } default { 'Red' } }
        Write-Host "   [$($Verdict.Level)] $($Verdict.Summary)" -ForegroundColor $color
        foreach ($n in $Verdict.Notes)   { Write-Host "   • $n" -ForegroundColor Gray }
        foreach ($a in $Verdict.Actions) { Write-Host "   → $a" -ForegroundColor Cyan }
    }
    Write-Host ""
}