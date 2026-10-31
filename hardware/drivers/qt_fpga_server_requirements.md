# Qt Desktop to PYNQ service

Copy `qt_fpga_server.py`, `edge_morph_driver.py`, `edge_morphology_system.bit` and

`edge_morphology_system.hwh` to the same PYNQ Jupyter working directory.  From a

Jupyter terminal, generate a token and start the service:

```bash

TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

echo "$TOKEN"

nohup python3 qt_fpga_server.py --bitfile edge_morphology_system.bit --token "$TOKEN" > qt_fpga_server.log 2>&1 &

```

Keep the printed token private.  On the Windows computer, copy

`src/software/legacy/qt/agri_morph_gui/backend/board_config.example.json` to `board_config.json`

and put the token in it. The config file is intentionally git-ignored.

Verify it from the Windows computer:

```powershell

curl.exe http://192.168.2.99:8765/health

```

The service has only two routes: unauthenticated `GET /health` and authenticated

`POST /process`. It accepts a maximum 12 MiB source image and three fixed output

sizes.  It does not provide a shell, Jupyter access or arbitrary file access.

The application must choose Sobel or Prewitt; the V4 overlay does not implement

the Qt `Off` edge option. Each request writes the real parameters including

`morph_mode` to the V4 control register, runs DMA, compares its actual output

with `pipeline_reference`, and returns the images and metrics to Windows.

