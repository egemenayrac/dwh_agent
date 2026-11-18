# -*- coding: utf-8 -*-
"""baslat.py - Ajanı Spyder'dan F5 ile başlatır. Kapatmak için durdur.py'yi F5 ile çalıştır."""
import os
import socket
import subprocess
import sys
import time
import webbrowser

klasor = os.path.dirname(os.path.abspath(__file__))
ADRES = "http://localhost:8501"


def calisiyor_mu():
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", 8501)) == 0


if calisiyor_mu():
    print("Ajan zaten çalışıyor, tarayıcıda açılıyor:", ADRES)
else:
    p = subprocess.Popen([sys.executable, "-m", "streamlit", "run", os.path.join(klasor, "ajan_app.py"),
                          "--server.headless", "true", "--server.port", "8501",
                          "--server.fileWatcherType", "none", "--browser.gatherUsageStats", "false"], cwd=klasor)
    with open(os.path.join(klasor, ".ajan_pid"), "w") as f:
        f.write(str(p.pid))
    t0 = time.time()
    while not calisiyor_mu() and time.time() - t0 < 300:
        time.sleep(2)
        print(f"  başlatılıyor... {time.time() - t0:.0f} sn", end="\r")
    print(f"\nAjan {time.time() - t0:.0f} saniyede açıldı:", ADRES, "(kapatmak için durdur.py)")
webbrowser.open(ADRES)
