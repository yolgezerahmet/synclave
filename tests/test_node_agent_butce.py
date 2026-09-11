#!/usr/bin/env python3
"""Node agent adım bütçesi + log teşhis kapısı (v2.5.1).

Ölçülmüş üretim hatası (11 Eyl 2026, H1):
  cron `run-node-agent.sh` dış kapısı 3600s'te script'i ÖLDÜRÜR; `node_agent.py`
  backup adımı da `timeout=3600` kullanıyordu → iç bütçe dış kapıya EŞİT olduğu
  için adım kendi hatasını hiç yazamadı. Sonuç: 14 ardışık koşu
  `last_status=error` + "Script timed out after 3600s" ve `/tmp/node_agent.log`
  BOŞ (Python stdout'u dosyaya yönlendirilince blok tamponlar → teşhis yok).
  Ölçüm: sync 'both' 6.7 dk'da bitti (16:19:53Z), backup 53+ dk sürdü → kapı vurdu.

Bu kapı üç şeyi zorlar:
  1. Her adım bütçesi dış kapının ALTINDA ve toplam bütçe dış kapıyı aşmaz.
  2. Motor çağrıları çıplak sabit yerine BUTCE_* sabitlerini kullanır.
  3. stdout satır tamponlu — öldürülen koşu bile log'da son adımı bırakır.
"""
import os
import re
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import synclave.node_agent as na  # noqa: E402

KAYNAK = (REPO / "synclave" / "node_agent.py").read_text(encoding="utf-8")

BUTCE_ADLARI = ("BUTCE_SYNC_S", "BUTCE_BACKUP_S", "BUTCE_MEMORY_S",
                "BUTCE_DIGER_S", "BUTCE_VARSAYILAN_S", "BUTCE_KISA_S")


def test_butceler_diskapinin_altinda():
    """Hiçbir iç bütçe dış kapıya eşit/yakın olmamalı (adım kendi hatasını yazsın)."""
    assert na.DIS_KAPI_S == 3600
    for ad in BUTCE_ADLARI:
        deger = getattr(na, ad)
        assert deger < na.DIS_KAPI_S, f"{ad}={deger} dış kapıya eşit/aşkın"
    # ölçülen 'both' 6.7 dk → sync bütçesi en az 2× pay bırakmalı
    assert na.BUTCE_SYNC_S >= 600


def test_toplam_butce_diskapinin_altinda():
    """Adım bütçeleri toplamı dış kapıyı aşarsa koşu yine rapor edilemeden ölür."""
    toplam = (na.BUTCE_SYNC_S + na.BUTCE_BACKUP_S + na.BUTCE_MEMORY_S
              + na.BUTCE_DIGER_S)
    assert toplam < na.DIS_KAPI_S, (
        f"toplam adım bütçesi {toplam}s ≥ dış kapı {na.DIS_KAPI_S}s — "
        "aşan koşu SIGKILL ile ölür ve teşhis kaybolur"
    )


def _kod_satirlari(kaynak: str) -> str:
    """Yorumları at — kapı KODU denetler; yorumlar eski değeri anabilir."""
    return "\n".join(s.split("#", 1)[0] for s in kaynak.splitlines())


def test_motor_cagrilari_butce_sabitlerini_kullanir():
    """`motor(...)` çağrılarında çıplak sabit timeout kalmamalı (KOD)."""
    kod = _kod_satirlari(KAYNAK)
    cagrilar = re.findall(r"motor\([^)]*\)", kod)
    assert cagrilar, "motor() çağrısı bulunamadı"
    for c in cagrilar:
        if "timeout=" not in c:
            continue
        assert "BUTCE_" in c, f"çıplak sabit timeout (bütçe dışı): {c.strip()}"
    # eski çıplak değerler KODA geri gelmesin (yorumda anılması serbest)
    for yasak in ("timeout=3600", "timeout=1200", "timeout=600)"):
        assert yasak not in kod, f"eski çıplak timeout koda geri geldi: {yasak}"


def test_stdout_satir_tamponlu():
    """Log dosyasına yönlendirilince blok tampon = boş log; satır tamponu şart."""
    assert "line_buffering=True" in KAYNAK
    assert "stdout.reconfigure" in KAYNAK


def test_run_timeout_rc_ve_mesaj():
    """Zaman aşımı gerçekten rapor edilir: rc=-1 + 'TIMEOUT <n>s'."""
    rc, out, err = na.run([sys.executable, "-c", "import time; time.sleep(5)"],
                          timeout=1, cwd=REPO)
    assert rc == -1
    assert "TIMEOUT 1s" in err


def test_adim_sure_olcer_ve_hatayi_yutar(capsys):
    """_adim: süre ölçer, başladı/bitti yazar, adım hatasını koşuya yaymaz."""
    rc, out, err, sure = na._adim("test", lambda: (7, "cikti", "hata"))
    assert (rc, out, err) == (7, "cikti", "hata")
    assert sure >= 0.0
    cikti = capsys.readouterr().out
    assert "▶ test başladı" in cikti and "◀ test bitti rc=7" in cikti

    def _patlat():
        raise RuntimeError("adım çöktü")

    rc2, _o, err2, _s2 = na._adim("coken", _patlat)
    assert rc2 == 1 and "adım çöktü" in err2       # koşu düşmez, rapor eder


def test_backup_butcesi_senkronu_kapsar():
    """Backup bütçesi sync'ten büyük olmalı (yedek sync'ten uzun sürer)."""
    assert na.BUTCE_BACKUP_S > na.BUTCE_SYNC_S
