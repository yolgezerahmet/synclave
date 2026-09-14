#!/usr/bin/env python3
"""restic fail-soft + retention güvenlik kapısı (v2.7.4).

Neden: 14 Eyl 2026'da ölçülen "retention açlığı" zinciri (twin commit `f7edde12`,
tick içinde public'e taşındı):

  1. `_restic` içinde `subprocess.run(..., timeout=...)` zaman aşımında
     `TimeoutExpired` FIRLATIR; sarmalayıcı bunu YAKALAMIYORDU → `cmd_backup`
     traceback ile çöküyordu: rc yok, rapor yok, log yok (sessiz ölüm).
  2. SIGKILL edilen restic süreci kilidini BIRAKAMAZ → repoda yetim kilit kalır
     (ölçüm: 17:54:56 lock `"exclusive":false`, pid 532056 ÖLÜ).
  3. `forget` bu kilit yüzünden eskiden `--retry-lock 30m` boyunca bekleyip yine
     öldürülüyordu → forget aylardır hiç tamamlanmadı, 667 snapshot birikti.

Bu kapı iki şeyi zorlar:
  A. restore fail-soft: timeout → `(-1, "TIMEOUT Ns ...")`, İSTİSNA YOK, süre ve
     çıktı kuyruğu mesajda (kanıt izi).
  B. retention güvenliği: yetim kilit süpürme `unlock` ile yapılır ve
     `--remove-all` KULLANILMAZ (uzak makinenin CANLI kilidi silinmemeli);
     forget sınırlı timeout alır; davranış env ile ayarlanabilir
     (SYNC_RETENTION_RETRY_LOCK / _TIMEOUT / _UNLOCK / _DRY_RUN).
"""
import subprocess
import types
from pathlib import Path

import pytest

import synclave.sync_motor as sm

REPO = Path(__file__).resolve().parent.parent
KOK = (REPO / "sync_motor.py").read_text(encoding="utf-8")


def _sahte_subprocess(stdout=None, stderr=None, kayit=None):
    """`subprocess` yerine geçen sahte modül; run() TimeoutExpired fırlatır."""
    def _run(cmd, **kw):
        if kayit is not None:
            kayit.update({"cmd": cmd, **kw})
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"),
                                        output=stdout, stderr=stderr)
    return types.SimpleNamespace(run=_run, TimeoutExpired=subprocess.TimeoutExpired)


@pytest.fixture()
def restic_hazir(monkeypatch):
    """restic binary + parola var gibi göster (aksi halde erken return olur)."""
    monkeypatch.setattr(sm.shutil, "which", lambda ad: "/usr/bin/restic")
    monkeypatch.setenv(sm.RESTIC_PASS_ENV, "test-parola")


def test_timeout_fail_soft_istisna_firlatmaz(restic_hazir, monkeypatch):
    """Zaman aşımı → (rc=-1, 'TIMEOUT Ns ...'); istisna ÇAĞIRANA ULAŞMAZ."""
    monkeypatch.setattr(sm, "subprocess", _sahte_subprocess())
    rc, out = sm._restic(["snapshots"], timeout=5)
    assert rc == -1, f"timeout rc=-1 olmalı (fail-soft), gelen: {rc!r}"
    assert "TIMEOUT 5s" in out, f"timeout süresi mesajda olmalı, gelen: {out!r}"


def test_timeout_mesaji_cikti_kuyrugunu_tasir(restic_hazir, monkeypatch):
    """Mesaj, restic çıktı kuyruğunu içerir (retention teşhisi için kanıt izi)."""
    monkeypatch.setattr(sm, "subprocess",
                        _sahte_subprocess(stdout=b"snapshot abc\n",
                                          stderr=b"repo locked\n"))
    rc, out = sm._restic(["forget", "--keep-monthly", "6"], timeout=900)
    assert rc == -1
    assert "TIMEOUT 900s" in out
    assert "repo locked" in out, f"stderr kuyruğu mesajda yok: {out!r}"


def test_timeout_degeri_subprocess_e_gecirilir(restic_hazir, monkeypatch):
    """`timeout` parametresi subprocess.run'a AYNEN geçer (sessiz varsayılana düşmez)."""
    kayit = {}
    monkeypatch.setattr(sm, "subprocess", _sahte_subprocess(kayit=kayit))
    sm._restic(["unlock"], timeout=180)
    assert kayit.get("timeout") == 180, f"timeout geçmedi: {kayit.get('timeout')!r}"
    assert kayit.get("cmd")[0].endswith("restic"), "komut restic binary ile başlamalı"


# ─── retention güvenliği (kaynak invariant'ları) ─────────────────────
def _kod_metni(kaynak: str) -> str:
    """Yorumları at — uyarı metinlerindeki ('--remove-all DEĞİL') anmalar
    koda dair kanıt DEĞİLDİR; kapı yalnız gerçek çağrı argümanlarına bakar."""
    satirlar = []
    for satir in kaynak.splitlines():
        # satır içi yorumu kes (tırnak içi '#' nadir; bu kapı için yeterli)
        kod = satir.split("#", 1)[0]
        satirlar.append(kod)
    return "\n".join(satirlar)


KOD = _kod_metni(KOK)


def test_yetim_kilit_supurme_uzak_canli_kilidi_silmez():
    """`unlock` kullanılır; `--remove-all` ASLA (uzak makinenin canlı kilidi korunur)."""
    assert '["unlock"]' in KOD, "yetim kilit süpürme (restic unlock) yok"
    assert '"--remove-all"' not in KOD and "'--remove-all'" not in KOD, (
        "`--remove-all` KODDA kullanılmış — bu uzak makinenin CANLI kilidini de "
        "siler (fail-open yazma riski)")


def test_retention_sinirli_timeout_ve_env_knoblari():
    """forget sınırlı timeout alır + davranış env ile ayarlanabilir (sessiz ölüm yok)."""
    assert 'os.environ.get("SYNC_RETENTION_TIMEOUT", "900")' in KOK, \
        "SYNC_RETENTION_TIMEOUT (varsayılan 900s) yok"
    assert 'os.environ.get("SYNC_RETENTION_RETRY_LOCK", "2m")' in KOK, \
        "SYNC_RETENTION_RETRY_LOCK (varsayılan 2m) yok"
    assert 'os.environ.get("SYNC_RETENTION_UNLOCK", "1")' in KOK, \
        "SYNC_RETENTION_UNLOCK anahtarı yok"
    assert 'os.environ.get("SYNC_RETENTION_DRY_RUN", "")' in KOK, \
        "SYNC_RETENTION_DRY_RUN anahtarı yok"
    assert "_restic(args, timeout=ret_timeout)" in KOK, \
        "forget sınırlı timeout ile çağrılmıyor (sessiz SIGKILL geri gelir)"


def test_retention_eski_sinirsiz_retry_lock_geri_gelmedi():
    """30m'lik eski retry-lock (tüm bütçeyi yiyen kısır döngü) geri gelmemeli."""
    assert '"--retry-lock", "30m"' not in KOK, (
        "eski `--retry-lock 30m` geri gelmiş — node fazı bütçeyi yiyip forget'i "
        "yine SIGKILL'e götürür")
