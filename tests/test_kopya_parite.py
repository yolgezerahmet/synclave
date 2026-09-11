#!/usr/bin/env python3
"""Kopya paritesi kapısı — sync_motor.py tek kanonik içerik olmalı (v2.5.0).

Neden: 11 Eyl 2026'da dört kopya (public kök, public paket, private kök,
private paket) FARKLI içerikteydi — biri `_lsd_names` düzeltmesini taşıyor,
diğeri taşımıyordu; üretim kök kopyası (cron'un çalıştırdığı dosya) hâlâ
`| wc -l` / `| tail -1` pipeline'larını kullanıyordu ve bu yüzden rclone
retry hiç tetiklenmiyordu. Testler yalnız `synclave/sync_motor.py` dosyasını
okuduğu için sapma GÖRÜNMEZDİ (yeşil test + bozuk üretim).

Bu kapı üç şeyi zorlar:
  1. Aynı depoda kök `sync_motor.py` == `synclave/sync_motor.py` (byte).
  2. Kritik semboller kanonik dosyada VAR (birleşim kaybı olmaz).
  3. `__version__` tek değer ve CHANGELOG'un en üst sürümüyle aynı.

İkiz depo (private `cumulus-sync-motor`) varsa, aynı sürümü beyan ettiği
sürece byte-eşit olmalı. `SYNCLAVE_IKIZ_ZORUNLU=0` ile o kontrol kapatılır
(ör. ikiz depo bilinçli olarak farklı bir dala bakıyorsa).
"""
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
KOK = REPO / "sync_motor.py"
MODUL = REPO / "synclave" / "sync_motor.py"

# Birleşik kanonik içerikte bulunması ZORUNLU işaretler (sapma dedektörleri).
ZORUNLU_SEMBOLLER = (
    "def rclone_read(",
    "def _lsd_names(",
    "def unique_a2a_nodes(",
    "def _sha_cached(",
    "def _log_event(",
    "def cmd_identity(",
    "def _is_idempotent_read(",
    '"listremotes"',
    '"direxists"',
    '"about"',
)


def _oku(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_ayni_depo_kopyalari_byte_esit():
    """Kök (üretim) kopya ile paket kopyası ayrışırsa kırmızı."""
    assert KOK.is_file() and MODUL.is_file(), "sync_motor.py kopyaları eksik"
    kok, modul = _oku(KOK), _oku(MODUL)
    if kok != modul:
        sapan = [
            f"kok {len(kok.splitlines())} satır / modul {len(modul.splitlines())} satır",
        ]
        for sem in ZORUNLU_SEMBOLLER:
            if (sem in kok) != (sem in modul):
                sapan.append(f"  yalnız birinde: {sem}")
        pytest.fail("sync_motor.py kopyaları ayrıştı:\n" + "\n".join(sapan))


def test_kanonik_semboller_var():
    """Birleştirme sırasında sembol kaybı olursa kırmızı."""
    src = _oku(MODUL)
    eksik = [s for s in ZORUNLU_SEMBOLLER if s not in src]
    assert not eksik, f"kanonik dosyada eksik sembol: {eksik}"


def test_pipeline_kalintisi_yok():
    """rc kaybı: okuma komutları pipeline'a dönerse retry tetiklenmez."""
    src = _oku(MODUL)
    assert "2>/dev/null | wc -l" not in src
    assert "2>/dev/null | tail -1" not in src


def _surum(metin: str) -> str:
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', metin, re.M)
    assert m, "__version__ bulunamadı"
    return m.group(1)


def test_surum_tek_kaynak_ve_changelog_ile_uyumlu():
    kok_v, mod_v = _surum(_oku(KOK)), _surum(_oku(MODUL))
    assert kok_v == mod_v, f"kopyalarda farklı sürüm: kök={kok_v} modül={mod_v}"
    changelog = _oku(REPO / "CHANGELOG.md")
    basliklar = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.M)
    assert basliklar, "CHANGELOG'da sürüm başlığı yok"
    assert basliklar[0] == kok_v, (
        f"CHANGELOG en üst sürüm ({basliklar[0]}) ile __version__ ({kok_v}) uyuşmuyor"
    )


def test_ikiz_depo_paritesi():
    """Aynı sürümü beyan eden ikiz depo kopyası byte-eşit olmalı."""
    if os.environ.get("SYNCLAVE_IKIZ_ZORUNLU", "1") == "0":
        pytest.skip("ikiz depo parite kontrolü kapatıldı (SYNCLAVE_IKIZ_ZORUNLU=0)")
    ikiz_kok = Path("/root/cumulus-sync-motor/sync_motor.py")
    if not ikiz_kok.is_file():
        pytest.skip("ikiz depo yok (bu makinede private kopya kurulu değil)")
    ikiz_metin = _oku(ikiz_kok)
    yerel_metin = _oku(MODUL)
    if _surum(ikiz_metin) != _surum(yerel_metin):
        pytest.skip(f"ikiz depo farklı sürümde ({_surum(ikiz_metin)}) — ayrı iş")
    assert ikiz_metin == yerel_metin, (
        "ikiz depo (private) kök kopyası ayrıştı — kurulum adımı atlanmış olabilir"
    )
