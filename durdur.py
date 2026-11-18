# -*- coding: utf-8 -*-
"""durdur.py - baslat.py ile açılan ajanı kapatır."""
import os
import signal

pid_dosya = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ajan_pid")
if os.path.exists(pid_dosya):
    pid = int(open(pid_dosya).read().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print("Ajan kapatıldı.")
    except OSError:
        print("Ajan zaten kapalı.")
    os.remove(pid_dosya)
else:
    print("Çalışan ajan bulunamadı.")
