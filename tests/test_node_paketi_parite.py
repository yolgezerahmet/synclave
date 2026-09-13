#!/usr/bin/env python3
"""Node kodu paketi (`synclave_kod/`) kanonik kopyalarla byte-eşit olmalı.

Neden (14 Eyl 2026 bulgusu): `synclave_kod/` node'lara düz (paket olmayan) modül
paketi olarak verilir ve `h2_dogrula.py` bu dizinden import eder. Ölçüm: yedi
modülden DÖRDÜ bayattı (sync_motor 2.2.0 ↔ 2.7.0, a2a_cli 164 ↔ 266 satır,
agent_mesh_a2a 440 ↔ 535, gpu_agent 181 ↔ 229). Aynı sınıf sapma bu depoyu bir
kez üretimde vurdu: "kök kopya cron'un çalıştırdığı dosyadır ve düzeltmeyi
taşımıyordu; testler paket kopyasını okuduğu için sapma GÖRÜNMEZDİ".

Kapı: dizin varsa (private klonda yok → SKIP) bu yedi flat modül kök kopyayla
byte-eşit olmalı. Yeni bir flat modül eklenirse `MODULLER` listesine eklenir.
"""
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
KOD = REPO / "synclave_kod"
MODULLER = ("a2a_cli.py", "agent_identity.py", "agent_mesh_a2a.py",
            "conversation_bridge.py", "gpu_agent.py", "gpu_task.py",
            "sync_motor.py")


@pytest.mark.parametrize("ad", MODULLER)
def test_node_paketi_kanonikle_ayni(ad):
    if not KOD.is_dir():
        pytest.skip("synclave_kod/ bu kopyada yok (twin/private klon)")
    kok, kod = REPO / ad, KOD / ad
    if not kok.is_file() or not kod.is_file():
        pytest.skip(f"{ad} iki tarafta birden yok")
    assert kok.read_bytes() == kod.read_bytes(), (
        f"{ad}: node paketi (synclave_kod/) kanonik kopyadan ayrıştı — "
        f"({len(kok.read_text(encoding='utf-8').splitlines())} ↔ "
        f"{len(kod.read_text(encoding='utf-8').splitlines())} satır)")


def test_kanonik_modul_kumesi_tam():
    """Ölçülen yedi flat modül eksiksiz mi (sessiz kapsam daralması olmasın)."""
    assert len(MODULLER) == 7 and len(set(MODULLER)) == 7
    for ad in MODULLER:
        assert (REPO / ad).is_file(), f"kanonik {ad} yok"
