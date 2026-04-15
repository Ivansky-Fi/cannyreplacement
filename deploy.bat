@echo off
echo Deploying to Railway...
cd /d "%~dp0"
git add .
git commit -m "Update"
git push
echo.
echo Done! Railway will redeploy in about 1 minute.
pause
