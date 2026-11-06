; Установщик «Пульт ПК» (Inno Setup 6). Собирается в CI, см. .github/workflows/build-installer.yml
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{7B1E2C54-5D0A-4E6B-9C3A-PCCONTROL0001}
AppName=Пульт ПК
AppVersion={#AppVersion}
AppPublisher=Fersiya
AppPublisherURL=https://github.com/Fersiya/PhoneV2-server
DefaultDirName={autopf}\PCControl
DefaultGroupName=Пульт ПК
DisableProgramGroupPage=yes
OutputBaseFilename=PCControl-Setup
OutputDir=..\dist-installer
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\PCControl.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
DisableWelcomePage=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "autostart"; Description: "Запускать вместе с Windows (рекомендуется)"; GroupDescription: "Дополнительно:"
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; GroupDescription: "Дополнительно:"; Flags: unchecked

[Files]
Source: "..\dist\PCControl\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Пульт ПК"; Filename: "{app}\PCControl.exe"
Name: "{group}\Удалить Пульт ПК"; Filename: "{uninstallexe}"
Name: "{commonstartup}\Пульт ПК"; Filename: "{app}\PCControl.exe"; Tasks: autostart
Name: "{autodesktop}\Пульт ПК"; Filename: "{app}\PCControl.exe"; Tasks: desktopicon

[Run]
; Брандмауэр: порт 8765 только для домашней сети и Tailscale, не для всего интернета
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Пульт ПК"""; Flags: runhidden; StatusMsg: "Настраиваю брандмауэр…"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Пульт ПК"" dir=in action=allow protocol=TCP localport=8765 profile=any remoteip=LocalSubnet,100.64.0.0/10"; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Пульт ПК (UDP)"" dir=in action=allow protocol=UDP localport=8765 profile=any remoteip=LocalSubnet,100.64.0.0/10"; Flags: runhidden
Filename: "{app}\PCControl.exe"; Description: "Запустить Пульт ПК"; Flags: nowait postinstall runasoriginaluser

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM PCControl.exe"; Flags: runhidden; RunOnceId: "KillApp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Пульт ПК"""; Flags: runhidden; RunOnceId: "FwTcp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Пульт ПК (UDP)"""; Flags: runhidden; RunOnceId: "FwUdp"
