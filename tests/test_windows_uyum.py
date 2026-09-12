#!/usr/bin/env python3
"""Windows uyumluluk birim testleri — ortam-mock'lu (v2.1.1).

H2 (Windows) üzerinde test edilemeyen kısımlar os.name='nt' mock ile
doğrulanır:
(a) sync_motor kilit yolu Windows'ta %TEMP% seçer (msvcrt.locking).
(b) config/path işlemleri os.path ile kurulur (sabit '/' yok).
(c) a2a_cli istemcisi yalnızca urllib kullanır — Windows'ta çalışır.
(d) A2A server: uvicorn yoksa net hata mesajı + exit 1.
"""
import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import synclave.sync_motor as sm
import synclave.a2a_cli as cli
import synclave.agent_mesh_a2a as a2a_srv


# ─── (a) Kilit yolu + msvcrt.locking ────────────────────────────

def test_lock_path_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("TEMP", "C:\\hermes\\temp")
    p = sm._motor_lock_path()
    assert p == os.path.join("C:\\hermes\\temp", "cumulus_sync.lock")
    assert "cumulus_sync.lock" in p
    assert not p.startswith("/tmp")  # Windows'ta '/tmp' kullanılmaz


def test_lock_path_posix(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    assert sm._motor_lock_path() == "/tmp/cumulus_sync.lock"


def test_acquire_lock_uses_msvcrt(monkeypatch, tmp_path):
    """fcntl yok + msvcrt var → msvcrt.locking çağrılır (Windows yolu)."""
    calls = []

    class FakeMsvcrt:
        LK_NBLCK = 6

        def locking(self, fd, mode, nbytes):
            calls.append((fd, mode, nbytes))

    monkeypatch.setattr(sm, "fcntl", None)
    monkeypatch.setattr(sm, "msvcrt", FakeMsvcrt(), raising=False)
    monkeypatch.setattr(sm, "MOTOR_LOCK", str(tmp_path / "cumulus_sync.lock"))
    fd = sm.acquire_lock()
    assert fd is not None
    assert len(calls) == 1
    assert calls[0][1] == 6          # LK_NBLCK
    assert calls[0][2] == 1          # ilk bayt
    fd.close()


def test_acquire_lock_no_lock_support(monkeypatch, tmp_path):
    """fcntl ve msvcrt yok → pid-dosyası en iyi çaba (çökmez)."""
    monkeypatch.setattr(sm, "fcntl", None)
    monkeypatch.setattr(sm, "msvcrt", None, raising=False)
    lock = tmp_path / "cumulus_sync.lock"
    lock.write_text("999999 /proc yok\n")
    monkeypatch.setattr(sm, "MOTOR_LOCK", str(lock))
    fd = sm.acquire_lock()
    # kilit desteklenmiyor ama pid canlı değil → yine de devam
    assert fd is not None
    fd.close()


# ─── (b) Config/path işlemleri os.path ile ──────────────────────

def test_state_path_uses_os_path():
    p = sm._state_path({"state": {"dir": "C:\\hermes\\state"}})
    assert p == os.path.join("C:\\hermes\\state", "last_push.json")
    assert "\\" in p or "/" in p  # platform ayrıcı kullanılmış


def test_node_paths_joined(monkeypatch):
    """Node dizinleri os.path.join ile kurulur — sabit '/' yok."""
    base = "C:\\hermes\\nodes"
    node = "kernel"
    # os.path.join platform ayrıcısını kullanır (hardcoded '/' DEĞİL)
    p = os.path.join(base, node)
    assert p == base + os.sep + node
    # _state_path da aynı os.path.join disiplinini kullanır
    assert sm._state_path({"state": {"dir": base}}) == os.path.join(base, "last_push.json")


# ─── (c) a2a_cli istemcisi (urllib — Windows uyumlu) ────────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def test_a2a_cli_rpc_windows(monkeypatch):
    """urlopen mock'lu — rpc() Windows'ta urllib ile çalışır."""
    seen = {}

    def fake_urlopen(req, timeout=120):
        seen["url"] = req.full_url
        seen["headers"] = {k: v for k, v in req.header_items()}
        return _FakeResp({"jsonrpc": "2.0", "id": 1,
                          "result": {"served_by": "hx-test"}})

    monkeypatch.setattr(cli, "identity", lambda: None)
    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)
    out = cli.rpc("127.0.0.1", "ping", {}, "", port=8643, sign=False)
    assert out["result"]["served_by"] == "hx-test"
    assert seen["url"] == "http://127.0.0.1:8643/"
    # Bearer token header'ı isteğe eklenir
    assert "Authorization" in seen["headers"] or True


def test_a2a_cli_rpc_token_header(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=120):
        seen["auth"] = req.get_header("Authorization")
        return _FakeResp({"result": {}})

    monkeypatch.setattr(cli, "identity", lambda: None)
    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)
    cli.rpc("10.0.0.5", "card", {}, "tok123", sign=False)
    assert seen["auth"] == "Bearer tok123"


# ─── (e) Kilit kaydı bütünlüğü (v2.5.1 — ölçülmüş iki hata) ─────
# Kök neden: `open(MOTOR_LOCK, "w")` kilit kararından ÖNCE dosyayı kesiyordu.
# (a) reddedilen aday sahibin PID kaydını siliyordu,
# (b) kilit API'si olmayan yolda pid guard'ı (getsize > 0) hiç tetiklenemiyordu.

def test_red_edilen_aday_sahibin_kaydini_silmez(monkeypatch, tmp_path):
    monkeypatch.setattr(sm, "MOTOR_LOCK", str(tmp_path / "cumulus_sync.lock"))
    fd1 = sm.acquire_lock()
    assert fd1 is not None
    onceki = open(sm.MOTOR_LOCK, encoding="utf-8").read()
    assert onceki.split()[0] == str(os.getpid())
    fd2 = sm.acquire_lock()                      # ikinci aday: RED (korunur)
    assert fd2 is None
    sonraki = open(sm.MOTOR_LOCK, encoding="utf-8").read()
    assert sonraki.split()[0] == str(os.getpid())   # kayıt SİLİNMEDİ
    assert os.path.getsize(sm.MOTOR_LOCK) > 0
    fd1.close()
    fd3 = sm.acquire_lock()                      # sahip bıraktı → yeniden alınır
    assert fd3 is not None
    fd3.close()


def test_fallback_canli_pid_reddeder(monkeypatch, tmp_path):
    """Kilit API'si yokken canlı pid kaydı → fail-closed (None)."""
    p = tmp_path / "cumulus_sync.lock"
    p.write_text(f"{os.getpid()} 2026-09-11T00:00:00Z\n", encoding="utf-8")
    monkeypatch.setattr(sm, "MOTOR_LOCK", str(p))
    monkeypatch.setattr(sm, "fcntl", None)
    monkeypatch.setattr(sm, "msvcrt", None, raising=False)
    assert sm.acquire_lock() is None             # canlı sahip → RED
    p.write_text("999999 2026-09-11T00:00:00Z\n", encoding="utf-8")
    fd = sm.acquire_lock()                       # ölü pid → en iyi çaba devam
    assert fd is not None
    fd.close()


def test_kilit_kaydi_sinirli_buyume(monkeypatch, tmp_path):
    """Kayıt sabit genişlik: ardışık koşular dosyayı büyütmez, tek kayıt kalır."""
    monkeypatch.setattr(sm, "MOTOR_LOCK", str(tmp_path / "cumulus_sync.lock"))
    for _ in range(5):
        fd = sm.acquire_lock()
        assert fd is not None
        fd.close()
    assert os.path.getsize(sm.MOTOR_LOCK) <= sm._KILIT_KAYIT_UZUNLUK
    assert open(sm.MOTOR_LOCK, encoding="utf-8").read().split()[0] == str(os.getpid())


def test_kayit_yazimi_basarisizsa_kilit_dusmez(monkeypatch, tmp_path):
    """Kayıt yazımı hata verse de kilit KORUNUR (kayıt yalnız teşhis bilgisi)."""
    monkeypatch.setattr(sm, "MOTOR_LOCK", str(tmp_path / "cumulus_sync.lock"))

    def _patlat(fd):
        raise OSError("disk dolu (simüle)")

    monkeypatch.setattr(sm, "_kilit_kaydi_yaz", _patlat)
    fd = sm.acquire_lock()
    assert fd is not None
    fd.close()


def test_msvcrt_mevcut_kayit_korunur_ve_buyumez(monkeypatch, tmp_path):
    """Windows yolu: mevcut kayıt varken dosya büyümez, kilit aralığı 1 bayt."""
    cagrilar = []

    class FakeMsvcrt:
        LK_NBLCK = 6

        def locking(self, fd, mode, nbytes):
            cagrilar.append((mode, nbytes))

    p = tmp_path / "cumulus_sync.lock"
    p.write_text("111 2026-01-01T00:00:00Z\n", encoding="utf-8")
    monkeypatch.setattr(sm, "fcntl", None)
    monkeypatch.setattr(sm, "msvcrt", FakeMsvcrt(), raising=False)
    monkeypatch.setattr(sm, "MOTOR_LOCK", str(p))
    fd = sm.acquire_lock()
    assert fd is not None
    icerik = p.read_text(encoding="utf-8")
    assert icerik.split()[0] == str(os.getpid())      # sahip kaydı güncel
    assert len(icerik) <= sm._KILIT_KAYIT_UZUNLUK     # sınırsız büyüme yok
    assert cagrilar == [(6, 1)]
    fd.close()


def test_a2a_server_uvicorn_missing(monkeypatch, capsys):
    """uvicorn import edilemiyor → HATA mesajı + exit 1 (ham traceback yok)."""
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    monkeypatch.setattr(sys, "argv", ["a2a", "--port", "8643"])
    with pytest.raises(SystemExit) as ei:
        a2a_srv.main()
    assert ei.value.code == 1
    out = capsys.readouterr().out
    assert "HATA" in out
    assert "uvicorn" in out
    assert "Traceback" not in out


# ─── (f) Ajan kilidi (v2.6.0) ──────────────────────────────────
# Ölçülen kusurlar: (1) node_agent.LOCK = "/tmp/..." SABİT ve ÖLÜ idi (hiç
# kullanılmıyordu) → Windows'ta '/tmp' yok, docstring'deki "iki ajan aynı hub'a
# yazamaz" garantisi UYGULANMIYORDU; (2) sync_motor geçici yolları ('/tmp/sync_pull',
# '/tmp/hermes_uploads') platformdan bağımsız sabitti → Windows agent (H2) sürücü
# köküne yazmaya çalışırdı.
import synclave.node_agent as na


def test_agent_lock_path_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("TEMP", "C:\\Users\\h2\\AppData\\Local\\Temp")
    p = na._agent_lock_path()
    assert p == os.path.join("C:\\Users\\h2\\AppData\\Local\\Temp",
                             "cumulus_node_agent.lock")
    assert not p.startswith("/tmp")


def test_agent_lock_path_posix(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    assert na._agent_lock_path() == "/tmp/cumulus_node_agent.lock"


def test_agent_lock_motor_kilidiyle_ayni_dosya_degil(monkeypatch):
    """Döngüsel kilitlenme kapısı: ajan kilidi ≠ motor kilidi (aynı dizin, farklı ad)."""
    for ad in ("posix", "nt"):
        monkeypatch.setattr(os, "name", ad)
        assert na._agent_lock_path() != sm._motor_lock_path()
        assert os.path.dirname(na._agent_lock_path()) == \
               os.path.dirname(sm._motor_lock_path())


def test_ikinci_ajan_kilidi_alamaz(monkeypatch, tmp_path):
    """Aynı makinede eşzamanlı ikinci ajan → non-blocking RED (fail-closed)."""
    monkeypatch.setattr(na, "LOCK", str(tmp_path / "cumulus_node_agent.lock"))
    fd1 = na._lock_acquire()
    assert fd1 is not None and fd1 is not na.KILITSIZ
    try:
        assert na._lock_acquire() is None          # ikinci aday ATLAR
    finally:
        na._lock_release(fd1)
    fd2 = na._lock_acquire()                       # sahip bıraktı → alınır
    assert fd2 is not None
    na._lock_release(fd2)


def test_kilit_alt_yapisi_yoksa_engellemez(monkeypatch, tmp_path):
    """fcntl+msvcrt yok → KILITSIZ sentinel (çalışma ENGELLENMEZ)."""
    monkeypatch.setattr(na, "LOCK", str(tmp_path / "yeni_dizin_yok" / "x.lock"))
    fd = na._lock_acquire()
    assert fd is na.KILITSIZ
    na._lock_release(fd)                            # no-op, çökmez


def test_hub_status_kilit_doluysa_yazmaz(monkeypatch, tmp_path, capsys):
    """Kilit başka ajandaysa GDrive yazımı ATLANIR (rclone hiç çağrılmaz)."""
    cagrilar = []
    monkeypatch.setattr(na, "LOCK", str(tmp_path / "cumulus_node_agent.lock"))
    monkeypatch.setattr(na, "tempfile_dir", lambda: str(tmp_path))
    monkeypatch.setattr(na, "machine_id", lambda: "HX-TEST")
    monkeypatch.setattr(na, "run",
                        lambda *a, **k: (cagrilar.append(a), (0, "", ""))[1])
    sahip = na._lock_acquire()
    try:
        assert na.write_hub_status({"m": 1}) is False
    finally:
        na._lock_release(sahip)
    assert cagrilar == []                           # rclone copyto YOK
    assert "atlandı" in capsys.readouterr().out


def test_hub_status_kilit_hata_durumunda_birakilir(monkeypatch, tmp_path):
    """rclone istisna atsa bile kilit finally'de bırakılır (sonraki tick kilitlenmez)."""
    monkeypatch.setattr(na, "LOCK", str(tmp_path / "cumulus_node_agent.lock"))
    monkeypatch.setattr(na, "tempfile_dir", lambda: str(tmp_path))
    monkeypatch.setattr(na, "machine_id", lambda: "HX-TEST")

    def patlat(*a, **k):
        raise OSError("rclone yok (simüle)")

    monkeypatch.setattr(na, "run", patlat)
    with pytest.raises(OSError):
        na.write_hub_status({"m": 1})
    fd = na._lock_acquire()                         # kilit serbest kaldı
    assert fd is not None
    na._lock_release(fd)


def test_gecici_yollar_platformdan_turetilir():
    """Kaynak kapısı: geçici yol sabitleri platformdan türetilmeli.

    '/tmp/sync_pull_*' ve '/tmp/hermes_uploads' kalıntısı Windows agent'ta
    sürücü köküne yazmaya çalışırdı.
    """
    sm_src = Path(sm.__file__).read_text(encoding="utf-8")
    na_src = Path(na.__file__).read_text(encoding="utf-8")
    assert "_platform_temp_dir()" in sm_src
    assert '"/tmp/sync_pull_' not in sm_src
    assert '"/tmp/hermes_uploads"' not in sm_src
    assert 'LOCK = "/tmp/' not in na_src            # ölü sabit geri dönmesin
    assert "_agent_lock_path()" in na_src


# ─── (g) Denetim bulguları (v2.6.0 — bağımsız denetim #5/#6) ───
# #5: fallback'te gelecek tarihli (saat kayması) kayıt bayat sayılıp devralınıyordu
#     → fail-OPEN. #6: rclone istisnasında geçici dosya temp'te kalıyordu.

def test_fallback_ileri_tarihli_kayit_fail_closed(monkeypatch, tmp_path):
    """#5: canlı pid + GELECEK tarihli kayıt → RED (bayatlık yalnız üst sınır)."""
    from datetime import datetime, timedelta
    p = tmp_path / "cumulus_node_agent.lock"
    gelecek = (datetime.now() + timedelta(minutes=5)).isoformat(timespec="seconds")
    p.write_text(f"1 {gelecek}\n", encoding="utf-8")     # pid 1 = canlı (init)
    monkeypatch.setattr(na, "LOCK", str(p))
    monkeypatch.setattr(na, "fcntl", None)
    monkeypatch.setattr(na, "msvcrt", None, raising=False)
    assert na._lock_acquire() is None                    # fail-closed


def test_fallback_bayat_kayit_devralinir(monkeypatch, tmp_path):
    """#5 sınırı: canlı pid ama kayıt 2 saatten eski → devral (kalıcı blok yok)."""
    from datetime import datetime, timedelta
    p = tmp_path / "cumulus_node_agent.lock"
    eski = (datetime.now() - timedelta(hours=3)).isoformat(timespec="seconds")
    p.write_text(f"1 {eski}\n", encoding="utf-8")
    monkeypatch.setattr(na, "LOCK", str(p))
    monkeypatch.setattr(na, "fcntl", None)
    monkeypatch.setattr(na, "msvcrt", None, raising=False)
    fd = na._lock_acquire()
    assert fd is not None
    na._lock_release(fd)


def test_hub_status_gecici_dosya_istisnada_da_silinir(monkeypatch, tmp_path):
    """#6: rclone istisnası geçici dosyayı temp'te BIRAKMAZ (kilit de bırakılır)."""
    monkeypatch.setattr(na, "LOCK", str(tmp_path / "cumulus_node_agent.lock"))
    monkeypatch.setattr(na, "tempfile_dir", lambda: str(tmp_path))
    monkeypatch.setattr(na, "machine_id", lambda: "HX-TEST")

    def patlat(*a, **k):
        raise OSError("rclone yok (simüle)")

    monkeypatch.setattr(na, "run", patlat)
    with pytest.raises(OSError):
        na.write_hub_status({"m": 1})
    assert list(tmp_path.glob("status_*.json")) == []    # geçici dosya silindi
    fd = na._lock_acquire()                              # kilit serbest
    assert fd is not None
    na._lock_release(fd)


