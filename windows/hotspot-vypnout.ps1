#Requires -Version 5.1
<#
Ruční zkouška na Windows: vypnout běžící hotspot, zopakovat při vypnutém,
ověřit odpojení telefonu a výsledek v Nastavení > Mobilní hotspot.
wifi.json zůstává v místních datech, aby příští zapnutí použilo stejné údaje.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'hotspot-common.ps1')

try {
    Initialize-ClassRallyWinRt
    $checked = $false
    $unverified = $false
    foreach ($profile in @(Get-ClassRallyConnectionProfiles)) {
        try {
            $manager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
            $state = [string]$manager.TetheringOperationalState
            if ($state -eq 'Off') { $checked = $true; continue }
            if ($state -eq 'InTransition') {
                throw [System.TimeoutException]::new('Windows právě mění stav hotspotu. Vyčkejte a ověřte stav v Nastavení.')
            }
            if ($state -ne 'On') { $unverified = $true; continue }
            $unverified = $true
            $result = Wait-ClassRallyWinRt -Operation ($manager.StopTetheringAsync()) -ResultType ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
            $status = [string]$result.Status
            Write-Host (Get-ClassRallyTetheringMessage $status)
            if ($status -eq 'Success') {
                Write-Host 'Mobilní hotspot je vypnutý.'
                exit 0
            }
            if ($status -eq 'OperationInProgress') {
                throw [System.TimeoutException]::new('Vypnutí hotspotu ještě probíhá. Ověřte stav v Nastavení.')
            }
        } catch [System.TimeoutException] {
            throw
        } catch {
            $unverified = $true
            Write-Host 'Tento profil nelze ovládat; zkusím další dostupný profil.'
        }
    }
    if ($checked -and -not $unverified) {
        Write-Host 'Hotspot dostupných profilů už je vypnutý.'
        exit 0
    }
    Write-Host 'Stav hotspotu se nepodařilo ověřit. Vypněte jej ručně v Nastavení:'
    Write-Host 'ms-settings:network-mobilehotspot'
    exit 1
} catch {
    Write-Host 'CHYBA: Hotspot se nepodařilo vypnout nebo Windows ještě mění jeho stav.'
    Write-Host 'Ověřte jej a případně vypněte ručně: ms-settings:network-mobilehotspot'
    exit 1
}
