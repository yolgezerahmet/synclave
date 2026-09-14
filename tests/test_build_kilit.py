#!/usr/bin/env python3
"""Build kilidi (flock) kapısı testleri — v2.7.2.

NEDEN: 14 Eyl 2026'da twin repo'da (private `cumulus-sync-motor`) build-gate
flock düzeltmesi commit edildi (c6050284) ama public repo'ya (`hermes-sync`)
taşınmadı → `test_kopya_parite.py::test_ikiz_depo_paritesi` sapmayı yakaladı
(sync_motor.py kök kopyaları ayrıştı, 1 failed / 375 passed).

Taşıma sırasında İKİ GERÇEK KUSUR ölçüldü ve kapatıldı:

D1 — ZAMAN AŞIMI YARIŞI (fail-closed ihlali): `flock -w 1800` ile
     `subprocess timeout=1800` EŞİT olduğunda flock 75 döndüremeden subprocess
     kill edilir. run_cmd TimeoutExpired'da ("timeout", -1) döner; çıktıda
     "RC=" bulunmaz → build_rc = -1 → `return False` → PUSH BLOKE. Yani
     düzeltmenin engellemeye çalıştığı SAHTE FAIL geri geliyordu.
     Kapı: subprocess zaman aşımı > kilit beklemesi (BUILD_GRACE > 0).

D2 — RC AYRIŞTIRMA ÇÖKMESİ: cmd.exe'de `$?` genişlemez → çıktı satırı
     "RC=$?" olur; korumasız `int("$?")` yakalanmamış ValueError fırlatır ve
     motor çöker (Windows'ta push yolu). Kapatıldı: ayrıştırma fail-closed —
     sayı olmayan RC doğrulanmış sayılmaz (FAIL), çökme yok.

Kapsam: (1) kilit dolu rc=75 → atla (True) + uyarı, (2) rc!=0 → False (push
durur), (3) rc=0 → True, (4) bozuk RC → çökme yok + fail-closed, (5) komut
flock taşır ve build komutu retry EDİLMEZ, (6) bozuk env güvenli varsayılana
düşer.
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import synclave.sync_motor as sm


class _RunKaydi:
    """run_cmd yerine geçen sahte çağrı — komut/zaman aşımını kaydeder."""

    def __init__(self, out):
        self.out = out
        self.calls = []

    def __call__(self, cmd, timeout=None, shell=False, retries=0):
        self.calls.append({"cmd": cmd, "timeout": timeout, "shell": shell})
        return self.out, 0


@pytest.fixture
def kernel_cfg(tmp_path):
    """Makefile taşıyan geçici kernel dizini (verify_build kapısı geçsin)."""
    (tmp_path / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
    return {"dirs": {"kernel": {"paths": [str(tmp_path)]}}}


def _kayit(monkeypatch, out):
    k = _RunKaydi(out)
    monkeypatch.setattr(sm, "run_cmd", k)
    return k


# ── (1) Kilit dolu → doğrulama atlanır, push engellenmez ────────

def test_kilit_dolu_atlanir(monkeypatch, kernel_cfg, caplog):
    caplog.set_level(logging.WARNING)
    _kayit(monkeypatch, "make: *** ...\nRC=75\n")
    assert sm.verify_build(kernel_cfg) is True
    assert any("kilit dolu" in r.message for r in caplog.records), (
        "rc=75 atlaması loglanmalı (sonraki koşuda yeniden denenir)")


# ── (2) Gerçek build hatası → push durur (fail-closed) ──────────

def test_build_hatasi_kapati_kapatir(monkeypatch, kernel_cfg, caplog):
    caplog.set_level(logging.ERROR)
    _kayit(monkeypatch, "error: 'x' undeclared\nRC=1\n")
    assert sm.verify_build(kernel_cfg) is False
    assert any("BAŞARISIZ" in r.message for r in caplog.records)


def test_build_pass(monkeypatch, kernel_cfg):
    _kayit(monkeypatch, "ARM ELF 318KB\nRC=0\n")
    assert sm.verify_build(kernel_cfg) is True


# ── (3) D2: sayı olmayan RC → çökme yok, fail-closed ────────────

@pytest.mark.parametrize("satir", ["RC=$?", "RC=", "RC=abc"])
def test_bozuk_rc_cokme_uretmez(monkeypatch, kernel_cfg, caplog, satir):
    caplog.set_level(logging.WARNING)
    _kayit(monkeypatch, f"cikti\n{satir}\n")
    # Eski kod: int("$?") → yakalanmamış ValueError. Yeni kod: fail-closed False.
    assert sm.verify_build(kernel_cfg) is False


def test_rc_satiri_yok_fail_closed(monkeypatch, kernel_cfg):
    # TimeoutExpired vakası: run_cmd ("timeout", -1) döner → RC= satırı yok.
    _kayit(monkeypatch, "timeout")
    assert sm.verify_build(kernel_cfg) is False


# ── (4) D1: zaman aşımı yarışı kapısı ───────────────────────────

def test_zaman_asimi_kilit_beklemesinden_buyuk(monkeypatch, kernel_cfg):
    monkeypatch.setenv("CUMULUS_LOCK_WAIT", "1500")
    monkeypatch.setenv("CUMULUS_BUILD_GRACE", "600")
    k = _kayit(monkeypatch, "RC=0\n")
    assert sm.verify_build(kernel_cfg) is True
    bekleme, zamasimi = sm._build_zaman_asimlari()
    assert zamasimi > bekleme, (
        "subprocess zaman aşımı kilit beklemesinden büyük olmalı — eşitlikte "
        "rc=75 yolu tetiklenemez ve SAHTE FAIL üretir (D1)")
    assert k.calls[0]["timeout"] == zamasimi
    assert k.calls[0]["timeout"] > 1500


def test_varsayilan_zaman_asimlari():
    for ad in ("CUMULUS_LOCK_WAIT", "CUMULUS_BUILD_GRACE"):
        os.environ.pop(ad, None)
    bekleme, zamasimi = sm._build_zaman_asimlari()
    assert (bekleme, zamasimi) == (1800, 2700)
    assert zamasimi > bekleme


@pytest.mark.parametrize("bozuk", ["abc", "0", "-5", ""])
def test_bozuk_env_guvenli_varsayilana_duser(monkeypatch, bozuk, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("CUMULUS_LOCK_WAIT", bozuk)
    bekleme, zamasimi = sm._build_zaman_asimlari()
    assert bekleme == 1800, f"{bozuk!r} sessizce kabul edilmemeli"
    assert zamasimi > bekleme


# ── (5) Komut biçimi: flock var, retry YOK ──────────────────────

def test_komut_flock_tasir_ve_retry_edilmez(monkeypatch, kernel_cfg):
    k = _kayit(monkeypatch, "RC=0\n")
    sm.verify_build(kernel_cfg)
    cmd = k.calls[0]["cmd"]
    assert "flock" in cmd and "-E 75" in cmd
    assert sm.BUILD_LOCK in cmd
    assert "make clean" in cmd and "bash -c" in cmd
    # Build komutu okuma DEĞİL → run_cmd retry politikası uygulanmaz
    # (yazma/yan etkili komuta retry YASAK — çift `make clean` riski).
    assert sm._is_idempotent_read(cmd) is False


def test_kernel_ayi_yoksa_dogrulama_atlanir():
    assert sm.verify_build({"dirs": {}}) is True
