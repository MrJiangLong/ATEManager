@echo off

REM 打包 ate-cli 为单文件 exe（需要 pip install pyinstaller）

cd /d %~dp0

pyinstaller -F -n ate-cli -i app.ico --distpath dist --workpath build --clean cli.py

echo done: dist\ate-cli.exe

