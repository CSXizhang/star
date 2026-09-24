# Discovers Stardew Valley installation directory on Windows.
[CmdletBinding()]
param()

$candidates = [System.Collections.Generic.List[string]]::new()

# 1. Common Steam library locations
$commonPaths = @(
    "C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley",
    "C:\Program Files\Steam\steamapps\common\Stardew Valley",
    "D:\SteamLibrary\steamapps\common\Stardew Valley",
    "E:\SteamLibrary\steamapps\common\Stardew Valley",
    "F:\SteamLibrary\steamapps\common\Stardew Valley"
)
foreach ($p in $commonPaths) {
    if (Test-Path -LiteralPath $p) {
        if (-not $candidates.Contains($p)) { $candidates.Add($p) }
    }
}

# 2. Windows Registry - Steam App 413150 uninstall key
$regPaths = @(
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 413150",
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 413150"
)
foreach ($rp in $regPaths) {
    $prop = Get-ItemProperty -Path $rp -ErrorAction SilentlyContinue
    if ($prop -and $prop.InstallLocation -and (Test-Path -LiteralPath $prop.InstallLocation)) {
        if (-not $candidates.Contains($prop.InstallLocation)) {
            $candidates.Add($prop.InstallLocation)
        }
    }
}

# 3. Steam client config (libraryfolders.vdf)
$steamReg = Get-ItemProperty -Path "HKCU:\SOFTWARE\Valve\Steam" -ErrorAction SilentlyContinue
if ($steamReg -and $steamReg.SteamPath) {
    $vdfPath = Join-Path $steamReg.SteamPath "steamapps\libraryfolders.vdf"
    if (Test-Path -LiteralPath $vdfPath) {
        $content = Get-Content -LiteralPath $vdfPath -Raw -ErrorAction SilentlyContinue
        if ($content) {
            $matches = [regex]::Matches($content, '"path"\s+"([^"]+)"')
            foreach ($m in $matches) {
                $libPath = $m.Groups[1].Value -replace '\\\\', '\'
                $gamePath = Join-Path $libPath "steamapps\common\Stardew Valley"
                if ((Test-Path -LiteralPath $gamePath) -and (-not $candidates.Contains($gamePath))) {
                    $candidates.Add($gamePath)
                }
            }
        }
    }
}

# Return best candidate:
# Prioritize directory containing StardewModdingAPI.exe
foreach ($c in $candidates) {
    if (Test-Path -LiteralPath (Join-Path $c "StardewModdingAPI.exe")) {
        return (Resolve-Path -LiteralPath $c).Path
    }
}

# Otherwise return first candidate with Stardew Valley.exe
foreach ($c in $candidates) {
    if (Test-Path -LiteralPath (Join-Path $c "Stardew Valley.exe")) {
        return (Resolve-Path -LiteralPath $c).Path
    }
}

return $null
