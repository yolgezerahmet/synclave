#!/usr/bin/env python3
"""Kopya paritesi kapısı — kök kopya ile paket kopyası ayrışmamalı (v2.5.0).

Neden: 11 Eyl 2026'da ON ÜÇ ortak modülün dördü kök ile paket kopyası arasında
AYRIŞMIŞTI (sync_motor.py 2.2.0 ↔ 2.3.2, node_agent.py, inbox_worker.py,
conversation_bridge.py, gpu_agent.py). Üretim kök kopyası (cron'un çalıştırdığı
dosya) v2.3.2'nin GDrive düzeltmesini taşımıyordu; `| wc -l` / `| tail -1`
pipeline'ları rclone rc'sini yutuyor, retry hiç tetiklenmiyordu. Testler yalnız
paket kopyasını okuduğu için sapma GÖRÜNMEZDİ (yeşil test + bozuk üretim).

Bu kapı üç şeyi zorlar:
  1. Kökte ve `synclave/` içinde bulunan HER ortak .py dosyası byte-eşit.
  2. sync_motor.py kritik sembolleri + pipeline yasağı + tek `__version__`
     (CHANGELOG'un en üst sürümüyle uyumlu).
  3. İkiz depo (private `cumulus-sync-motor`) varsa aynı sürümü beyan ettiği
     sürece kopyaları da byte-eşit. `SYNCLAVE_IKIZ_ZORUNLU=0` ile kapatılır.
"""
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
KOK = REPO / "sync_motor.py"
MODUL = REPO / "synclave" / "sync_motor.py"
IKIZ_REPO = Path("/root/cumulus-sync-motor")

# sync_motor.py kanonik içerikte bulunması ZORUNLU işaretler (sapma dedektörü).
ZORUNLU_SEMBOLLER = (
    "def rclone_read(",
    "def _lsd_names(",
    "def unique_a2a_nodes(",
    "def _sha_cached(",
    "def _log_event(",
    "def cmd_identity(",
    "def _is_idempotent_read(",
    "_SURUM_ADI_RE",
    '"listremotes"',
    '"direxists"',
    '"about"',
)


def _oku(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _ikiz_kapali() -> bool:
    return os.environ.get("SYNCLAVE_IKIZ_ZORUNLU", "1") == "0"


def _ortak_moduller(repo: Path) -> list:
    """Kökte VE pakette bulunan .py dosyaları (kopya olması gerekenler)."""
    pkg = repo / "synclave"
    return sorted(p.name for p in pkg.glob("*.py") if (repo / p.name).is_file())


def test_ayni_depo_kopyalari_byte_esit():
    """Kök (üretim) kopya ile paket kopyası ayrışırsa kırmızı — tüm ortak modüller."""
    ortak = _ortak_moduller(REPO)
    assert ortak, "ortak modül bulunamadı (dizin yapısı değişmiş)"
    ayri = []
    for ad in ortak:
        kok, modul = _oku(REPO / ad), _oku(REPO / "synclave" / ad)
        if kok != modul:
            satir = f"{ad}: kök {len(kok.splitlines())} / paket {len(modul.splitlines())} satır"
            for sem in ZORUNLU_SEMBOLLER:
                if (sem in kok) != (sem in modul):
                    satir += f" | yalnız birinde: {sem}"
            ayri.append(satir)
    assert not ayri, "kök ↔ paket kopyaları ayrıştı:\n" + "\n".join(ayri)


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


def test_paket_surumu_tek_kaynak_pyproject_paket_motor():
    """pyproject / paket __init__ / motor / CHANGELOG en üst sürümü AYNI olmalı.

    Neden (11 Eyl 2026 bulgusu — gerçek sapma): pyproject `1.0.2`, paket
    `__init__.__version__` `1.0.1`, `sync_motor.__version__` `2.5.0`, PyPI'da
    yayınlanmış en yüksek sürüm ise `2.4.1` idi. Sürümler ayrışınca iki gerçek
    sonuç doğar: (1) paket kendini yanlış sürümle tanıtır, (2) bu ağaçtan
    yayın yapılırsa sürüm numarası yayındaki `2.4.1`'in ALTINDA kalır ve
    `pip install synclave` yeni sürümü ÇEKMEZ (resolver en yükseği seçer).
    Bu kapı dört kaynağı tek değere bağlar.
    """
    paket_v = _surum(_oku(REPO / "synclave" / "__init__.py"))
    motor_v = _surum(_oku(KOK))
    pypro = _oku(REPO / "pyproject.toml")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pypro)
    assert m, "pyproject.toml'da [project] version bulunamadı"
    basliklar = re.findall(r"^## \[(\d+\.\d+\.\d+)\]",
                           _oku(REPO / "CHANGELOG.md"), re.M)
    assert basliklar, "CHANGELOG'da sürüm başlığı yok"
    degerler = {
        "pyproject": m.group(1),
        "paket __init__": paket_v,
        "sync_motor": motor_v,
        "CHANGELOG[0]": basliklar[0],
    }
    assert len(set(degerler.values())) == 1, (
        f"sürüm kaynakları ayrıştı (tek kaynak olmalı): {degerler}"
    )


def test_ikiz_depo_paritesi():
    """Aynı sürümü beyan eden ikiz depo kopyaları byte-eşit olmalı (tüm ortak modüller)."""
    if _ikiz_kapali():
        pytest.skip("ikiz depo parite kontrolü kapatıldı (SYNCLAVE_IKIZ_ZORUNLU=0)")
    if not (IKIZ_REPO / "sync_motor.py").is_file():
        pytest.skip("ikiz depo yok (bu makinede private kopya kurulu değil)")
    if _surum(_oku(IKIZ_REPO / "sync_motor.py")) != _surum(_oku(MODUL)):
        pytest.skip(f"ikiz depo farklı sürümde — ayrı iş")
    ayri = []
    for ad in _ortak_moduller(REPO):
        ikiz = IKIZ_REPO / "synclave" / ad
        ikiz_kok = IKIZ_REPO / ad
        if not ikiz.is_file() or not ikiz_kok.is_file():
            continue
        if _oku(REPO / "synclave" / ad) != _oku(ikiz):
            ayri.append(f"{ad}: paket kopyaları ayrıştı")
        if _oku(REPO / ad) != _oku(ikiz_kok):
            ayri.append(f"{ad}: kök kopyaları ayrıştı")
    assert not ayri, "ikiz depo parite sapması:\n" + "\n".join(ayri)
