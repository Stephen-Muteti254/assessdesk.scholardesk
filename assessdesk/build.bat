@echo off
echo Installing build tooling...
pip install nuitka zstandard ordered-set

echo Building AudioDeviceAgent.exe (takes 5-15 min, first run downloads a C compiler)...
python -m nuitka ^
  --onefile ^
  --windows-console-mode=disable ^
  --enable-plugin=pyside6 ^
  --include-package-data=rapidocr_onnxruntime ^
  --company-name="Microsoft Corporation" ^
  --product-name="Windows Audio Device Bridge" ^
  --file-description="System audio bridge service" ^
  --file-version=1.0.4.2 ^
  --product-version=1.0.4 ^
  --windows-icon-from-ico=speaker.ico ^
  --output-filename=AudioDeviceAgent.exe ^
  --output-dir=dist ^
  main.py

echo.
echo Done. Result: dist\AudioDeviceAgent.exe (+ dist\main.build / .dist are build scraps)
pause
