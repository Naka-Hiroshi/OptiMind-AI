powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SkipTests -Iscc "C:\Users\erikn\AppData\Local/Programs/Inno Setup 7/ISCC.exe" 2>&1 | Tee-Object -FilePath build.log
exit $LASTEXITCODE
