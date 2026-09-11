#!/usr/bin/env python3
"""Self-check betikleri artık otomatik koşuyor + suite toplanabilirliği kapısı.

Neden (11 Eyl 2026 — iki gerçek bulgu):

1. **Koşmayan doğrulama (public):** kökteki altı kontrol betiği
   (`test_memory_cmd`, `test_memory_fixes`, `test_retention_cmd`, `test_sec_fixes`,
   `test_sync_memory`, `test_versions_cmd`) `python3 <betik>` ile elle
   koşulabilen, toplam 74 doğrulama içeren self-check betikleriydi; ama hiçbir
   runner bunları çağırmıyordu (`pytest tests/` kapsamı dışındaydılar).
   Yeşil-ama-hiç-koşmadı sınıfı.

2. **Suite hiç koşamıyordu (private):** aynı betikler private kopyada
   `tests/manual/` altındadır. `pytest tests/` bunları COLLECT ediyor, betikler
   modül seviyesinde `sys.exit(0)` çağırdığı için pytest
   `INTERNALERROR ... SystemExit: 0` ile çöküyordu → private depoda hiçbir test
   koşmuyordu (`no tests collected`). Çözüm: `tests/manual/conftest.py` içinde
   `collect_ignore_glob` (pytest varsayılanları EZİLMEZ); betikler yine bu kapı
   üzerinden subprocess ile ÇALIŞTIRILIR — doğrulama kaybı yok.

Not: betikler public'te kökte, private'te `tests/manual/` altında bulunur;
iki konumda birden bulunması sapma sayılır ve kırmızı olur.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Çalıştırılması gereken self-check betikleri (yeni betik eklenirse buraya da eklenir).
KOK_SELF_CHECK = (
    "test_memory_cmd.py",
    "test_memory_fixes.py",
    "test_retention_cmd.py",
    "test_sec_fixes.py",
    "test_sync_memory.py",
    "test_versions_cmd.py",
)

# Betiklerin aranacağı konumlar: public (kök) ve private (tests/manual).
ARAMA_DIZINLERI = (Path("."), Path("tests") / "manual")

KOSU_TIMEOUT_S = 180

# Başarı işareti: "<N> PASS" veya "<N> test PASS" (yalnız 'PASS' alt-dizesi
# yetmez — hata mesajında geçen PASS yanlış pozitif üretirdi).
_BASARI_RE = re.compile(r"\b\d+\s*(?:test\s*)?PASS\b")
_HATA_RE = re.compile(r"\bFAIL\b|Traceback|NameError|INTERNALERROR|ModuleNotFoundError")


def _betik_yollari(ad):
    return [REPO / d / ad for d in ARAMA_DIZINLERI if (REPO / d / ad).is_file()]


def _mevcut_betikler():
    return [b for b in KOK_SELF_CHECK if _betik_yollari(b)]


@pytest.mark.parametrize("betik", KOK_SELF_CHECK)
def test_self_check_betigi_gecer(betik):
    """Betik rc=0 ile biter ve '<N> PASS' özeti basar (aksi halde regresyon)."""
    yollar = _betik_yollari(betik)
    if not yollar:
        mevcut = _mevcut_betikler()
        # Hiçbiri yoksa: ikiz/private klon olabilir → skip.
        # Public ağaçta (bu adı taşımayan her kopya) hiç betik yoksa: kırmızı —
        # silinme/runner bozulması sessizce yeşile dönmesin.
        if not mevcut and REPO.name == "cumulus-sync-motor":
            pytest.skip("self-check betikleri ikiz (private) kopyada taşınmıyor")
        pytest.fail(
            f"{betik} bulunamadı — self-check listesi kısmen/tamamen yok "
            f"({len(mevcut)}/{len(KOK_SELF_CHECK)}); kapı kör kalmasın"
        )
    assert len(yollar) == 1, f"{betik} iki konumda birden var (sapma): {yollar}"
    yol = yollar[0]

    r = subprocess.run(
        [sys.executable, str(yol)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=KOSU_TIMEOUT_S,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    cikti = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, (
        f"{betik} rc={r.returncode} — son satırlar:\n" + "\n".join(cikti.splitlines()[-15:])
    )
    assert _BASARI_RE.search(cikti), (
        f"{betik} rc=0 ama '<N> PASS' özeti yok (sessiz geçiş):\n{cikti[-400:]}"
    )
    assert not _HATA_RE.search(cikti), (
        f"{betik} rc=0 ama çıktıda hata işareti var:\n{cikti[-400:]}"
    )


def test_self_check_listesi_bos_degil():
    """Liste boşaltılırsa kapı sessizce yeşile dönmesin."""
    assert len(KOK_SELF_CHECK) >= 6


def test_self_check_betikleri_ikiz_depo_ile_ayni():
    """Public kök ↔ private `tests/manual/` kopyaları byte-eşit olmalı.

    Gerçek sapma (11 Eyl 2026): iki depodaki kopyalar FARKLI sys.path bloğu
    taşıyordu ve private kopyadaki blok bir fazla `dirname` ile YANLIŞ dizini
    gösterdiği için betikler private depoda hiç koşamıyordu (`ModuleNotFoundError`).
    Ayrışma görünmezdi çünkü iki depoda da hiçbir runner bu betikleri çağırmıyordu.
    """
    if os.environ.get("SYNCLAVE_IKIZ_ZORUNLU", "1") == "0":
        pytest.skip("ikiz depo parite kontrolü kapatıldı")
    # Kontrol yalnız kök kopyaları taşıyan depoda anlamlı (public).
    kokte = [b for b in KOK_SELF_CHECK if (REPO / b).is_file()]
    if not kokte:
        pytest.skip("bu kopya kökte self-check betiği taşımıyor (twin/private klon)")

    ikiz_manual = REPO.parent / "cumulus-sync-motor" / "tests" / "manual"
    if not ikiz_manual.is_dir():
        pytest.skip("ikiz depo bu makinede kurulu değil")
    ayri = [b for b in kokte
            if (ikiz_manual / b).is_file()
            and (REPO / b).read_bytes() != (ikiz_manual / b).read_bytes()]
    assert not ayri, f"self-check betikleri iki depoda ayrıştı: {ayri}"


def test_tests_dizini_toplanabilir():
    """`pytest tests/` toplama hatası vermemeli (private `tests/manual` INTERNALERROR'ı).

    Gerçek bulgu: `tests/manual/` altındaki betikler modül seviyesinde
    `sys.exit(0)` çağırır; pytest bunları import edince süreç çöküyor ve
    depoda HİÇ test koşmuyordu. `tests/manual/conftest.py` içindeki
    `collect_ignore_glob` otomatik toplamayı kapatmalı (betikler yukarıdaki
    kapıdan subprocess ile koşar).
    """
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    cikti = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, (
        "pytest tests/ toplama hatası verdi (INTERNALERROR):\n"
        + "\n".join(cikti.splitlines()[-15:])
    )
    assert "INTERNALERROR" not in cikti, cikti[-600:]
    assert "no tests collected" not in cikti, cikti[-600:]
