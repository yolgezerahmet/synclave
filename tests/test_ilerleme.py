#!/usr/bin/env python3
"""İlerleme yayını testleri — sync_progress.json (panel/agent-status okur).

Neden (13 Eyl 2026): panel ve `agent-status` bir yedeğin hangi node'da olduğunu,
yüzdeyi ve ETA'yı göremiyordu; koşu boyunca tek görünür kanıt terminal çıktısıydı.
İlerleme yayını YAN KANALDIR: yazma hatası bir koşuyu DÜŞÜREMEZ, dosya atomik
yazılır (okuyucu yarım JSON görmez).
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import synclave.sync_motor as sm

REPO = Path(__file__).resolve().parent.parent
KAYNAK = (REPO / "sync_motor.py").read_text(encoding="utf-8")


@pytest.fixture
def yol(tmp_path, monkeypatch):
    p = tmp_path / "state" / "sync_progress.json"
    monkeypatch.setattr(sm, "_PROGRESS_PATH", str(p))
    return p


def _oku(p):
    return json.loads(p.read_text(encoding="utf-8"))


def test_progress_start_sifirlar(yol):
    sm.progress_start("backup", ["hermes", "kernel", "pcb"])
    d = _oku(yol)
    assert d["running"] is True
    assert d["cmd"] == "backup"
    assert d["total"] == 3 and d["done"] == 0 and d["percent"] == 0
    assert d["nodes_done"] == {} and d["error"] is None and d["current"] is None
    assert d["updated"], "zaman damgası yok"


def test_progress_step_yuzde_ve_eta(yol, monkeypatch):
    monkeypatch.setattr(sm.time, "time", lambda: 160.0)
    sm.progress_start("backup", ["a", "b", "c", "d"])
    sm.progress_step(3, 4, "c", started=100.0, nodes_done={"a": "ok", "b": "ok"})
    d = _oku(yol)
    assert d["done"] == 2 and d["percent"] == 50      # 2/4 iş bitti
    assert d["current"] == "c" and d["total"] == 4
    assert d["elapsed_s"] == 60
    assert d["eta_s"] == 60                            # (60/2) * 2 kalan iş
    assert d["nodes_done"] == {"a": "ok", "b": "ok"}


def test_progress_step_ilk_node_eta_yok(yol, monkeypatch):
    monkeypatch.setattr(sm.time, "time", lambda: 130.0)
    sm.progress_step(1, 3, "a", started=100.0, nodes_done={})
    d = _oku(yol)
    assert d["done"] == 0 and d["percent"] == 0 and d["eta_s"] is None


def test_progress_step_toplam_sifir_cokmez(yol, monkeypatch):
    monkeypatch.setattr(sm.time, "time", lambda: 100.0)
    sm.progress_step(1, 0, "x", started=100.0, nodes_done={})
    assert _oku(yol)["percent"] == 0


def test_progress_node_done_cagiranin_mapini_bozmaz(yol):
    benim = {"a": "ok"}
    sm.progress_node_done("b", "atlandı", benim)
    assert benim == {"a": "ok"}, "çağıranın nodes_done map'i yerinde değişti"
    d = _oku(yol)
    assert d["nodes_done"] == {"a": "ok", "b": "atlandı"}
    assert d["last_node"] == "b" and d["last_status"] == "atlandı"


def test_progress_finish_basari(yol):
    sm.progress_start("backup", ["a"])
    sm.progress_finish(ok=True)
    d = _oku(yol)
    assert d["running"] is False and d["percent"] == 100
    assert d["current"] is None and d["finished"]


def test_progress_finish_hata(yol):
    sm.progress_start("backup", ["a"])
    sm.progress_finish(ok=False, error="a: rclone rc=5")
    d = _oku(yol)
    assert d["running"] is False and d["percent"] is None
    assert d["error"] == "a: rclone rc=5"


def test_alanlar_birikerek_korunur(yol):
    """start → step → node_done → finish zincirinde önceki alanlar kaybolmaz."""
    sm.progress_start("backup", ["a", "b"])
    sm.progress_step(1, 2, "a", started=0.0, nodes_done={})
    sm.progress_node_done("a", "ok", {})
    sm.progress_finish(ok=True)
    d = _oku(yol)
    assert d["cmd"] == "backup" and d["total"] == 2
    assert d["nodes_done"] == {"a": "ok"} and d["running"] is False


def test_bozuk_json_uzerine_yazilir(yol):
    yol.parent.mkdir(parents=True, exist_ok=True)
    yol.write_text("{bozuk json,,", encoding="utf-8")
    sm.progress_start("backup", ["a"])
    d = _oku(yol)
    assert d["total"] == 1, "bozuk dosya kurtarılamadı"


def test_yazma_hatasi_istisna_yukseltmez(tmp_path, monkeypatch):
    """Yol dosya olarak işgal edilmişse (makedirs OSError) sessiz kalmalı."""
    isgal = tmp_path / "sync_progress.json"
    isgal.write_text("x", encoding="utf-8")
    monkeypatch.setattr(sm, "_PROGRESS_PATH", str(isgal / "alt" / "p.json"))
    sm.progress_start("backup", ["a"])          # istisna YÜKSELMEMELİ
    sm.progress_finish(ok=True)


def test_gecici_dosya_kalmaz(yol):
    sm.progress_start("backup", ["a"])
    sm.progress_step(1, 1, "a", started=0.0, nodes_done={})
    assert not Path(str(yol) + ".tmp").exists(), "atomik yazımın .tmp dosyası kaldı"


def test_cmd_backup_enstrumante_edilmis():
    """cmd_backup gerçekten start/step/node_done/finish çağırıyor mu (kaynak kapısı)?"""
    govde = KAYNAK.split("def cmd_backup(cfg, node=None, hub=None, dry_run=False):", 1)[1]
    govde = govde.split("\ndef ", 1)[0]
    for cagri in ("progress_start(\"backup\", nodes)", "progress_step(_p_i, len(nodes)",
                  "progress_node_done(", "progress_finish(ok=(_p_fail is None)"):
        assert cagri in govde, f"cmd_backup içinde eksik: {cagri}"
    # finish finally içinde olmalı (hata yolunda da yayın kapanır)
    kuyruk = govde[govde.index("    finally:"):]
    assert "progress_finish(" in kuyruk, "progress_finish finally içinde değil"


def test_progress_finish_yalniz_bir_cagri_yeri():
    """Yanlış fonksiyona sızmış progress_finish çağrısı = NameError (sessiz risk)."""
    assert KAYNAK.count("progress_finish(ok=") == 2, (
        "beklenen: 1 tanım + 1 çağrı (cmd_backup)")
