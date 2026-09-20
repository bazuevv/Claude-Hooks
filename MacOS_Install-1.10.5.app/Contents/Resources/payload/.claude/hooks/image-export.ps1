$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$imageRequest = [Console]::In.ReadToEnd() | ConvertFrom-Json
$imageBytes = [Convert]::FromBase64String($imageRequest.base64)
if ($imageRequest.action -eq 'copy') {
    $imageStream = [System.IO.MemoryStream]::new($imageBytes, $false)
    $bitmap = [System.Drawing.Bitmap]::new($imageStream)
    try {
        $clipboardData = [System.Windows.Forms.DataObject]::new()
        $clipboardData.SetImage($bitmap)
        $imageStream.Position = 0
        $clipboardData.SetData('PNG', $false, $imageStream)
        [System.Windows.Forms.Clipboard]::SetDataObject($clipboardData, $true, 5, 100)
        '{"ok":true}'
    } finally {
        $bitmap.Dispose()
        $imageStream.Dispose()
    }
} elseif ($imageRequest.action -eq 'save') {
    $dialog = [System.Windows.Forms.SaveFileDialog]::new()
    $dialogOwner = [System.Windows.Forms.Form]::new()
    try {
        # The helper has no console or application window. Give the dialog
        # a foreground owner and avoid the shell COM picker in this process.
        $dialog.AutoUpgradeEnabled = $false
        $dialog.Title = 'Save image'
        $dialog.InitialDirectory = [Environment]::GetFolderPath('UserProfile')
        $dialog.FileName = $imageRequest.name
        $dialog.DefaultExt = $imageRequest.extension
        $dialog.Filter = 'Image (*.' + $imageRequest.extension + ')|*.' + $imageRequest.extension
        $dialog.AddExtension = $true
        $dialog.OverwritePrompt = $true
        $dialogOwner.ShowInTaskbar = $false
        $dialogOwner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
        $dialogOwner.Size = [System.Drawing.Size]::new(1, 1)
        $dialogOwner.Opacity = 0
        $dialogOwner.TopMost = $true
        $dialogOwner.Show()
        $dialogOwner.Activate()
        if ($dialog.ShowDialog($dialogOwner) -eq [System.Windows.Forms.DialogResult]::OK) {
            [System.IO.File]::WriteAllBytes($dialog.FileName, $imageBytes)
            '{"ok":true}'
        } else {
            '{"ok":true,"cancelled":true}'
        }
    } finally {
        $dialog.Dispose()
        $dialogOwner.Dispose()
    }
} else {
    throw 'Unknown image action'
}
