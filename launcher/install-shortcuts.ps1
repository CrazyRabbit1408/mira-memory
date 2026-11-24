$ErrorActionPreference = "Stop"

$launcher = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath("Desktop")
$folder = Join-Path $desktop "Mira Memory"
New-Item -ItemType Directory -Force -Path $folder | Out-Null

$buttons = @(
    @{ Name = "Мира — запустить"; File = "START-MIRA.bat" },
    @{ Name = "Мира — запуск с логом"; File = "START-MIRA-DEBUG.bat" },
    @{ Name = "Мира — остановить"; File = "STOP-MIRA.bat" },
    @{ Name = "Мира — статус"; File = "MIRA-STATUS.bat" },
    @{ Name = "Мира — открыть панель"; File = "OPEN-MIRA.bat" },
    @{ Name = "Мира — подготовить"; File = "PREPARE-MIRA.bat" }
)

$shell = New-Object -ComObject WScript.Shell
foreach ($button in $buttons) {
    $target = Join-Path $launcher $button.File
    $shortcut = Join-Path $folder ($button.Name + ".lnk")
    $link = $shell.CreateShortcut($shortcut)
    $link.TargetPath = $target
    $link.WorkingDirectory = $launcher
    $link.Description = "Управление локальным AI-персонажем Мирой"
    $link.Save()
}

Write-Host "Кнопки созданы в: $folder"
Write-Host "Главная кнопка: «Мира — запустить»"