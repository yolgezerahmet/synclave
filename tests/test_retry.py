#!/usr/bin/env python3
"""Retry/backoff birim testleri — rclone hata dayanıklılığı (v2.1.1).

Kapsam:
- sync_common_knowledge._run_rclone: okuma komutunda geçici hata → 1 retry
  → başarı; yazma komutuna retry YOK; timeout varsayılanı 180s.
- sync_motor.run_cmd: retries>0 sadece idempotent OKUMA komutlarında;
  yazma komutlarına ASLA retry; hata logu 'sync hata:' formatı.
"""
import json
import os
import re
import subprocess
import sys
import types
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import synclave.sync_common_knowledge as ck
import synclave.sync_motor as sm


class _FakeResult:
    def __init__(self, rc, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _make_fake_run(script, calls):
    """script: [(rc, out, err), ...] — her çağrıda sırayla döner.

    Sonuç dizisi biterse son eleman tekrar kullanılır.
    """

    def fake_run(cmd_args, capture_output=True, text=True, errors="replace",
                 timeout=60, **kw):
        calls.append(list(cmd_args))
        rc, out, err = script[min(len(calls) - 1, len(script) - 1)]
        return _FakeResult(rc, out, err)

    return fake_run


@pytest.fixture
def no_sleep(monkeypatch):
    """Retry beklemesini sıfırla (test hızı)."""
    monkeypatch.setattr(time, "sleep", lambda s: None)


# ─── sync_common_knowledge._run_rclone ──────────────────────────

def test_run_rclone_read_retry_success(monkeypatch, no_sleep):
    calls = []
    fake = _make_fake_run([(1, "", "connection reset"), (0, '{"ok":1}', "")], calls)
    monkeypatch.setattr(ck, "subprocess", types.SimpleNamespace(run=fake))
    rc, out, err = ck._run_rclone(["cat", "gdrive:hermes-sync/hahmet/shared/state.json"])
    assert rc == 0
    assert json.loads(out) == {"ok": 1}
    assert len(calls) == 2  # hata → retry → başarı


def test_run_rclone_read_retry_then_fail(monkeypatch, no_sleep):
    calls = []
    fake = _make_fake_run([(1, "", "HTTP 503 Service Unavailable"),
                           (1, "", "HTTP 503 Service Unavailable")], calls)
    monkeypatch.setattr(ck, "subprocess", types.SimpleNamespace(run=fake))
    rc, out, err = ck._run_rclone(["lsf", "gdrive:hermes-sync/hahmet/shared/tasks", "--files-only"])
    assert rc != 0
    assert len(calls) == 2  # yalnız 1 retry — üçüncü çağrı YOK
    assert "sync hata:" in err
    assert "rc=1" in err
    assert "retry=1" in err


def test_run_rclone_write_no_retry(monkeypatch, no_sleep):
    calls = []
    fake = _make_fake_run([(1, "", "connection reset")], calls)
    monkeypatch.setattr(ck, "subprocess", types.SimpleNamespace(run=fake))
    rc, out, err = ck._run_rclone(["copyto", "/tmp/x.json",
                                   "gdrive:hermes-sync/hahmet/shared/state.json"])
    assert rc != 0
    assert len(calls) == 1  # yazma komutu → retry YOK
    assert "sync hata:" in err


def test_run_rclone_not_found_no_retry(monkeypatch, no_sleep):
    """'not found' geçici hata DEĞİL — retry yok (fail-closed ayrımı korunur)."""
    calls = []
    fake = _make_fake_run([(1, "", "directory not found")], calls)
    monkeypatch.setattr(ck, "subprocess", types.SimpleNamespace(run=fake))
    rc, out, err = ck._run_rclone(["cat", "gdrive:hermes-sync/hahmet/none/shared/state.json"])
    assert rc != 0
    assert len(calls) == 1
    assert "sync hata:" in err


def test_run_rclone_fatal_exception_no_retry(monkeypatch, no_sleep):
    """OceanAPI #4: rc==-1 kalıcı exception (rclone executable yok) → retry YOK."""
    calls = []
    fake = _make_fake_run([(-1, "", "rclone: command not found")], calls)
    monkeypatch.setattr(ck, "subprocess", types.SimpleNamespace(run=fake))
    rc, out, err = ck._run_rclone(["cat", "gdrive:hermes-sync/hahmet/shared/state.json"])
    assert rc != 0
    assert len(calls) == 1  # kalıcı hata → retry YOK
    assert "sync hata:" in err


def test_run_rclone_default_timeout_180():
    import inspect
    sig = inspect.signature(ck._run_rclone)
    assert sig.parameters["timeout"].default == 180
    # tüm rclone okumaları 180s altında — görev spesifikasyonu (120→180)
    sig2 = inspect.signature(ck._read_remote_json)
    assert sig2.parameters["timeout"].default == 180


# ─── sync_motor.run_cmd ─────────────────────────────────────────

def test_run_cmd_read_retry_success(monkeypatch, no_sleep):
    calls = []
    fake = _make_fake_run([(-1, "timeout", ""), (0, "saglikli", "")], calls)
    monkeypatch.setattr(sm, "subprocess", types.SimpleNamespace(run=fake))
    out, rc = sm.run_cmd("rclone cat gdrive:x/y", retries=1)
    assert rc == 0
    assert out == "saglikli"
    assert len(calls) == 2


def test_run_cmd_write_never_retries(monkeypatch, no_sleep):
    calls = []
    fake = _make_fake_run([(1, "", "connection reset")], calls)
    monkeypatch.setattr(sm, "subprocess", types.SimpleNamespace(run=fake))
    # retries=1 verilse bile yazma komutu (copy) retry YAPMAZ
    out, rc = sm.run_cmd("rclone copy /tmp/a gdrive:hermes-sync/hahmet", retries=1)
    assert rc == 1
    assert len(calls) == 1


def test_run_cmd_write_with_readlike_token_no_retry(monkeypatch, no_sleep):
    """OceanAPI #2/#3: dosya adı okuma kelimesine benzese bile yazma retry YOK.

    'rclone copy status <dest>' — 'status' 2. token'da ama alt komut 'copy'
    (YAZMA) → can_retry=False olmalı.
    """
    calls = []
    fake = _make_fake_run([(1, "", "connection reset")], calls)
    monkeypatch.setattr(sm, "subprocess", types.SimpleNamespace(run=fake))
    out, rc = sm.run_cmd("rclone copy status gdrive:hermes-sync/hahmet", retries=1)
    assert rc == 1
    assert len(calls) == 1  # yazma → retry YOK
    assert not sm._is_idempotent_read("rclone copy status gdrive:x")
    assert not sm._is_idempotent_read("rclone move cat gdrive:x gdrive:y")


def test_run_cmd_fatal_exception_no_retry(monkeypatch, no_sleep):
    """OceanAPI #4: rc==-1 kalıcı exception (dosya yok) → retry YOK."""
    calls = []
    fake = _make_fake_run([(-1, "", "No such file or directory")], calls)
    monkeypatch.setattr(sm, "subprocess", types.SimpleNamespace(run=fake))
    out, rc = sm.run_cmd("rclone cat gdrive:x/y", retries=1)
    assert rc == -1
    assert len(calls) == 1  # kalıcı hata → retry YOK


def test_run_cmd_default_no_retry(monkeypatch, no_sleep):
    calls = []
    fake = _make_fake_run([(1, "", "HTTP 503")], calls)
    monkeypatch.setattr(sm, "subprocess", types.SimpleNamespace(run=fake))
    out, rc = sm.run_cmd("rclone cat gdrive:x/y")  # retries varsayılan 0
    assert rc == 1
    assert len(calls) == 1


def test_run_cmd_non_read_cmd_no_retry(monkeypatch, no_sleep):
    """'backup' yazma sayılır — ilk 3 token'da okuma yok → retry YOK."""
    calls = []
    fake = _make_fake_run([(-1, "timeout", "")], calls)
    monkeypatch.setattr(sm, "subprocess", types.SimpleNamespace(run=fake))
    out, rc = sm.run_cmd("rclone backup gdrive:x/y", retries=1)
    assert rc == -1
    assert len(calls) == 1


def test_run_cmd_retry_log_format(monkeypatch, no_sleep, caplog):
    """Log satırı: 'sync hata: <komut> rc=<rc> <süre>s retry=<n>'."""
    import logging
    calls = []
    fake = _make_fake_run([(-1, "timeout", ""), (0, "ok", "")], calls)
    monkeypatch.setattr(sm, "subprocess", types.SimpleNamespace(run=fake))
    with caplog.at_level(logging.WARNING, logger="sync_motor"):
        out, rc = sm.run_cmd("rclone cat gdrive:x/y", retries=1)
    assert rc == 0
    msgs = [r.getMessage() for r in caplog.records]
    retry_msg = [m for m in msgs if "retry=1/1" in m]
    assert retry_msg, f"retry log satırı bulunamadı: {msgs}"
    assert retry_msg[0].startswith("sync hata:")
    assert "rc=-1" in retry_msg[0]
    assert "s" in retry_msg[0]  # süre


def test_is_transient_classification():
    assert sm._is_transient_rc(-1, "")
    assert sm._is_transient_rc(1, "HTTP 503 Service Unavailable")
    assert sm._is_transient_rc(1, "connection reset by peer")
    assert sm._is_transient_rc(1, "i/o timeout")
    assert not sm._is_transient_rc(1, "directory not found")
    assert not sm._is_transient_rc(2, "file does not exist")
    # OceanAPI #4: kalıcı exception'lar geçici sayılmaz
    assert not sm._is_transient_rc(-1, "No such file or directory")
    assert not sm._is_transient_rc(-1, "rclone: command not found")
    assert not sm._is_transient_rc(-1, "Permission denied")
    assert ck._is_transient_rclone_error(-1, "")
    assert ck._is_transient_rclone_error(1, "HTTP 500 Internal Server Error")
    assert ck._is_transient_rclone_error(1, "connection refused")
    assert not ck._is_transient_rclone_error(1, "not found")
    assert not ck._is_transient_rclone_error(1, "invalid object name")
    assert not ck._is_transient_rclone_error(-1, "rclone: command not found")


# ─── v2.1.2: pipeline'sız GDrive okuma + GERÇEK rc + retry bağlama ─────
# Kapanan açık: `rclone lsd ... | tail -1` / `| wc -l` rc'yi tail/wc'den
# alıyordu → retry hiç tetiklenmiyor ve ağ hatası 'versiyon yok' gibi
# görünüyordu. Artık çıktı yerel parse edilir, rc gerçek rclone rc'sidir.

def test_lsd_names_parses_crlf_skips_blank_and_header():
    out = ("          -1 2026-09-01 13:27:04        -1 20260901_132704\r\n"
           "\r\n"
           "          -1 2026-09-02 01:39:11        -1 20260902_013911\r\n"
           "    -1 -1 -1 Name\n")
    assert sm._lsd_names(out) == ["20260901_132704", "20260902_013911"]


def test_lsd_names_empty_and_garbage():
    assert sm._lsd_names("") == []
    assert sm._lsd_names(None) == []
    assert sm._lsd_names("tek_token\n") == []          # tek token ad sayılmaz
    assert sm._lsd_names("   \n \t \n") == []


def test_lsd_names_keeps_spaces_in_name():
    """QCode denetimi #3: ad boşluk içeriyorsa son token almak adı kırpar."""
    out = "          -1 2026-09-01 13:27:04        -1 my backup dir\n"
    assert sm._lsd_names(out) == ["my backup dir"]


def test_listremotes_idempotent_read_exact_match():
    assert sm._is_idempotent_read("rclone listremotes") is True
    assert sm._is_idempotent_read("rclone direxists gdrive:x") is True
    # tam token eşleşmesi — benzer ad yanlış sınıflanmaz (OceanAPI #5)
    assert sm._is_idempotent_read("rclone listremotesX") is False
    # yazma komutu, içinde okuma token'ı geçse bile retry DIŞI
    assert sm._is_idempotent_read("rclone copy listremotes dest") is False
    assert sm._is_idempotent_read("rclone copyto direxists gdrive:x") is False


def test_run_cmd_listremotes_retries_on_transient(monkeypatch, no_sleep):
    calls = []
    monkeypatch.setattr(sm.subprocess, "run", _make_fake_run(
        [(1, "", "connection reset"), (0, "gdrive:\n", "")], calls))
    out, rc = sm.run_cmd("rclone listremotes", timeout=30, retries=1)
    assert rc == 0 and "gdrive:" in out
    assert len(calls) == 2          # retry gerçekten yapıldı


def test_run_cmd_write_with_read_token_no_retry(monkeypatch, no_sleep):
    calls = []
    monkeypatch.setattr(sm.subprocess, "run", _make_fake_run(
        [(1, "", "connection reset")], calls))
    out, rc = sm.run_cmd("rclone copyto /tmp/a gdrive:x", retries=1)
    assert rc == 1
    assert len(calls) == 1          # yazma komutuna retry YOK


def _pull_cfg():
    return {"machine": "T",
            "gdrive": {"versioned_dir": "gdrive:cumulusos-backups/versiyonlu"},
            "dirs": {"scripts": {"path": "/nonexistent"}}}


def test_gdrive_pull_latest_rc_error_not_reported_as_missing(monkeypatch, caplog):
    monkeypatch.setattr(sm, "rclone_available", lambda: True)
    monkeypatch.setattr(sm, "run_cmd", lambda *a, **k: ("", 1))
    with caplog.at_level("WARNING"):
        assert sm.gdrive_pull_latest(_pull_cfg(), "scripts") is False
    assert any("listesi alınamadı" in r.message for r in caplog.records)


def test_gdrive_pull_latest_empty_listing_reports_no_version(monkeypatch, caplog):
    monkeypatch.setattr(sm, "rclone_available", lambda: True)
    monkeypatch.setattr(sm, "run_cmd", lambda *a, **k: ("", 0))
    with caplog.at_level("INFO"):
        assert sm.gdrive_pull_latest(_pull_cfg(), "scripts") is False
    assert any("versiyon yok" in r.message for r in caplog.records)


def test_gdrive_pull_latest_parses_latest_without_pipeline(monkeypatch):
    monkeypatch.setattr(sm, "rclone_available", lambda: True)
    calls = []
    ls_out = ("          -1 2026-09-01 13:27:04        -1 20260901_132704\n"
              "          -1 2026-09-02 01:39:11        -1 20260902_013911\n")

    def fake_run_cmd(cmd, timeout=60, shell=False, retries=0, **kw):
        calls.append((cmd, shell, retries))
        if cmd.startswith("rclone lsd"):
            return ls_out, 0
        return "", 1          # copy adımı bilinçli başarısız (GDrive'a yazmıyoruz)

    monkeypatch.setattr(sm, "run_cmd", fake_run_cmd)
    assert sm.gdrive_pull_latest(_pull_cfg(), "scripts") is False
    lsd_cmd, lsd_shell, lsd_retries = calls[0]
    assert "|" not in lsd_cmd and lsd_shell is False   # pipeline YOK
    assert lsd_retries == 1                            # retry açık
    assert "2>/dev/null" not in lsd_cmd
    assert "20260902_013911" in calls[1][0]            # en SON sürüm seçildi


def test_no_pipeline_in_gdrive_reads_source_guard():
    """Regresyon kapısı: okuma komutları pipeline'a geri dönmemeli (rc kaybı)."""
    src = Path(sm.__file__).read_text(encoding="utf-8")
    assert "rclone lsd" in src
    assert "2>/dev/null | wc -l" not in src
    assert "2>/dev/null | tail -1" not in src


def test_gdrive_pull_latest_cikti_sirasi_garanti_degil(monkeypatch):
    """rclone lsd çıktı SIRASI garanti değil → en yeni timestamp sözlük sırasıyla.

    Bağımsız denetim bulgusu (11 Eyl 2026): eski kod çıktının son satırını
    (`names[-1]`) en yeni sanıyordu; rclone sıralamayı garanti etmediği için
    karışık sıralı çıktıda YANLIŞ (eski) sürüm çekilebilirdi. Yeni seçim
    YYYYMMDD_HHMMSS biçimli adlar içinden `max()` — sıradan bağımsız.
    """
    monkeypatch.setattr(sm, "rclone_available", lambda: True)
    calls = []
    # Bilinçli KARIŞIK sıra + gürültü: boş satır, tek token'lu satır,
    # 'Name' başlıklı satır, biçimsiz dir adı, en ESKİ sürüm EN SONDA.
    ls_out = (
        "   -1 2026-09-02 01:39:11        -1 20260902_013911\r\n"
        "   -1 2026-09-10 19:17:33        -1 20260910_191733\r\n"
        "Name         -1 2026-09-11 07:00:00        -1 20260911_070000\r\n"
        "\r\n"
        "   -1 2026-09-01 13:27:04        -1 20260901_132704\n"
        "   -1 2026-09-05 08:00:00        -1 bozuk_dizin\n"
    )

    def fake_run_cmd(cmd, timeout=60, shell=False, retries=0, **kw):
        calls.append((cmd, shell, retries))
        if cmd.startswith("rclone lsd"):
            return ls_out, 0
        return "", 1          # copy adımı bilinçli başarısız (GDrive'a yazmıyoruz)

    monkeypatch.setattr(sm, "run_cmd", fake_run_cmd)
    assert sm.gdrive_pull_latest(_pull_cfg(), "scripts") is False
    # çıktının SON satırı değil, en BÜYÜK timestamp seçilmeli
    assert "20260910_191733" in calls[1][0]
    assert "20260901_132704" not in calls[1][0]


def test_gdrive_pull_latest_gecersiz_bicim_elener(monkeypatch):
    """Biçim TAM eşleşmeli: '20260901132704_' (alt çizgi sonda, 15 karakter) ELENİR.

    İkinci denetim turu bulgusu (11 Eyl 2026): `len(n)==15 and n.replace('_','').isdigit()`
    yalnız uzunluk sayıyordu → alt çizgi yanlış yerde olsa da geçiyordu ve `max()`
    ile seçilebiliyordu. `^\\d{8}_\\d{6}$` ile yapısal bozuk ad dışarıda kalır.
    """
    monkeypatch.setattr(sm, "rclone_available", lambda: True)
    calls = []
    # DİKKAT: bozuk ad, geçerli adlardan SÖZLÜK SIRASINDA DAHA BÜYÜK seçildi —
    # aksi hâlde `max()` zaten doğru adı seçerdi ve test kanıt olmazdı.
    ls_out = ("   -1 2026-09-11 07:00:00        -1 20260911070000_\r\n"
              "   -1 2026-09-02 01:39:11        -1 20260902_013911\n"
              "   -1 2026-09-03 02:00:00        -1 _20260903_020000\n")

    def fake_run_cmd(cmd, timeout=60, shell=False, retries=0, **kw):
        calls.append((cmd, shell, retries))
        if cmd.startswith("rclone lsd"):
            return ls_out, 0
        return "", 1

    monkeypatch.setattr(sm, "run_cmd", fake_run_cmd)
    assert sm.gdrive_pull_latest(_pull_cfg(), "scripts") is False
    assert "20260902_013911" in calls[1][0]       # tek geçerli ad seçildi
    assert "20260911070000_" not in calls[1][0]   # sözlük sırası büyük ama BOZUK ad seçilmedi
    assert "_20260903_020000" not in calls[1][0]


def test_gdrive_pull_latest_tamamen_gecersiz_liste_uyarir(monkeypatch, caplog):
    """Hiç geçerli timestamp dizini yoksa: retry edilmiş liste + warning (fail-closed)."""
    monkeypatch.setattr(sm, "rclone_available", lambda: True)
    monkeypatch.setattr(sm, "run_cmd",
                        lambda *a, **k: ("   -1 2026-09-05 08:00:00 -1 bozuk\n", 0))
    with caplog.at_level("WARNING"):
        assert sm.gdrive_pull_latest(_pull_cfg(), "scripts") is False
    assert any("geçersiz versiyon" in r.message for r in caplog.records)


# ─── v2.1.2: rclone_read — doğrudan subprocess OKUMALARI retry kapsamında ──

def _patch_subprocess(monkeypatch, mod, fake):
    """mod.subprocess'i sahte run + gerçek TimeoutExpired ile değiştir."""
    monkeypatch.setattr(mod, "subprocess",
                        types.SimpleNamespace(run=fake,
                                              TimeoutExpired=subprocess.TimeoutExpired))


def test_rclone_read_retry_success(monkeypatch, no_sleep):
    """Geçici hata (connection reset) → 1 retry (3s) → başarı."""
    calls = []
    fake = _make_fake_run([(1, "", "connection reset"), (0, "20260901.tar.gz\n", "")], calls)
    _patch_subprocess(monkeypatch, sm, fake)
    rc, out, err = sm.rclone_read(["lsf", "gdrive:hub/scripts", "--files-only"])
    assert rc == 0
    assert "20260901.tar.gz" in out
    assert len(calls) == 2                     # hata → retry → başarı
    assert calls[0][:2] == ["rclone", "lsf"]   # gerçek rclone komutu


def test_rclone_read_default_timeout_180_and_passed_through(monkeypatch, no_sleep):
    """Varsayılan timeout 180s (spec) ve subprocess'e gerçekten geçirilir."""
    import inspect
    assert inspect.signature(sm.rclone_read).parameters["timeout"].default == 180
    seen = {}

    def fake_run(cmd_args, capture_output=True, text=True, errors="replace",
                 timeout=60, **kw):
        seen["timeout"] = timeout
        return _FakeResult(0, "ok\n", "")

    _patch_subprocess(monkeypatch, sm, fake_run)
    rc, out, _ = sm.rclone_read(["lsf", "gdrive:hub/x", "--files-only"])
    assert rc == 0 and seen["timeout"] == 180


def test_rclone_read_retry_then_fail_keeps_rc_and_stderr(monkeypatch, no_sleep, caplog):
    """İki deneme de 5xx → rc!=0 döner, stderr korunur (fail-closed), 2 çağrı."""
    calls = []
    fake = _make_fake_run([(1, "", "HTTP 503 Service Unavailable"),
                           (1, "", "HTTP 503 Service Unavailable")], calls)
    _patch_subprocess(monkeypatch, sm, fake)
    with caplog.at_level("WARNING"):
        rc, out, err = sm.rclone_read(["lsjson", "gdrive:hub/x", "--hash"])
    assert rc == 1 and len(calls) == 2         # yalnız 1 retry
    assert "HTTP 503" in err                   # stderr kaybolmaz (çağırıcı basar)
    assert any("sync hata:" in r.message and "rc=1" in r.message
               and "retry=1/1" in r.message for r in caplog.records)


def test_rclone_read_permanent_error_no_retry(monkeypatch, no_sleep):
    """Kalıcı hata (yok/permission denied) → retry YOK, tek çağrı."""
    calls = []
    fake = _make_fake_run([(1, "", "no such file or directory")], calls)
    _patch_subprocess(monkeypatch, sm, fake)
    rc, out, err = sm.rclone_read(["lsf", "gdrive:hub/yok"])
    assert rc == 1 and len(calls) == 1
    assert "no such file" in err


def test_rclone_read_timeout_is_transient_and_retries(monkeypatch, no_sleep):
    """Timeout → geçici sayılır (retry), ikinci deneme başarılıysa rc=0."""
    calls = []

    def fake_run(cmd_args, capture_output=True, text=True, errors="replace",
                 timeout=60, **kw):
        calls.append(list(cmd_args))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd_args, timeout)
        return _FakeResult(0, "ok\n", "")

    _patch_subprocess(monkeypatch, sm, fake_run)
    rc, out, err = sm.rclone_read(["lsf", "gdrive:hub/z"])
    assert rc == 0 and len(calls) == 2


def test_rclone_read_write_verb_rejected(monkeypatch, no_sleep):
    """YAZMA komutu rclone_read'a verilirse ValueError — çift yazma fail-closed."""
    calls = []
    fake = _make_fake_run([(0, "", "")], calls)
    _patch_subprocess(monkeypatch, sm, fake)
    for args in (["copyto", "a.tar.gz", "gdrive:hub/n/a.tar.gz"],
                 ["copy", "/tmp/dir", "gdrive:hub"],
                 ["sync", "/tmp/dir", "gdrive:hub"],
                 ["delete", "gdrive:hub/x"],
                 ["copy", "status", "gdrive:hub/dest"]):   # okuma-benzeri ad tuzağı
        with pytest.raises(ValueError):
            sm.rclone_read(args)
    assert calls == []          # hiç subprocess çağrısı yapılmadı


def test_direct_rclone_reads_source_guard():
    """Regresyon kapısı: sync_motor'da doğrudan subprocess ile rclone OKUMASI kalamaz.

    Tüm okuma çağrıları rclone_read (retry'li) üzerinden geçer; subprocess.run
    ile kalan çağrılar yalnız YAZMA alt-komutları olabilir.
    """
    src = Path(sm.__file__).read_text(encoding="utf-8")
    verbs = re.findall(r'subprocess\.run\(\s*\[\s*["\']rclone["\']\s*,\s*["\']([a-z]+)["\']',
                       src)
    assert verbs, "rclone çağrı taraması eşleşme bulamadı (regex bayat)"
    allowed_writes = set(sm._RETRY_WRITE_TOKENS) | {"mkdir", "touch"}
    leftover_reads = sorted(set(verbs) - allowed_writes)
    assert not leftover_reads, f"doğrudan subprocess ile OKUMA kaldı: {leftover_reads}"
    # okuma yardımcısı gerçekten kullanılıyor (1 tanım + >=5 çağrı yeri)
    assert len(re.findall(r"rclone_read\(", src)) >= 6


def test_rclone_read_only_read_tokens_accepted():
    """Politika kaynağı _RETRY_READ_TOKENS: okuma sözcükleri kabul, yazma red."""
    for args in (["lsf", "gdrive:hub"], ["lsjson", "gdrive:hub", "--hash"],
                 ["lsd", "gdrive:hub"], ["cat", "gdrive:hub/f.json"]):
        assert sm._is_idempotent_read(" ".join(["rclone"] + args))
    for args in (["copyto", "a", "b"], ["copy", "a", "b"], ["mkdir", "gdrive:hub/x"]):
        assert not sm._is_idempotent_read(" ".join(["rclone"] + args))


# ─── Denetim bulguları (QCode/OceanAPI hipotezleri) — exception sınıfı ─────

def test_rclone_read_fatal_exception_no_retry(monkeypatch, no_sleep):
    """KALICI exception (rclone kurulu değil) → retry YOK, tek deneme.

    Hipotez: rc==-1 durumunda geçici sayılıp 3s boşa bekleme + çift deneme
    yapılabilir. Politika: _RETRY_FATAL ('no such file'/'command not found')
    eşleşirse geçici SAYILMAZ (run_cmd ile aynı kural).
    """
    calls = []

    def fake_run(cmd_args, capture_output=True, text=True, errors="replace",
                 timeout=60, **kw):
        calls.append(list(cmd_args))
        raise FileNotFoundError(2, "No such file or directory", "rclone")

    _patch_subprocess(monkeypatch, sm, fake_run)
    rc, out, err = sm.rclone_read(["lsf", "gdrive:hub/x"])
    assert rc == -1 and len(calls) == 1
    assert "No such file" in err


def test_rclone_read_transient_exception_retries(monkeypatch, no_sleep):
    """GEÇİCİ exception (bağlantı sıfırlandı) → 1 retry, sonra başarı."""
    calls = []

    def fake_run(cmd_args, capture_output=True, text=True, errors="replace",
                 timeout=60, **kw):
        calls.append(list(cmd_args))
        if len(calls) == 1:
            raise ConnectionResetError("connection reset by peer")
        return _FakeResult(0, "ok\n", "")

    _patch_subprocess(monkeypatch, sm, fake_run)
    rc, out, err = sm.rclone_read(["lsf", "gdrive:hub/y"])
    assert rc == 0 and len(calls) == 2


def test_rclone_read_permission_denied_no_retry(monkeypatch, no_sleep):
    """rc=1 + 'permission denied' → kalıcı: gereksiz 3s retry beklemesi YOK."""
    calls = []
    fake = _make_fake_run([(1, "", "403 Forbidden: permission denied")], calls)
    _patch_subprocess(monkeypatch, sm, fake)
    rc, out, err = sm.rclone_read(["lsjson", "gdrive:hub/x", "--hash"])
    assert rc == 1 and len(calls) == 1
    assert "permission denied" in err


# ─── Politika sapma kapısı (drift gate) ─────────────────────────────
# Retry politikası iki YERDE ayrı ayrı uygulanır:
#   sync_motor._RETRY_READ_TOKENS          (run_cmd / rclone_read)
#   sync_common_knowledge._RCLONE_READ_COMMANDS   (_run_rclone)
# Biri güncellenip diğeri unutulursa dayanıklılık sessizce zayıflar.
# Bu kapı, iki uygulamanın AYNI kümeyi ve AYNI kararı vermesini zorlar.
# Ölçülen gerçek sapma (12 Eyl 2026): 'status' motorda okuma, ck'da yazma
# sayılıyordu — ck yalnızca cat/lsf çağırdığı için canlı hata değildi,
# ama 'about'/'direxists'/'ping' eklendiğinde retry kaybı doğururdu.

_OKUMA_ALT_KOMUTLAR = ["cat", "lsf", "lsjson", "lsd", "status", "ping",
                       "listremotes", "direxists", "about"]
_YAZMA_ALT_KOMUTLAR = ["copy", "copyto", "move", "sync", "mkdir", "delete",
                       "purge", "backup", "push", "restore", "upload", "rm"]


def test_okuma_kumeleri_kume_olarak_esit():
    """İki sınıflandırıcının okuma kümeleri EŞİT olmalı (tek politika)."""
    motor = set(sm._RETRY_READ_TOKENS)
    ck_set = set(ck._RCLONE_READ_COMMANDS)
    assert motor == ck_set, (
        "retry okuma kümeleri saptı — yalnız sync_motor'da: %s | yalnız "
        "sync_common_knowledge'da: %s" % (sorted(motor - ck_set),
                                          sorted(ck_set - motor)))


def test_okuma_ve_yazma_kumeleri_cakismaz():
    """Güvenlik değişmezi: bir sözcük hem okuma hem yazma olamaz."""
    yazma = set(sm._RETRY_WRITE_TOKENS)
    for ad, okuma in (("sync_motor", set(sm._RETRY_READ_TOKENS)),
                      ("sync_common_knowledge", set(ck._RCLONE_READ_COMMANDS))):
        kesisim = okuma & yazma
        assert not kesisim, f"{ad}: okuma∩yazma boş olmalı — {sorted(kesisim)}"


@pytest.mark.parametrize("alt", _OKUMA_ALT_KOMUTLAR)
def test_iki_siniflandirici_okumada_ayni_karari_verir(alt):
    """Her okuma alt-komutu iki tarafta da 'retry edilebilir' olmalı."""
    assert sm._is_idempotent_read(f"rclone {alt} gdrive:hub")
    assert ck._is_rclone_read([alt, "gdrive:hub"])


@pytest.mark.parametrize("alt", _YAZMA_ALT_KOMUTLAR)
def test_iki_siniflandirici_yazmada_ayni_karari_verir(alt):
    """Her yazma alt-komutu iki tarafta da RED edilmeli (fail-closed)."""
    assert not sm._is_idempotent_read(f"rclone {alt} a gdrive:hub")
    assert not ck._is_rclone_read([alt, "a", "gdrive:hub"])


@pytest.mark.parametrize("cmd", [
    "rclone copy status gdrive:x gdrive:y",
    "rclone move cat gdrive:x gdrive:y",
    "rclone sync lsf gdrive:x gdrive:y",
    "rclone copyto lsjson gdrive:x gdrive:y",
    "rclone backup about gdrive:x",
    "rclone push ping gdrive:x",
])
def test_yazmada_gizlenmis_okuma_sozcugu_retry_etmez(cmd):
    """Yazma alt-komutu okuma sözcüğü taşısa bile İKİ taraf da RED etmeli.

    'rclone copy status dest' — dosya adı okuma sözcüğüne benziyor;
    yazma olduğu için çift yazma riskine karşı retry kesinlikle kapalı.
    """
    assert not sm._is_idempotent_read(cmd)
    assert not ck._is_rclone_read(cmd.split()[1:])


def test_run_rclone_status_okumadir_retry_eder(monkeypatch, no_sleep):
    """'status' bir OKUMADIR (spec: cat/lsf/status) → geçici hatada 1 retry.

    Düzeltme öncesi ck 'status'u yazma sayıp retry ETMİYORDU (sapma);
    geçici ağ hatasında dayanıklılık motordan zayıftı.
    """
    calls = []
    fake = _make_fake_run([(1, "", "connection reset by peer"),
                           (0, "job: none\n", "")], calls)
    _patch_subprocess(monkeypatch, ck, fake)
    rc, out, err = ck._run_rclone(["status", "gdrive:hub"])
    assert rc == 0
    assert len(calls) == 2  # geçici hata → retry → başarı
