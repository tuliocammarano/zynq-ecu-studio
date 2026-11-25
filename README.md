# Zynq-7010 ECU Studio

Interface web estática para calibração, mapas 16×16 (estilo FTManager), curvas 1D e telemetria ao vivo da ECU Zynq-7010 (EBAZ4205).

- **GitHub Pages (HTTPS)**: funciona 100% estático via **Web Serial API** (Chrome/Edge -> USB `COM11` @ 115200 8N1).
- **Modo Local (Wi-Fi)**: rode `python ecu_web_server.py --host 0.0.0.0 --port 8080` para conectar via ponte TCP do ESP32-C3 (`192.168.25.134:2543`).
