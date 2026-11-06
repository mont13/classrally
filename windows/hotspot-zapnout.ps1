#Requires -Version 5.1
<#
Ruční zkouška (Windows PowerShell 5.1, běžný uživatel):
  - zapnout s internetem i bez něj; Wi-Fi vypnutá; žádný použitelný profil;
  - znovu zapnout již běžící hotspot, bez odpojení připojeného telefonu;
  - ověřit aktuální SSID, heslo a IP; wifi.json načte /host jako Wi-Fi QR;
  - na Win10 2004+ nechat hotspot bez klientů déle než pět minut;
  - telefon připojit, ponechat připojení bez internetu a otevřít herní QR.
Skript nesmí být spouštěn s přesměrováním do logu: na konzoli ukáže Wi-Fi heslo.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'hotspot-common.ps1')

try {
    Initialize-ClassRallyWinRt
    $wifiPath = Join-Path (Join-Path $env:LOCALAPPDATA 'ClassRally') 'wifi.json'
    $ssid = 'ClassRally-' + [Guid]::NewGuid().ToString('N').Substring(0, 4)
    $password = $null
    if (Test-Path -LiteralPath $wifiPath -PathType Leaf) {
        try {
            $saved = [IO.File]::ReadAllText($wifiPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
            if ($saved.ssid -is [string]) { $ssid = $saved.ssid }
            if ($saved.password -is [string]) { $password = $saved.password }
        } catch {
            Write-Warning 'Uložené údaje Wi-Fi nejsou čitelné. Pro nový hotspot vytvořím nové údaje.'
        }
    }
    if ([string]::IsNullOrWhiteSpace($ssid) -or [Text.Encoding]::UTF8.GetByteCount($ssid) -gt 32 -or $ssid -match '[\x00-\x1f\x7f]') {
        throw [System.ArgumentException]::new('Název Wi-Fi musí mít 1 až 32 bajtů UTF-8 a nesmí obsahovat řídicí znaky. Opravte jej v učitelském portálu.')
    }
    if ([string]::IsNullOrEmpty($password)) {
        $random = New-Object byte[] 8
        $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $generator.GetBytes($random) } finally { $generator.Dispose() }
        $password = [BitConverter]::ToString($random).Replace('-', '').ToLowerInvariant()
    }
    if ($password.Length -lt 8 -or $password.Length -gt 63 -or $password -match '[^\x20-\x7e]') {
        throw [System.ArgumentException]::new('Heslo hotspotu musí mít 8 až 63 tisknutelných znaků ASCII. Opravte je v učitelském portálu.')
    }

    $manager = $null
    foreach ($profile in @(Get-ClassRallyConnectionProfiles)) {
        try {
            $candidate = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
            $state = [string]$candidate.TetheringOperationalState
            if ($state -eq 'On') {
                Write-Host 'Hotspot už je zapnutý; použiji jeho současný název a heslo.'
                $manager = $candidate
                break
            }
            if ($state -eq 'InTransition') {
                throw [System.TimeoutException]::new('Windows právě mění stav hotspotu. Vyčkejte, ověřte jej v Nastavení a spusťte skript znovu.')
            }
            $configuration = $candidate.GetCurrentAccessPointConfiguration()
            $configuration.Ssid = $ssid
            $configuration.Passphrase = $password
            # Win10/11: konfigurace je IAsyncAction, start IAsyncOperation<T>.
            # StartTetheringAsync(config) vyzaduje jiny typ a az Win11 24H2:
            # https://learn.microsoft.com/en-us/uwp/api/windows.networking.networkoperators.networkoperatortetheringmanager.starttetheringasync
            Wait-ClassRallyWinRt -Operation ($candidate.ConfigureAccessPointAsync($configuration))
            $result = Wait-ClassRallyWinRt -Operation ($candidate.StartTetheringAsync()) -ResultType ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
            $status = [string]$result.Status
            Write-Host (Get-ClassRallyTetheringMessage $status)
            if ($status -eq 'Success' -or $status -eq 'AlreadyOn') {
                $manager = $candidate
                break
            }
            if ($status -eq 'OperationInProgress') {
                throw [System.TimeoutException]::new('Operace hotspotu ještě probíhá. Ověřte stav v Nastavení a spusťte skript znovu.')
            }
        } catch [System.TimeoutException] {
            throw
        } catch {
            # Nepiseme syrove WinRT vyjimky: mohou obsahovat nastaveni site.
            Write-Host 'Tento síťový profil hotspot nespustil; zkusím další dostupný profil.'
        }
    }
    if ($null -eq $manager) { throw 'Žádný dostupný síťový profil nedovolil zapnout mobilní hotspot.' }

    try {
        if ([Windows.Foundation.Metadata.ApiInformation]::IsMethodPresent('Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager', 'DisableNoConnectionsTimeout')) {
            [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::DisableNoConnectionsTimeout()
            Write-Host 'Automatické vypínání hotspotu bez klientů je vypnuté.'
        } else {
            Write-Warning 'Tato verze Windows neumí vypnout časový limit hotspotu; zkontrolujte úsporu energie v Nastavení.'
        }
    } catch {
        Write-Warning 'Hotspot běží, ale vypnutí časového limitu se nepodařilo. Zkontrolujte úsporu energie v Nastavení.'
    }

    $actual = $manager.GetCurrentAccessPointConfiguration()
    Save-ClassRallyWifi -Ssid $actual.Ssid -Password $actual.Passphrase
    Write-Host ''
    Write-Host ('Wi-Fi: {0}' -f $actual.Ssid)
    Write-Host ('Heslo: {0}' -f $actual.Passphrase)
    $addresses = @()
    for ($attempt = 0; $attempt -lt 5; $attempt++) {
        $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -like '192.168.137.*' } | Select-Object -ExpandProperty IPAddress -Unique)
        if ($addresses.Count -gt 0) { break }
        Start-Sleep -Seconds 1
    }
    if ($addresses.Count -eq 0) {
        $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | Select-Object -ExpandProperty IPAddress -Unique)
        Write-Host 'Windows zatím neoznámil obvyklou adresu hotspotu. V /host vyberte adresu adaptéru hotspotu.'
    }
    foreach ($address in $addresses) { Write-Host ('Adresa pro žáky: http://{0}:48217' -f $address) }
    Write-Host 'Na notebooku spusťte ClassRally a otevřete http://localhost:48217/host.'
    Write-Host 'Údaje Wi-Fi jsou uložené v místních datech ClassRally pro QR kód učitele.'
    exit 0
} catch {
    # Vlastni popis chyby nikdy nevypisuje ulozenou konfiguraci ani heslo.
    if ($_.Exception -is [System.TimeoutException] -or $_.Exception -is [System.ArgumentException]) {
        Write-Host ('CHYBA: {0}' -f $_.Exception.Message)
    } else {
        Write-Host 'CHYBA: Hotspot nebo uložení údajů se nepodařilo. Ověřte Wi-Fi, platný název a heslo (8 až 63 znaků ASCII), oprávnění k místním datům a pravidla vašeho IT.'
    }
    Show-ClassRallyHotspotHelp
    exit 1
}
