$ErrorActionPreference = "Stop"
$Pasta = Split-Path -Parent $MyInvocation.MyCommand.Path
$Inicializador = Join-Path $Pasta "Iniciar_Orbiway.bat"
$Icone = Join-Path $Pasta "assets\orbiway.ico"
if (-not (Test-Path -LiteralPath $Inicializador)) { throw "Não encontrei Iniciar_Orbiway.bat em: $Pasta" }
if (-not (Test-Path -LiteralPath $Icone)) { throw "Não encontrei o ícone: $Icone" }
$Shell = New-Object -ComObject WScript.Shell
$Desktop = $Shell.SpecialFolders.Item("Desktop")
if ([string]::IsNullOrWhiteSpace($Desktop)) { $Desktop = [Environment]::GetFolderPath("Desktop") }
$Atalho = Join-Path $Desktop "Orbiway.lnk"
$Link = $Shell.CreateShortcut($Atalho)
$Link.TargetPath = $Inicializador
$Link.WorkingDirectory = $Pasta
$Link.IconLocation = "$Icone,0"
$Link.Description = "Orbiway - Roteirização Inteligente de Entregas"
$Link.Save()
try {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class OrbiwayShellNotify {
    [DllImport("shell32.dll")]
    public static extern void SHChangeNotify(uint e, uint f, IntPtr a, IntPtr b);
}
"@ -ErrorAction SilentlyContinue
    [OrbiwayShellNotify]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
} catch {}
Write-Host "Atalho Orbiway criado: $Atalho" -ForegroundColor Green
Read-Host "Pressione Enter para fechar"
