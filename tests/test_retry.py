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
                       "listremotes", "direxists", "about", "version"]
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


# ─── Bayrak önekli gizlenme (QCode claude-sonnet-5 denetimi, bulgu #4) ──
# 'rclone' sözdiziminde alt-komut normalde 2. token'dır. Global bir bayrak
# öne geçerse (ve bayrağın DEĞERİ okuma sözcüğü ise) eski ilk-3-token
# taraması yazmayı OKUMA sanıp retry açabiliyordu — çift yazma riski.
# Ölçüm (12 Eyl 2026): bu kalıp kodda HİÇ kullanılmıyor, yani canlı hata
# değildi; yine de veto penceresi ilk-3'e genişletilip kapatıldı ve kilitlendi.

@pytest.mark.parametrize("cmd", [
    "rclone --config lsf copy gdrive:a gdrive:b",
    "rclone --config lsf copyto a b",
    "rclone --log-level status sync gdrive:a gdrive:b",
    "rclone -v --config cat move a b",
])
def test_bayrak_onekli_yazma_okuma_sanilmaz(cmd):
    """Bayrak öne geçse bile yazma alt-komutu retry AÇAMAZ (fail-closed)."""
    assert not sm._is_idempotent_read(cmd)


@pytest.mark.parametrize("cmd", [
    "rclone --config /root/.config/rclone/rclone.conf lsf gdrive:hub --files-only",
    "rclone -v lsjson gdrive:hub --hash",
    "rclone --log-level INFO lsd gdrive:hub",
])
def test_bayrak_onekli_okuma_retry_almaya_devam_eder(cmd):
    """Genişleyen veto, GERÇEK okuma çağrılarını kırmamalı."""
    assert sm._is_idempotent_read(cmd)


# ─── v2.6.1 SINIFLANDIRICI SERTLEŞTİRMESİ (bağımsız denetim, 12 Eyl 2026) ──
# Denetçinin verdiği örnek (`status a b c d copy`) ÖLÇÜMLE YANLIŞ çıktı:
# 'copy' veto penceresinin (toks[1:6]) İÇİNDE ve zaten False dönüyordu.
# Ama aynı denetimin altında yatan iki GERÇEK kaçak ölçüldü ve kapatıldı:
#   • yazma sözcüğü pencere DIŞINDA → `rclone status a b c d e f copy`
#     (eski sınıflandırıcı: True = yazma komutuna retry açılıyordu)
#   • boru hattının 2. parçasında gizli yazma → `rclone lsf h | xargs … copyto`
#     (eski: yalnız ilk parça inceleniyordu → True)

@pytest.mark.parametrize("cmd", [
    "rclone status a b c d e f copy",
    "rclone lsf gdrive:hub --files-only --hash --max-depth 1 -v copy",
])
def test_pencere_disinda_yazma_sozcugu_veto_eder(cmd):
    """Yazma sözcüğü 5 argümanlık pencerenin dışına çıksa da VETO eder."""
    assert not sm._is_idempotent_read(cmd)


@pytest.mark.parametrize("cmd", [
    "rclone lsf gdrive:hub | xargs -I{} rclone copyto {} gdrive:dest",
    "rclone lsf gdrive:hub && rclone copyto a b",
    "rclone lsd gdrive:hub; rclone delete gdrive:hub/x",
    "rclone lsf gdrive:hub | tail -1",
    "rclone cat gdrive:hub/f.json | bash",
])
def test_boru_hattinda_gizli_yazma_veya_bilinmeyen_parca_retry_almaz(cmd):
    """Bileşik komutta HER parça okuma olmalı — aksi halde fail-closed RED."""
    assert not sm._is_idempotent_read(cmd)


def test_tek_parca_okuma_retry_almaya_devam_eder():
    """Sertleştirme GERÇEK okuma çağrılarını kırmamalı (canlı retry yerleri)."""
    for cmd in ("rclone lsd gdrive:hermes-sync/hahmet/H1/versiyonlu",
                "rclone lsf gdrive:hub --files-only",
                "rclone -v lsjson gdrive:hub --hash",
                "python3 /root/.hermes/scripts/a2a_cli.py ping 100.92.2.47 "
                "--token abc123",
                "lsf"):
        assert sm._is_idempotent_read(cmd), cmd


def test_komut_parcalari_bos_ve_ayiracli():
    """Parçalayıcı: boş girdi/yalnız ayıraç → parça yok; | ; && → 3 parça."""
    assert sm._komut_parcalari("") == []
    assert sm._komut_parcalari("   ") == []
    assert sm._komut_parcalari("  | ; && ") == []
    assert len(sm._komut_parcalari("a | b && c")) == 3


def test_run_cmd_boru_hattinda_yazma_retry_etmez(monkeypatch, no_sleep):
    """Boru hattında gizli yazma → retries=1 verilse bile TEK subprocess çağrısı."""
    calls = []
    fake = _make_fake_run([(1, "", "connection reset by peer")], calls)
    _patch_subprocess(monkeypatch, sm, fake)
    _out, rc = sm.run_cmd(
        "rclone lsf gdrive:hub | xargs -I{} rclone copyto {} gdrive:dest",
        retries=1)
    assert rc == 1
    assert len(calls) == 1, "boru hattındaki yazma retry aldı (fail-open)"


def test_retry_cagri_yerleri_bilesik_komut_kullanmaz():
    """Kaynak kapısı: `retries=1` veren çağrı yerleri boru/zincir kullanmamalı.

    Sınıflandırıcı artık bileşik komutları RED ediyor; ama retry'li bir çağrı
    yeri boru hattına dönerse retry SESSİZCE kaybolurdu (dayanıklılık düşüşü).
    Bu kapı ikisinin ayrışmasını engeller (yorum satırları ayıklanır).
    """
    src = Path(sm.__file__).read_text(encoding="utf-8")
    kod = "\n".join(l for l in src.splitlines()
                    if not l.lstrip().startswith("#"))
    yerler = [m.start() for m in re.finditer(r"retries=1", kod)]
    assert len(yerler) >= 3, "retry çağrı yeri taraması bayat (>=3 bekleniyor)"
    for pos in yerler:
        bag = kod[max(0, pos - 260):pos]
        if "def run_cmd" in bag or "def run_with_retry" in bag:
            continue          # tanım satırları (varsayılan değer)
        assert not re.search(r"[|;&]", bag), \
            f"retries=1 + kabuk ayırıcı (retry kaybı): {bag[-90:]!r}"


# ─── v2.6.2 SINIFLANDIRICI SERTLEŞTİRMESİ (13 Eyl 2026) ────────────────────
# ÖLÇÜLMÜŞ kaçak: yazma veto kümesi EKSİKTİ. Adı kümede olmayan mutasyon
# alt-komutları, KONUMSAL argümanı bir okuma sözcüğü olduğunda sınıflandırıcı
# "okuma" sanıp retry açıyordu — yani "yazmaya ASLA retry" değişmezi
# sınıflandırıcı sınırında deliniyordu (fail-open).
# Fix ÖNCESİ ölçüm (bu depo, 13 Eyl 2026):
#   'rclone moveto cat gdrive:dest'   → True   (MOVE retry)
#   'rclone touch status gdrive:p'    → True   (WRITE retry)
#   'rclone deletefile cat remote:x'  → True   (DELETE retry)
# Fix SONRASI: üçü de False; canlı okuma çağrıları (lsf/cat/lsjson/lsd)
# retry almaya devam ediyor (aşağıdaki regresyon testi).
_KONUMSAL_OKUMA_TUZAGI = [
    ["moveto", "cat", "gdrive:dest"],
    ["touch", "status", "gdrive:p"],
    ["deletefile", "cat", "gdrive:x"],
    ["rmdirs", "lsf", "gdrive:hub"],
    ["cleanup", "status", "gdrive:hub"],
    ["copyurl", "cat", "gdrive:dest"],
    ["bisync", "lsf", "gdrive:a", "gdrive:b"],
    ["settier", "status", "gdrive:x"],
    ["rcat", "cat", "gdrive:p"],
    ["mkdir", "lsf", "gdrive:yeni"],
    ["dedupe", "lsjson", "gdrive:hub"],
]

# `rclone help` (rclone v1.60.1, bu makinede ölçüldü) komut listesinden
# süzülen DURUM DEĞİŞTİREN alt-komutlar. Salt-okuma komutları burada YOK
# (about/cat/check/checksum/cryptcheck/cryptdecode/hashsum/help/link/
#  listremotes/ls/lsd/lsf/lsjson/lsl/md5sum/ncdu/obscure/sha1sum/size/
#  test/tree/version).
_RCLONE_MUTASYON_ALT_KOMUTLARI = [
    "authorize", "backend", "bisync", "cleanup", "completion", "config",
    "copy", "copyto", "copyurl", "dedupe", "delete", "deletefile",
    "genautocomplete", "gendocs", "mkdir", "mount", "move", "moveto",
    "purge", "rc", "rcat", "rcd", "reconnect", "rmdir", "rmdirs",
    "selfupdate", "serve", "settier", "sync", "touch",
]


@pytest.mark.parametrize("args", _KONUMSAL_OKUMA_TUZAGI)
def test_konumsal_okuma_sozcugu_yazmayi_retry_ettirmez(args):
    """Mutasyon alt-komutu + konumsal okuma sözcüğü → İKİ taraf da RED.

    Sınıflandırıcı, okuma sözcüğünü PENCEREDE aradığı için (bayrak önekli
    okumalar kırılmasın diye) konumsal bir 'cat'/'status' argümanı okuma
    sanılabiliyordu. Veto kümesi artık mutasyon komutlarının tamamını
    kapsıyor → fail-closed.
    """
    cmd = " ".join(["rclone"] + args)
    assert not sm._is_idempotent_read(cmd), f"yazmaya retry açık: {cmd}"
    assert not ck._is_rclone_read(args), f"ck yazmaya retry açık: {cmd}"


def test_yazma_kumesi_gercek_rclone_mutasyonlarini_kapsar():
    """Politika kaynağı: ölçülmüş mutasyon listesi veto kümesinde olmalı."""
    eksik = sorted(set(_RCLONE_MUTASYON_ALT_KOMUTLARI) - set(sm._RETRY_WRITE_TOKENS))
    assert not eksik, f"veto kümesinde eksik mutasyon alt-komutları: {eksik}"


def test_okuma_kumesi_gercek_rclone_mutasyonu_icermez():
    """Salt-okuma kümesine mutasyon komutu sızmamalı (fail-closed yönü)."""
    kesisim = sorted(set(sm._RETRY_READ_TOKENS) & set(_RCLONE_MUTASYON_ALT_KOMUTLARI))
    assert not kesisim, f"okuma kümesinde mutasyon komutu: {kesisim}"
    assert not (set(ck._RCLONE_READ_COMMANDS) & set(_RCLONE_MUTASYON_ALT_KOMUTLARI))


def test_canli_okumalar_veto_genislemesinden_etkilenmedi():
    """Regresyon: veto büyümesi GERÇEK okuma çağrılarını kırmamalı."""
    for cmd in ("rclone lsd gdrive:hermes-sync/hahmet/H1/versiyonlu",
                "rclone lsf gdrive:hub --files-only",
                "rclone -v lsjson gdrive:hub --hash",
                "rclone cat gdrive:hub/status.json",
                "rclone --config /root/.config/rclone/rclone.conf lsf gdrive:hub"):
        assert sm._is_idempotent_read(cmd), cmd


def test_run_cmd_konumsal_tuzakta_tek_deneme(monkeypatch, no_sleep):
    """Uçtan uca: konumsal tuzak + retries=1 → TEK subprocess çağrısı."""
    calls = []
    fake = _make_fake_run([(1, "", "connection reset by peer")], calls)
    _patch_subprocess(monkeypatch, sm, fake)
    _out, rc = sm.run_cmd("rclone moveto cat gdrive:dest", retries=1)
    assert rc == 1
    assert len(calls) == 1, "konumsal tuzak retry aldı (fail-open)"


# ─── SINIRSIZ rclone ÇAĞRISI KAPISI (14 Eyl 2026) ───────────────────────
# Ölçülen olay: backup upload `rclone copyto` çağrısı SINIRSIZDI. GDrive
# throttle'da (shared client_id) rclone asılı kaldı → cron 3600s'te SIGTERM
# attı, stdout blok-tamponlu olduğu için log BOŞ kaldı (tanı imkânsız) ve sync
# kilidi saatlerce tutuldu (delta koşuları da atlandı). Parite kapısı aynı
# sınıfın ikinci örneğini `cmd_rollback` indirmesinde buldu (timeout yok).
# Kural: retry EKLENMESİ yetmez — asıl güvence her çağrının SINIRLI olmasıdır.

def _rclone_subprocess_bloklari(kaynak: str):
    """subprocess.run(...) bloklarını DENGELİ parantezle çıkarır → (satır, blok).

    Düz regex iç içe parantezde erken kesiyor (`os.path.basename(tarp)`) ve
    timeout'suz çağrıyı 'timeout var' sanıyordu — bu yüzden dengeli tarama.
    """
    satirlar = kaynak.splitlines()
    bloklar = []
    for i, satir in enumerate(satirlar):
        if "subprocess.run(" not in satir:
            continue
        buf, j = satir, i
        derinlik = buf.count("(") - buf.count(")")
        while derinlik > 0 and j + 1 < len(satirlar):
            j += 1
            buf += "\n" + satirlar[j]
            derinlik = buf.count("(") - buf.count(")")
        bloklar.append((i + 1, buf))
    return bloklar


def test_rclone_subprocess_blok_taramasi_dogru():
    """Kapının dedektörü: dengeli tarama iç içe parantezde kesmemeli."""
    ornek = ('r = subprocess.run(["rclone", "copyto", t,\n'
             '                   os.path.basename(t)], timeout=180)\n')
    bloklar = _rclone_subprocess_bloklari(ornek)
    assert len(bloklar) == 1
    assert "timeout=180" in bloklar[0][1], "tarama blok sonunu kaçırdı"


@pytest.mark.parametrize("hedef", ["root", "paket"])
def test_her_rclone_subprocess_cagrisi_timeout_tasir(hedef):
    """sync_motor.py'deki HER rclone subprocess.run çağrısı timeout taşımalı.

    Kırmızıysa: sınırsız bir rclone çağrısı eklenmiş demektir → GDrive
    throttle'da asılma, cron SIGTERM, boş log ve tutulu kilit geri gelir.
    """
    kok = Path(sm.__file__).resolve().parent.parent          # depo kökü
    dosya = kok / "sync_motor.py" if hedef == "root" else Path(sm.__file__)
    kaynak = dosya.read_text(encoding="utf-8")
    sinirsiz = []
    for satir, blok in _rclone_subprocess_bloklari(kaynak):
        if "rclone" not in blok:
            continue
        if "timeout=" not in blok:
            sinirsiz.append(f"L{satir}: {' '.join(blok.split())[:100]}")
    assert not sinirsiz, (
        "timeout'suz rclone çağrısı (asılmada kilit + tanısız SIGTERM):\n"
        + "\n".join(sinirsiz)
    )


def test_rollback_indirmesi_timeout_ve_fail_closed():
    """cmd_rollback indirmesi: 180s sınırı + TimeoutExpired'da rc=1, yazma YOK.

    Fail-closed kanıtı: zaman aşımında return 1 — geri alma/uygulama aşamasına
    geçilmez; hedef dizine hiçbir dosya yazılmaz (geçici dizindeki kısmi indirme
    finally rmtree ile silinir).
    """
    src = Path(sm.__file__).read_text(encoding="utf-8")
    bloklar = _rclone_subprocess_bloklari(src)
    hedef = [b for _, b in bloklar if 'copyto", f"{hub}/{node}/{version}"' in b]
    assert hedef, "cmd_rollback indirme çağrısı bulunamadı (yeniden adlandırıldı?)"
    assert "timeout=180" in hedef[0], "cmd_rollback indirmesi sınırsız"
    assert "TimeoutExpired" in src.split("def cmd_rollback")[1][:4000], (
        "cmd_rollback zaman aşımını yakalamıyor (tanısız çökme)"
    )


# ─── YAZMAYA RETRY YASAĞI — YAPISAL (AST) KAPI (14 Eyl 2026) ────────────
# Denetim bulgusu (gpt-5.6-sol): cmd_backup upload'ı `for _attempt in (1, 2)`
# döngüsüyle YAZMA (copyto) çağrısını tekrar deniyordu → "yazmaya asla retry"
# ilkesinin ihlali. Metin taraması bunu yakalamaz (çağrı yine bir kez geçer);
# yapı gerekir. Bu kapı, YAZMA alt-komutlu bir subprocess.run çağrısının
# deneme/while döngüsü içinde bulunmasını REDDEDER.

def _yazma_retry_donguleri(kaynak: str):
    """Deneme döngüsü (literal aralık/while) içindeki rclone YAZMA çağrıları."""
    import ast

    agac = ast.parse(kaynak)
    yazma = set(sm._RETRY_WRITE_TOKENS)
    bulgular = []

    def _yazma_cagrilari(dugumler):
        adlar = []
        for d in dugumler:
            for n in ast.walk(d):
                if not isinstance(n, ast.Call):
                    continue
                f = n.func
                if not (isinstance(f, ast.Attribute) and f.attr == "run"):
                    continue
                for a in n.args:
                    if not isinstance(a, ast.List):
                        continue
                    for el in a.elts:
                        if isinstance(el, ast.Constant) and isinstance(el.value, str):
                            v = el.value.strip("\"'")
                            if v in yazma:
                                adlar.append(v)
        return adlar

    for fn in ast.walk(agac):
        if not isinstance(fn, (ast.For, ast.While)):
            continue
        if isinstance(fn, ast.For):
            it = fn.iter
            literal = (isinstance(it, (ast.Tuple, ast.List))
                       and all(isinstance(e, ast.Constant) for e in it.elts))
            aralik = (isinstance(it, ast.Call)
                      and getattr(it.func, "id", "") == "range")
            if not (literal or aralik):
                continue                      # 'for n in nodes:' gibi normal döngü
        adlar = _yazma_cagrilari(fn.body)
        if adlar:
            bulgular.append(f"L{fn.lineno}: deneme döngüsü içinde YAZMA {adlar}")
    return bulgular


def test_yazma_cagrisi_retry_dongusunde_degil():
    """YAZMA (copyto/copy/sync...) çağrısı deneme döngüsü içinde olamaz.

    Retry yalnız idempotent OKUMALARDA ve RUN seviyesindedir (node atlanır,
    sonraki koşu telafi eder). Kırmızıysa: uzak hedefin durumu bilinmeden
    ikinci yazma riski geri gelmiş demektir.
    """
    kok = Path(sm.__file__).resolve().parent.parent / "sync_motor.py"
    for kaynak, ad in ((Path(sm.__file__).read_text(encoding="utf-8"), "paket"),
                       (kok.read_text(encoding="utf-8"), "kök")):
        bulgular = _yazma_retry_donguleri(kaynak)
        assert not bulgular, f"{ad} kopyada yazmaya retry: {bulgular}"


def test_yazma_retry_dedektoru_eski_ihlali_yakalar():
    """Dedektör kanıtı: kaldırılan 2-denemeli upload döngüsü YAKALANIR."""
    eski = (
        "def f(cfg, nodes):\n"
        "    for n in nodes:\n"
        "        r = None\n"
        "        for _attempt in (1, 2):\n"
        "            try:\n"
        "                r = subprocess.run([\"rclone\", \"copyto\", t, dest],\n"
        "                                   capture_output=True, timeout=180)\n"
        "                break\n"
        "            except subprocess.TimeoutExpired:\n"
        "                r = None\n"
        "        if r is None:\n"
        "            continue\n"
    )
    bulgular = _yazma_retry_donguleri(eski)
    assert bulgular, "dedektör eski yazma-retry ihlalini KAÇIRDI"
    assert "copyto" in bulgular[0]


def test_yazma_retry_dedektoru_normal_node_dongusune_takilmaz():
    """Yanlış pozitif kapısı: 'for n in nodes:' içindeki tek yazma normaldir."""
    normal = (
        "def f(cfg, nodes):\n"
        "    for n in nodes:\n"
        "        try:\n"
        "            r = subprocess.run([\"rclone\", \"copyto\", t, dest], timeout=180)\n"
        "        except subprocess.TimeoutExpired:\n"
        "            continue\n"
    )
    assert _yazma_retry_donguleri(normal) == []


# ─── v2.7.6 ÖLÇÜLMÜŞ İKİ KUSUR (16 Eyl 2026) ────────────────────────────────
# K1) `run_cmd._exec` istisna yolunda metin yalnız `out`'a konuyor, `err` BOŞ
#     bırakılıyordu → `_is_transient_rc()` kalıcı hataları `err`'den ayırt
#     ettiği için KALICI istisna (rclone binary yok / izin reddi) geçici
#     sanılıp retry ediliyordu. Ölçüm (fix ÖNCESİ, bu depo):
#       run_cmd("rclone cat gdrive:x/y", retries=1) + FileNotFoundError
#       → 3s bekleme + "sync hata: … rc=-1 0.0s retry=1/1"   (retry OLMAMALIYDI)
#     Aynı hata sınıfı sync_common_knowledge._run_rclone'da DOĞRU yapılıyordu
#     (err = str(e)) — iki yol artık tutarlı.
# K2) `run_with_retry` koşulu `i < retries and A or B` → `(A and B) or B`:
#     SON denemede 'timeout' içeren istisna yine retry dalına giriyor, döngü
#     bitince fonksiyon **None** dönüyordu → istisna SESSİZCE YUTULUYORDU
#     (fail-open) ve tanı 'retry 2/1' gibi yanıltıcı yazıyordu.
#     Ölçüm (fix ÖNCESİ): run_with_retry(boom, retries=1) → returned=None,
#     2 × 5s bekleme.

def _fake_sub(monkeypatch, run_fn):
    """subprocess.run'ı değiştir; TimeoutExpired GERÇEK sınıf kalır.

    (Sadece `run` içeren bir namespace, `except subprocess.TimeoutExpired`
    satırını AttributeError ile patlatıyordu — ölçüldü.)
    """
    monkeypatch.setattr(sm, "subprocess", types.SimpleNamespace(
        run=run_fn, TimeoutExpired=subprocess.TimeoutExpired))


def test_run_cmd_kalici_istisna_retry_etmez(monkeypatch, no_sleep):
    """K1: rclone binary yok (FileNotFoundError) → KALICI → retry YOK."""
    cagri = []

    def patla(*a, **kw):
        cagri.append(1)
        raise FileNotFoundError(2, "No such file or directory", "rclone")

    _fake_sub(monkeypatch, patla)
    out, rc = sm.run_cmd("rclone cat gdrive:x/y", retries=1)
    assert rc == -1
    assert len(cagri) == 1, "kalıcı istisna retry edildi (K1 regresyonu)"
    assert "No such file" in str(out)          # tanı kaybolmaz


def test_run_cmd_gecici_istisna_retry_eder(monkeypatch, no_sleep):
    """K1 regresyon kapısı: GEÇİCİ istisna retry almaya DEVAM eder."""
    n = {"c": 0}

    def gecici(*a, **kw):
        n["c"] += 1
        if n["c"] == 1:
            raise ConnectionResetError(104, "Connection reset by peer")
        return types.SimpleNamespace(stdout="ok", returncode=0, stderr="")

    _fake_sub(monkeypatch, gecici)
    out, rc = sm.run_cmd("rclone cat gdrive:x/y", retries=1)
    assert (out, rc) == ("ok", 0)
    assert n["c"] == 2


def test_run_cmd_izin_reddi_istisnasi_retry_etmez(monkeypatch, no_sleep):
    """K1: PermissionError de kalıcıdır (err artık sınıflandırıcıya gidiyor)."""
    cagri = []

    def patla(*a, **kw):
        cagri.append(1)
        raise PermissionError(13, "Permission denied")

    _fake_sub(monkeypatch, patla)
    _, rc = sm.run_cmd("rclone lsf gdrive:hub", retries=1)
    assert rc == -1 and len(cagri) == 1


def test_run_with_retry_gecici_istisnayi_yutmaz(monkeypatch, no_sleep):
    """K2: son denemede bile istisna YÜKSELİR — asla None dönmez."""
    def boom(*a, **kw):
        raise TimeoutError("operation timeout")

    with pytest.raises(TimeoutError):
        sm.run_with_retry(boom, retries=1)


def test_run_with_retry_kalici_istisnada_tek_deneme(monkeypatch, no_sleep):
    """K2: kalıcı işaretli istisna İLK denemede yükselir (retry YOK)."""
    cagri = {"n": 0}

    def kalici(*a, **kw):
        cagri["n"] += 1
        raise PermissionError(13, "Permission denied")

    with pytest.raises(PermissionError):
        sm.run_with_retry(kalici, retries=2)
    assert cagri["n"] == 1


def test_run_with_retry_bilinmeyen_istisna_retry_etmez(monkeypatch, no_sleep):
    """K2 fail-closed: tanınmayan hata sessizce retry edilmez, yükselir."""
    cagri = {"n": 0}

    def tuhaf(*a, **kw):
        cagri["n"] += 1
        raise ValueError("bozuk konfig")

    with pytest.raises(ValueError):
        sm.run_with_retry(tuhaf, retries=2)
    assert cagri["n"] == 1


def test_run_with_retry_basari_ve_gecici_sonrasi_basari(monkeypatch, no_sleep):
    """Mutlu yol + geçici hatadan sonra başarı (sayaç doğru)."""
    assert sm.run_with_retry(lambda x: x + 1, 41, retries=1) == 42

    n = {"c": 0}

    def bir_kere_gecici(*a, **kw):
        n["c"] += 1
        if n["c"] == 1:
            raise ConnectionResetError(104, "Connection reset by peer")
        return "tamam"

    assert sm.run_with_retry(bir_kere_gecici, retries=1) == "tamam"
    assert n["c"] == 2


def test_istisna_yolu_iki_modulde_ayni_davranir(monkeypatch, no_sleep):
    """K1 tutarlılık kapısı: sync_motor.run_cmd ↔ sync_common_knowledge.

    İkisi de kalıcı istisnada retry ETMEMELİ (sapma tam burada oluşmuştu:
    sck doğru, run_cmd yanlış — bu kapı iki yolun ayrışmasını engeller).
    """
    sm_cagri, ck_cagri = [], []

    def sm_patla(*a, **kw):
        sm_cagri.append(1)
        raise FileNotFoundError(2, "No such file or directory", "rclone")

    def ck_patla(*a, **kw):
        ck_cagri.append(1)
        raise FileNotFoundError(2, "No such file or directory", "rclone")

    _fake_sub(monkeypatch, sm_patla)
    sm.run_cmd("rclone cat gdrive:x/y", retries=1)
    assert len(sm_cagri) == 1, "run_cmd kalıcı istisnayı retry etti"

    monkeypatch.setattr(ck, "subprocess", types.SimpleNamespace(
        run=ck_patla, TimeoutExpired=subprocess.TimeoutExpired))
    ck._run_rclone(["cat", "gdrive:x/y"])
    assert len(ck_cagri) == 1, "_run_rclone kalıcı istisnayı retry etti"


def test_run_with_retry_oncelik_kapisi():
    """K2 statik kapı: bozuk `and … or …` önceliği geri gelemez.

    Ölçüm: eski satır `if i < retries and "Errno" in str(e) or …` —
    `(A and B) or C` olarak çalışıp SON denemede istisnayı yutuyordu.
    """
    src = Path(sm.__file__).read_text(encoding="utf-8")
    m = re.search(r"def run_with_retry\(.*?\n(?=def |\Z)", src, re.S)
    assert m, "run_with_retry gövdesi bulunamadı"
    # Yürütülebilir kod: docstring + yorum ayıklanır (kapı, kendi
    # dokümantasyonunda alıntılanan eski satıra TAKILMAMALI — ölçüldü).
    kod = re.sub(r'""".*?"""', "", m.group(0), flags=re.S)
    kod = "\n".join(l for l in kod.splitlines()
                    if not l.lstrip().startswith("#"))
    assert 'and "Errno" in' not in kod, "bozuk öncelik ifadesi geri gelmiş"
    assert re.search(r"\braise\b", kod), "istisna yükseltme yolu kayıp (yutma riski)"
    assert "_RETRY_FATAL" in kod and "_RETRY_TRANSIENT" in kod, \
        "sınıflandırma tek kaynaktan yapılmıyor (sapma riski)"


def test_kalici_veto_tum_rc_degerlerinde_gecerli():
    """Denetim bulgusu (gpt-5.6-sol): KALICI işaret 5xx/geçici işareti YENER.

    Ölçüm (fix ÖNCESİ, bu depo):
      _is_transient_rc(1, "no such file or directory - 503 Service Unavailable") → True
      _is_transient_rc(3, "command not found (temporary failure)")               → True
      _is_transient_rc(1, "permission denied: i/o timeout")                      → True
    Üçü de KALICI bir hatanın retry edilmesi demekti (fail-open): veto yalnız
    `rc == -1` yolunda uygulanıyordu.
    """
    assert not sm._is_transient_rc(
        1, "no such file or directory - 503 Service Unavailable")
    assert not sm._is_transient_rc(3, "command not found (temporary failure)")
    assert not sm._is_transient_rc(1, "permission denied: i/o timeout")

    # Geçici yollar bozulmadı (retry almaya devam eder)
    assert sm._is_transient_rc(1, "HTTP 503 Service Unavailable")
    assert sm._is_transient_rc(1, "connection reset by peer")
    assert sm._is_transient_rc(-1, "connection reset by peer")
    assert sm._is_transient_rc(-1, "")
    assert not sm._is_transient_rc(1, "directory not found")


# ─── v2.7.9: rclone_available ÜÇ DURUM + retry (ÖLÇÜLMÜŞ kusur) ──────────
# ÖLÇÜM (fix ÖNCESİ, bu depo):
#   _is_idempotent_read("rclone version")             → False (kümede yoktu)
#   rclone_available() → run_cmd("rclone version")    → retries=0, timeout=60
#   doctor → run_cmd("rclone listremotes", timeout=30, shell=True) → retries=0
# Etki: GEÇİCİ hata 'rclone yok' sanılıyordu → gdrive_snapshot "rclone yok —
# GDrive snapshot atlandı" deyip ATLIYOR, gdrive_pull_latest False dönüyordu
# (GDrive kanalı sessizce kapanır); doctor sahte "GDrive remote YOK" yazıyordu.


def test_version_idempotent_okuma_sinifinda():
    assert sm._is_idempotent_read("rclone version") is True
    assert ck._is_rclone_read(["version"]) is True


@pytest.mark.parametrize("cmd", [
    "rclone copy version gdrive:a gdrive:b",
    "rclone moveto version gdrive:a gdrive:b",
    "rclone deletefile version gdrive:a",
])
def test_version_yazma_vetosunu_gecemez(cmd):
    """'version' argümanı taşıyan YAZMA komutları retry almaz (veto önce)."""
    assert sm._is_idempotent_read(cmd) is False
    assert ck._is_rclone_read(cmd.split()[1:]) is False


def test_rclone_durum_gecici_hatada_belirsiz_ve_fail_closed(monkeypatch, no_sleep):
    """Retry tükendi + geçici hata → 'belirsiz' ve kullanılabilir DEĞİL."""
    calls = []
    fake = _make_fake_run([(1, "", "connection reset by peer")], calls)
    _patch_subprocess(monkeypatch, sm, fake)
    assert sm.rclone_durum() == "belirsiz"
    assert len(calls) == 2                      # TEK çağrı: 1 deneme + 1 retry
    assert calls[0][:2] == ["rclone", "version"]
    assert sm.rclone_available() is False       # ayrı çağrı → yine fail-closed
    assert len(calls) == 4


def test_rclone_durum_gecici_hatada_retry_ile_ok(monkeypatch, no_sleep):
    """Geçici hata → retry → başarı: GDrive kanalı artık sessizce kapanmaz."""
    calls = []
    fake = _make_fake_run([(1, "", "i/o timeout"), (0, "rclone v1.60.1", "")],
                          calls)
    _patch_subprocess(monkeypatch, sm, fake)
    assert sm.rclone_durum() == "ok"
    assert len(calls) == 2                      # 1 deneme + 1 retry (tek çağrı)


def test_rclone_available_ok_ise_true(monkeypatch, no_sleep):
    """rclone_available = (durum == 'ok') — başarıda tek çağrı, True."""
    calls = []
    fake = _make_fake_run([(0, "rclone v1.60.1", "")], calls)
    _patch_subprocess(monkeypatch, sm, fake)
    assert sm.rclone_available() is True
    assert len(calls) == 1


def test_rclone_durum_yoksa_tek_deneme(monkeypatch, no_sleep):
    """KALICI yokluk (binary yok) retry ETMEZ; 'belirsiz'den AYRI raporlanır."""
    calls = []
    fake = _make_fake_run(
        [(-1, "", "[Errno 2] No such file or directory: 'rclone'")], calls)
    _patch_subprocess(monkeypatch, sm, fake)
    assert sm.rclone_durum() == "yok"
    assert len(calls) == 1                      # KALICI → retry YOK
    assert sm.rclone_available() is False
    assert len(calls) == 2


def test_rclone_durum_rc127_yok_sayilir(monkeypatch, no_sleep):
    """rc=127 (komut yok) KALICI kabul edilir — tek deneme, 'yok'."""
    calls = []
    fake = _make_fake_run([(127, "", "")], calls)
    _patch_subprocess(monkeypatch, sm, fake)
    assert sm.rclone_durum() == "yok"
    assert len(calls) == 1


def test_erisilebilirlik_probe_timeout_kisaltilmadi():
    """Eski çağrı 60s varsayılanındaydı → yoklama timeout'u KISALTILAMAZ.

    Kısaltma (30s gibi) yük altındaki makinede yanlış "rclone yok" negatifini
    geri getirirdi; bu kapı değerin sessizce düşürülmesini engeller.
    """
    assert sm._RCLONE_PROBE_TIMEOUT >= 60
    assert sm._RCLONE_PROBE_TIMEOUT < 180        # yoklama, veri okuması değil


def test_rclone_durum_probe_timeoutunu_gonderir(monkeypatch, no_sleep):
    """Yoklama, sabit timeout'u rclone_read'e AYNEN geçirir (sessiz varsayılan yok)."""
    gorulen = {}

    def fake(cmd_args, capture_output=True, text=True, errors="replace",
             timeout=60, **kw):
        gorulen["timeout"] = timeout
        return _FakeResult(0, "rclone v1.60.1", "")

    _patch_subprocess(monkeypatch, sm, fake)
    assert sm.rclone_durum() == "ok"
    assert gorulen["timeout"] == sm._RCLONE_PROBE_TIMEOUT


def test_rclone_durum_siniflandirma_sapmasinda_cokmez(monkeypatch, caplog):
    """Savunma: okuma sınıflandırması bozulursa yoklama ÇÖKMEZ → 'belirsiz'.

    rclone_read, okuma kümesinde olmayan bir komutu ValueError ile reddeder.
    'version' sessizce kümeden çıkarsa bu istisna sync koşusunu çökertirdi;
    bunun yerine yüksek sesle loglanır ve fail-closed 'belirsiz' döner.
    """
    def patlat(*a, **kw):
        raise ValueError("rclone_read yalnız idempotent OKUMA komutları içindir")

    monkeypatch.setattr(sm, "rclone_read", patlat)
    with caplog.at_level("ERROR"):
        assert sm.rclone_durum() == "belirsiz"
    assert "sınıflandırma sapması" in caplog.text


def test_doctor_gdrive_sorgulanamazsa_yok_demez(monkeypatch, capsys, tmp_path,
                                                no_sleep):
    """Geçici hatada doctor 'GDrive remote YOK' DEMEZ → 'sorgulanamadı' + rc=1."""
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda ad: f"/usr/bin/{ad}")
    calls = []
    fake = _make_fake_run([(1, "", "connection refused")], calls)
    _patch_subprocess(monkeypatch, sm, fake)
    cfg = {"dirs": {"kernel": {"path": str(tmp_path)}}, "github": {}}
    rc = sm.cmd_doctor(cfg)
    out = capsys.readouterr().out
    assert "GDrive remote (rclone): sorgulanamadı" in out
    assert "GDrive remote (rclone): YOK" not in out
    assert rc == 1
    assert len(calls) == 2                      # listremotes: retry'li okuma


def test_doctor_gdrive_remote_gercekten_yoksa_yok_der(monkeypatch, capsys,
                                                      tmp_path, no_sleep):
    """rc=0 + liste boş → GERÇEK yokluk: 'YOK' raporu korunur."""
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda ad: f"/usr/bin/{ad}")
    calls = []
    fake = _make_fake_run([(0, "remote1:\nremote2:\n", "")], calls)
    _patch_subprocess(monkeypatch, sm, fake)
    cfg = {"dirs": {"kernel": {"path": str(tmp_path)}}, "github": {}}
    rc = sm.cmd_doctor(cfg)
    out = capsys.readouterr().out
    assert "GDrive remote (rclone): YOK" in out
    assert rc == 1


def test_rclone_okuma_cagri_yerleri_retry_ister():
    """AST kapısı: rclone OKUMA komutları run_cmd ile retries'siz çağrılamaz.

    v2.7.9'da kapatılan kusur tam olarak buydu: okuma komutu run_cmd ile
    (retries'siz) çağrılıyordu. Yeni bir okuma çağrı yeri eklenirse ya
    rclone_read kullanılmalı ya retries=1 verilmeli; yazma komutları muaf
    (onlara retry zaten YASAK).
    """
    import ast
    kok = Path(__file__).resolve().parent.parent / "sync_motor.py"
    tree = ast.parse(kok.read_text(encoding="utf-8"))
    ihlal = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "run_cmd"):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            metin = arg.value
        elif isinstance(arg, ast.JoinedStr):
            metin = "".join(v.value if isinstance(v, ast.Constant) else "x"
                            for v in arg.values)
        else:
            continue
        if "rclone" not in metin:
            continue
        if not sm._is_idempotent_read(metin):   # yazma → retry zaten yasak
            continue
        kw = {k.arg: k for k in node.keywords}
        r = kw.get("retries")
        if not (r and isinstance(r.value, ast.Constant) and r.value.value >= 1):
            ihlal.append((node.lineno, metin[:70]))
    assert not ihlal, f"retry'siz rclone OKUMA çağrı yeri: {ihlal}"


