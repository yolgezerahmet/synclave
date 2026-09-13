#!/usr/bin/env python3
"""Uçtan uca kanıt: GDrive pull yolunda hafıza birleştirme (gerçek çağrı yeri).

`memory_merge_append` birim testleri doğru olsa bile ÇAĞRI YERİ yanlış olabilir
(bu deponun 11 Eyl dersi: doğru fonksiyon + kopuk/yanlış yol). Bu test gerçek
`sm.gdrive_pull_latest` akışını çalıştırır: rclone lsd → rclone copy (mock,
GDrive'a YAZILMAZ) → gerçek tar.gz açma → çakışma/birleştirme dalları.

Kanıtlanan:
  (a) iki makinenin § kayıtları BİRLEŞİR (kayıp yok, kısa kayıt korunur),
  (b) bu durumda .conflict kopyası ÜRETİLMEZ,
  (c) uzak ⊆ yerel ise yine kopya üretilmez,
  (d) gerçek çakışmada (uzak karar verilemez) yerel korunur + .conflict yazılır,
  (e) hafıza DIŞI dosyalarda eski davranış aynen sürer.
"""
import os
import socket
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import synclave.sync_motor as sm

A = "H1: GDrive pull yolu 12 saniyede tamamlandı, kayıt A uzun."
B = "H2: yedek rotasyonu 30 dakikalık retry-lock ile tamamlandı, kayıt B uzun."
KISA = "kısa kayıt"                     # eski filtre (len>20) bunu silerdi

LS_OUT = "   -1 2026-09-14 00:14:10        -1 20260914_001410\n"


def _tar_kur(tmpx: Path, node: str, dosyalar):
    """{ad: içerik} → {tmpx}/sync_pull_<node>/<node>.tar.gz (rclone copy taklidi)."""
    d = tmpx / f"sync_pull_{node}"
    d.mkdir(parents=True, exist_ok=True)
    pkg = d / f"{node}.tar.gz"
    with tarfile.open(pkg, "w:gz") as tf:
        for ad, icerik in dosyalar.items():
            gecici = d / f"_kaynak_{ad.replace('/', '_')}"
            gecici.write_text(icerik, encoding="utf-8")
            tf.add(gecici, arcname=ad)
    return pkg


@pytest.fixture
def ortam(tmp_path, monkeypatch):
    """Gerçek pull akışı için izole ortam: hedef dizin + sahte temp + sahte rclone."""
    hedef = tmp_path / "node_dizini"
    hedef.mkdir()
    tmpx = tmp_path / "tmpx"
    tmpx.mkdir()
    monkeypatch.setattr(sm, "_platform_temp_dir", lambda: str(tmpx))
    monkeypatch.setattr(sm, "rclone_available", lambda: True)
    monkeypatch.setattr(sm, "detect_machine", lambda *a, **k: "TEST")
    monkeypatch.setattr(sm, "run_cmd",
                        lambda cmd, **kw: (LS_OUT, 0) if cmd.startswith("rclone lsd")
                        else ("", 0))          # copy: GDrive'a YAZMA, rc=0
    cfg = {"machine": "TEST",
           "gdrive": {"versioned_dir": "gdrive:test-backups/versiyonlu"},
           "dirs": {"hermes": {"path": str(hedef)}}}
    return cfg, hedef, tmpx


def _kayitlar(p: Path):
    return sm.memory_records(p.read_text(encoding="utf-8"))


def test_pull_hafiza_birlesir_cakisma_kopyasi_olmaz(ortam):
    cfg, hedef, tmpx = ortam
    (hedef / "MEMORY.md").write_text(f"{A}\n§\n{KISA}\n", encoding="utf-8")
    (hedef / "config.json").write_text("yerel içerik\n", encoding="utf-8")
    _tar_kur(tmpx, "hermes", {"MEMORY.md": f"{A}\n§\n{B}\n",
                              "config.json": "uzak içerik farklı\n"})

    assert sm.gdrive_pull_latest(cfg, "hermes") is True

    kayitlar = _kayitlar(hedef / "MEMORY.md")
    assert A in kayitlar and B in kayitlar, "iki makinenin kaydı birleşmedi"
    assert KISA in kayitlar, "birleştirme kısa kaydı sildi"
    assert not list(hedef.glob("MEMORY.md.conflict.*")), "hafıza için kopya üretildi"
    # (e) hafıza dışı dosyada eski davranış: yerel korunur + kopya yazılır
    assert (hedef / "config.json").read_text(encoding="utf-8") == "yerel içerik\n"
    assert list(hedef.glob("config.json.conflict.*")), "hafıza dışı çakışma korunmadı"


def test_pull_uzak_alt_kume_ise_kopya_yok(ortam):
    """Uzak, yerelin alt kümesi (eski sürüm): kopya ÜRETİLMEZ, dosya değişmez."""
    cfg, hedef, tmpx = ortam
    (hedef / "MEMORY.md").write_text(f"{A}\n§\n{B}\n", encoding="utf-8")
    onceki = (hedef / "MEMORY.md").read_bytes()
    _tar_kur(tmpx, "hermes", {"MEMORY.md": f"{A}\n"})

    assert sm.gdrive_pull_latest(cfg, "hermes") is True

    assert (hedef / "MEMORY.md").read_bytes() == onceki
    assert not list(hedef.glob("MEMORY.md.conflict.*")), (
        "uzak ⊆ yerel iken kopya üretildi — kopya birikimi sürer")


def test_pull_karar_verilemezse_yerel_korunur_ve_kopya_yazilir(ortam):
    """Fail-closed: uzakta § kaydı yok → birleştirme yok, yerel korunur, kopya yazılır."""
    cfg, hedef, tmpx = ortam
    (hedef / "MEMORY.md").write_text(f"{A}\n§\n{B}\n", encoding="utf-8")
    _tar_kur(tmpx, "hermes", {"MEMORY.md": "\n   \n"})

    assert sm.gdrive_pull_latest(cfg, "hermes") is True

    assert _kayitlar(hedef / "MEMORY.md") == [A, B], "yerel içerik bozuldu"
    assert list(hedef.glob("MEMORY.md.conflict.*")), "fail-closed kopyası yazılmadı"


def test_pull_yeni_dosya_normal_yazilir(ortam):
    """Hedefte olmayan dosya (hafıza dâhil) birleştirme akışına girmez."""
    cfg, hedef, tmpx = ortam
    _tar_kur(tmpx, "hermes", {"MEMORY.md": f"{A}\n§\n{B}\n", "yeni.txt": "yeni\n"})

    assert sm.gdrive_pull_latest(cfg, "hermes") is True

    assert _kayitlar(hedef / "MEMORY.md") == [A, B]
    assert (hedef / "yeni.txt").read_text(encoding="utf-8") == "yeni\n"
