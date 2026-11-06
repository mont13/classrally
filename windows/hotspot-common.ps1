# Spolecne funkce pro Windows PowerShell 5.1; tento soubor se pouze dot-sourcuje.
# Rucni overeni na Windows 10/11: nacist WinRT, zapnout/vypnout hotspot,
# vyzkouset profil s internetem i bez nej a ceske chyby pri vypnute Wi-Fi.

function Initialize-ClassRallyWinRt {
    if ([Environment]::OSVersion.Platform -ne 'Win32NT' -or
        $PSVersionTable.PSEdition -ne 'Desktop' -or
        $PSVersionTable.PSVersion -lt [version]'5.1') {
        throw 'Použijte Windows PowerShell 5.1 (powershell.exe), nikoli PowerShell 7 (pwsh).'
    }
    Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction Stop
    # Assembly Windows.Networking.* na nekterych sestavenich nefunguje;
    # WinRT resolver pak vyzaduje obecne assembly Windows.
    foreach ($entry in @(
        @('Windows.Networking.Connectivity.NetworkInformation', 'Windows.Networking.Connectivity'),
        @('Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager', 'Windows.Networking.NetworkOperators'),
        @('Windows.Networking.NetworkOperators.NetworkOperatorTetheringAccessPointConfiguration', 'Windows.Networking.NetworkOperators'),
        @('Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult', 'Windows.Networking.NetworkOperators'),
        @('Windows.Foundation.Metadata.ApiInformation', 'Windows.Foundation')
    )) {
        $loaded = $false
        foreach ($assembly in @($entry[1], 'Windows')) {
            try {
                $null = [Type]::GetType(('{0}, {1}, ContentType=WindowsRuntime' -f $entry[0], $assembly), $true)
                $loaded = $true
                break
            } catch { }
        }
        if (-not $loaded) { throw 'Windows neposkytuje potřebné WinRT API pro mobilní hotspot.' }
    }
}

function Wait-ClassRallyWinRt {
    param([Parameter(Mandatory = $true)]$Operation, [Type]$ResultType)
    # IAsyncAction (Configure...) a IAsyncOperation<T> (Start/Stop...) maji
    # ruzne AsTask overloady. Reflexe zabrani nejednoznacnemu vyberu v PS 5.1.
    if ($ResultType) {
        $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
            $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
            $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
        } | Select-Object -First 1
        $method = $method.MakeGenericMethod($ResultType)
    } else {
        $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
            $_.Name -eq 'AsTask' -and -not $_.IsGenericMethodDefinition -and
            $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.FullName -eq 'Windows.Foundation.IAsyncAction'
        } | Select-Object -First 1
    }
    $task = $method.Invoke($null, @($Operation))
    if (-not $task.Wait(60000)) {
        # Nezkouset dalsi profil: puvodni operace muze ve Windows jeste bezet.
        throw [System.TimeoutException]::new('Windows do 60 sekund neodpověděl. Stav hotspotu ověřte v Nastavení a teprve potom opakujte pokus.')
    }
    return $task.GetAwaiter().GetResult()
}

function Get-ClassRallyConnectionProfiles {
    $profiles = @()
    $internet = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
    if ($null -ne $internet) { $profiles += $internet }
    $profiles += @([Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles())
    $seen = @{}
    foreach ($profile in $profiles) {
        if ($null -eq $profile) { continue }
        $key = [string]$profile.ProfileName
        if ($null -ne $profile.NetworkAdapter) { $key += [string]$profile.NetworkAdapter.NetworkAdapterId }
        if (-not $seen.ContainsKey($key)) {
            $seen[$key] = $true
            $profile
        }
    }
}

function Get-ClassRallyTetheringMessage {
    param([string]$Status)
    # Vsechny hodnoty TetheringOperationStatus vcetne Win11 24H2.
    switch ($Status) {
        'Success' { 'Operace se podařila.' }
        'Unknown' { 'Windows neoznámil příčinu chyby; ověřte hotspot v Nastavení.' }
        'MobileBroadbandDeviceOff' { 'Mobilní modem je vypnutý. Zapněte jej nebo použijte jiný profil.' }
        'WiFiDeviceOff' { 'Wi-Fi adaptér je vypnutý. Zapněte Wi-Fi a vypněte režim letadlo.' }
        'EntitlementCheckTimeout' { 'Operátor včas nepotvrdil možnost sdílení připojení. Zkuste jiný profil.' }
        'EntitlementCheckFailure' { 'Mobilní operátor nepovoluje sdílení tohoto připojení. Zkuste jiný profil.' }
        'OperationInProgress' { 'Windows právě mění stav hotspotu. Vyčkejte a ověřte stav v Nastavení.' }
        'BluetoothDeviceOff' { 'Požadované zařízení Bluetooth je vypnuté. Zapněte jej nebo v Nastavení zvolte sdílení přes Wi-Fi.' }
        'NetworkLimitedConnectivity' { 'Profil má omezené připojení; Windows z něj nedovolil hotspot spustit.' }
        'AlreadyOn' { 'Hotspot už je zapnutý; použiji jeho současný název a heslo.' }
        'RadioRestriction' { 'Wi-Fi adaptér nebo předpisy nepovolují vybrané pásmo. V Nastavení zvolte 2,4 GHz.' }
        'BandInterference' { 'Pásmo hotspotu se ruší s hlavním připojením. V Nastavení zvolte jiné pásmo, obvykle 2,4 GHz.' }
        default { 'Windows vrátil neznámý stav hotspotu. Ověřte jej v Nastavení.' }
    }
}

function Show-ClassRallyHotspotHelp {
    Write-Host ''
    Write-Host 'Bez internetu Windows někdy nemá použitelný síťový profil.'
    Write-Host '1. Připojte notebook na chvíli k hotspotu telefonu a zkuste zapnutí znovu.'
    Write-Host '2. IT může připravit adaptér Microsoft KM-TEST Loopback (vyžaduje správce).'
    Write-Host '   ClassRally žádný ovladač neinstaluje; ani loopback nezaručuje podporu hotspotu.'
    Write-Host '3. Otevřete Nastavení > Síť a internet > Mobilní hotspot:'
    Write-Host '   ms-settings:network-mobilehotspot'
    Write-Host '   Nebo použijte vlastní cestovní Wi-Fi router.'
}

function Save-ClassRallyWifi {
    param([string]$Ssid, [string]$Password)
    # Heslo se uklada pouze do uzivatelovych dat, nikdy do logu/balicku.
    # JSON nema BOM: server jej muze cist jako bezne UTF-8.
    $folder = Join-Path $env:LOCALAPPDATA 'ClassRally'
    $null = [IO.Directory]::CreateDirectory($folder)
    $path = Join-Path $folder 'wifi.json'
    $temporary = Join-Path $folder ('wifi-{0}.tmp' -f [Guid]::NewGuid().ToString('N'))
    $json = @{ ssid = $Ssid; password = $Password } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($temporary, $json, (New-Object Text.UTF8Encoding($false)))
    if ([IO.File]::Exists($path)) {
        [IO.File]::Replace($temporary, $path, $null)
    } else {
        [IO.File]::Move($temporary, $path)
    }
}
