#Requires -Version 5.1
<#
Pouze pravidlo pro pribaleny python\python.exe, TCP 48217, Private+Public,
vzdalene adresy LocalSubnet. Nikdy nevypina firewall ani nemeni cizi pravidla.
-Odebrat vrati tuto zmenu; data ani Python se nemazou.
Ruční zkouška na Windows 11:
  1. Spustit jako běžný uživatel, potvrdit UAC účtem správce.
  2. V wf.msc ověřit program, TCP 48217, Private+Public a LocalSubnet.
  3. Spustit podruhé: pro tuto cestu programu stále jediné pravidlo.
  4. Z telefonu ve stejné Wi-Fi otevřít hru; z jiné sítě pravidlo nepovoluje.
  5. Spustit -Odebrat dvakrát; odstraní jen toto pravidlo, opakování je úspěch.
  6. Vyzkoušet cestu s mezerami a odmítnutí UAC (nenulový návratový kód).
#>
[CmdletBinding()]
param([switch]$Odebrat)

$ErrorActionPreference = 'Stop'
try {
    if ([Environment]::OSVersion.Platform -ne 'Win32NT' -or $PSVersionTable.PSEdition -ne 'Desktop') {
        throw 'Spusťte tento soubor pomocí Windows PowerShell 5.1.'
    }
    $python = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'python\python.exe'))
    if (-not $Odebrat -and -not (Test-Path -LiteralPath $python -PathType Leaf)) {
        Write-Host 'CHYBA: Chybí přibalený python\python.exe. Spusťte skript z rozbaleného balíčku nebo instalace ClassRally.'
        exit 1
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host 'Windows požádá správce o jednorázové potvrzení změny firewallu.'
        $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath))
        if ($Odebrat) { $arguments += '-Odebrat' }
        $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        try {
            $process = Start-Process -FilePath $powershell -ArgumentList $arguments -Verb RunAs -Wait -PassThru
            exit $process.ExitCode
        } catch {
            Write-Host 'CHYBA: Správce nepotvrdil změnu nebo spuštění selhalo. Požádejte školní IT.'
            exit 1
        }
    }

    # Hash cele cesty oddeli soubezne instalace; nemenime pravidlo jine kopie.
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($python.ToLowerInvariant()))).Replace('-', '').Substring(0, 16)
    } finally { $sha.Dispose() }
    $ruleName = 'ClassRally-TCP-48217-' + $hash
    $existing = @(Get-NetFirewallRule -PolicyStore PersistentStore -Name $ruleName -ErrorAction SilentlyContinue)
    foreach ($rule in $existing) {
        if ($rule.Group -ne 'ClassRally') { throw 'Kolize názvu pravidla firewallu; požádejte IT o kontrolu.' }
    }
    if ($Odebrat) {
        if ($existing.Count -gt 0) {
            $existing | Remove-NetFirewallRule -ErrorAction Stop
        }
        Write-Host 'Pravidlo ClassRally pro tuto instalaci je odebrané (nebo už neexistovalo).'
        exit 0
    }
    $settings = @{
        Direction = 'Inbound'; Action = 'Allow'; Enabled = 'True'
        Profile = @('Private', 'Public'); Protocol = 'TCP'; LocalPort = 48217
        RemoteAddress = 'LocalSubnet'; Program = $python; EdgeTraversalPolicy = 'Block'
        LocalAddress = 'Any'; RemotePort = 'Any'; InterfaceType = 'Any'
    }
    if ($existing.Count -gt 0) {
        $existing | Set-NetFirewallRule @settings -ErrorAction Stop
    } else {
        $null = New-NetFirewallRule -PolicyStore PersistentStore -Name $ruleName -DisplayName 'ClassRally (TCP 48217)' -Group 'ClassRally' @settings -ErrorAction Stop
    }
    Write-Host 'Povoleno: jen přibalený Python, příchozí TCP 48217, místní podsíť, profily Soukromý a Veřejný.'
    Write-Host ('Program: {0}' -f $python)
    Write-Host 'Návrat: spusťte Odebrat-firewall.bat nebo tento skript s parametrem -Odebrat.'
    Write-Host 'Pokud přístup dál blokují pravidla školy nebo dřívější zákaz Pythonu, požádejte IT.'
    exit 0
} catch {
    Write-Host 'CHYBA: Pravidlo firewallu se nepodařilo změnit. Ověřte práva správce a pravidla školy s IT.'
    exit 1
}
