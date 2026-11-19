# Embedded in the task, so no writable script is needed at login.
$ErrorActionPreference = 'Stop'
while ($true) {
    try {
        # Native stderr must not turn a completed runner into a terminating error.
        $ErrorActionPreference = 'Continue'
        & $runner runner
    }
    catch { }
    finally { $ErrorActionPreference = 'Stop' }
    # Includes clean exits after version draining, failures and spawn errors.
    Start-Sleep -Seconds 15
}
