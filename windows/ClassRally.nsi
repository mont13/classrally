; Compile only when makensis is already installed. No plugin/download required.
; Windows VM: verify per-user install, all shortcuts, update and data-preserving uninstall.
Unicode true
!include "MUI2.nsh"
!ifndef APP_SOURCE
  !error "APP_SOURCE must point to the prepared ClassRally payload"
!endif
!ifndef OUTPUT_FILE
  !error "OUTPUT_FILE must be specified"
!endif
Name "ClassRally"
OutFile "${OUTPUT_FILE}"
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\Programs\ClassRally"
InstallDirRegKey HKCU "Software\ClassRally" "InstallDir"
!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Czech"

Section "ClassRally"
  SetShellVarContext current
  SetOutPath "$INSTDIR"
  File /r "${APP_SOURCE}/*"
  WriteUninstaller "$INSTDIR\Odinstalovat.exe"
  CreateDirectory "$SMPROGRAMS\ClassRally"
  CreateShortcut "$SMPROGRAMS\ClassRally\ClassRally.lnk" "$INSTDIR\Spustit-ClassRally.bat"
  CreateShortcut "$SMPROGRAMS\ClassRally\ClassRally – zapnout hotspot.lnk" "$INSTDIR\Zapnout-hotspot.bat"
  CreateShortcut "$SMPROGRAMS\ClassRally\ClassRally – povolit ve firewallu.lnk" "$INSTDIR\Povolit-firewall.bat"
  CreateShortcut "$SMPROGRAMS\ClassRally\Odinstalovat ClassRally.lnk" "$INSTDIR\Odinstalovat.exe"
  WriteRegStr HKCU "Software\ClassRally" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ClassRally" "DisplayName" "ClassRally"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ClassRally" "UninstallString" '$"$INSTDIR\Odinstalovat.exe$"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ClassRally" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ClassRally" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ClassRally" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  ; Only installed program files. NEVER touch $LOCALAPPDATA\ClassRally (user data).
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\static"
  RMDir /r "$INSTDIR\questions"
  Delete "$INSTDIR\server.py"
  Delete "$INSTDIR\qrgen.py"
  Delete "$INSTDIR\db.py"
  Delete "$INSTDIR\ws.py"
  Delete "$INSTDIR\mailer.py"
  Delete "$INSTDIR\bugreport.py"
  Delete "$INSTDIR\launcher.py"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\Spustit-ClassRally.bat"
  Delete "$INSTDIR\Zapnout-hotspot.bat"
  Delete "$INSTDIR\Vypnout-hotspot.bat"
  Delete "$INSTDIR\Povolit-firewall.bat"
  Delete "$INSTDIR\Odebrat-firewall.bat"
  Delete "$INSTDIR\hotspot-common.ps1"
  Delete "$INSTDIR\hotspot-zapnout.ps1"
  Delete "$INSTDIR\hotspot-vypnout.ps1"
  Delete "$INSTDIR\firewall-povolit.ps1"
  Delete "$INSTDIR\CTI-ME-PRVNI.txt"
  Delete "$INSTDIR\WIFI-NAVOD.txt"
  Delete "$INSTDIR\Odinstalovat.exe"
  RMDir "$INSTDIR"
  RMDir /r "$SMPROGRAMS\ClassRally"
  DeleteRegKey HKCU "Software\ClassRally"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ClassRally"
SectionEnd
