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


# ─── v2.7.5 retention SIRASI (14 Eyl 2026, tick 089e3b2575f6) ───────────────
# İkinci kök neden: forget adımı cmd_restic_backup SONUNDA çağrılıyordu; Hermes
# cron script timeout'u sabit 3600 s (scheduler.py `_DEFAULT_SCRIPT_TIMEOUT`),
# node döngüsü ~24 dk ölçüldü → forget uzun koşularda HİÇ çalışmıyordu
# (snapshot 667 -> 725, +58/gün). Kapı: retention node döngüsünden ÖNCE de çağrılmalı.
def test_retention_once_kaynagi_tek_fonksiyon():
    """Retention tek fonksiyonda toplanır (iki çağrı noktası, tek gövde)."""
    src = (REPO / "sync_motor.py").read_text(encoding="utf-8")
    assert src.count("def _restic_retention(") == 1, "retention gövdesi tek olmalı"
    # Eski gömülü blok kalmamalı (çift gövde = davranış sapması)
    assert src.count('print(f"    🧹 retention: OK') == 1
    assert src.count('"--keep-daily", "7"') == 1


def test_retention_node_dongusunden_once_cagrilir():
    """SIRA KAPISI: ilk _restic_retention çağrısı, ilk node döngüsünden ÖNCE."""
    src = (REPO / "sync_motor.py").read_text(encoding="utf-8")
    govde = src[src.index("def cmd_restic_backup("):]
    cagri = govde.index("_restic_retention(cfg, dry_run)")
    dongu = govde.index("for n in nodes:")
    assert cagri < dongu, "retention ÖNCE çağrılmıyor — 3600 s bütçesi onu açlığa iter"
    # Ve sonda da (idempotent) çağrı kalmalı
    assert govde.count("_restic_retention(cfg, dry_run)") >= 2
    assert "SYNC_RETENTION_ORDER" in govde
    assert '"both"' in govde


def test_retention_sira_knobu_gecerli_degerler():
    """Sıra knobu normalize edilir; tanınmayan değer fail-safe 'both'a düşer.

    v2.7.8: knob artık default-argümansız okunur (normalize + fail-safe düşüş).
    """
    src = (REPO / "sync_motor.py").read_text(encoding="utf-8")
    assert 'os.environ.get("SYNC_RETENTION_ORDER")' in src
    assert ".strip().lower()" in src, "knob normalize edilmiyor (büyük harf/boşluk)"
    assert '_ret_order not in ("first", "last", "both")' in src, (
        "tanınmayan knob değeri için fail-safe düşüş yok → retention sessizce "
        "hiç çalışmaz (v2.7.5 açlığı typo ile geri gelir)")
    assert '_ret_order in ("first", "both")' in src
    assert '_ret_order in ("last", "both")' in src


# ─── DAVRANIŞ KAPISI (17 Eyl 2026 — bağımsız denetim bulgusu, gpt-5.6-sol) ───
# Yukarıdaki üç kapı kaynak METNİNİ sayar; kırılgandır (yorum/tırnak/biçim
# değişimi işlev doğruyken testi kırar) ve asıl sözleşmeyi KANITLAMAZ:
# SYNC_RETENTION_ORDER knobu retention'ın KAÇ KEZ ve NEREDE çağrıldığını
# belirler. Bu kapı mock'lu çalıştırmayla sayı + sıra ölçer — gerçek restic
# ÇAĞRILMAZ, node döngüsü sahte olay kaydıyla izlenir.
def _retention_kos(monkeypatch, order, node_sayisi=2):
    """cmd_restic_backup'ı mock'lu koştur; olay dizisini döndür.

    Olaylar: ("retention",) veya ("backup", <node yolu>), çağrı sırasıyla.
    """
    olaylar = []
    cfg = {"dirs": {f"n{i}": {"path": f"/sahte/kaynak-{i}"}
                    for i in range(node_sayisi)}}
    monkeypatch.setattr(sm, "_restic_retention",
                        lambda c, d=False: olaylar.append(("retention",)))
    monkeypatch.setattr(
        sm, "_restic",
        lambda args, **kw: (olaylar.append(("backup", args[1])), (0, ""))[1])
    monkeypatch.setattr(sm.os.path, "exists", lambda p: True)
    if order is None:
        monkeypatch.delenv("SYNC_RETENTION_ORDER", raising=False)
    else:
        monkeypatch.setenv("SYNC_RETENTION_ORDER", order)
    sm.cmd_restic_backup(cfg, dry_run=False)
    return olaylar


@pytest.mark.parametrize("order,beklenen", [
    ("first", 1),
    ("last", 1),
    ("both", 2),
    (None, 2),          # knop YOK → varsayılan "both" (fail-safe)
])
def test_retention_sayisi_ve_sirasi_davranissal(monkeypatch, order, beklenen):
    """DAVRANIŞ: retention çağrı sayısı + node döngüsüne göre SIRASI."""
    olaylar = _retention_kos(monkeypatch, order)
    r = [i for i, o in enumerate(olaylar) if o[0] == "retention"]
    n = [i for i, o in enumerate(olaylar) if o[0] == "backup"]
    assert n, "node backup'ları hiç çağrılmadı (mock kurulumu bozuk)"
    assert len(r) == beklenen, (
        f"SYNC_RETENTION_ORDER={order!r}: {beklenen} retention beklenirdi, "
        f"{len(r)} çağrıldı — retention açlığı regresyonu")
    if order == "first":
        assert r[0] < n[0], "first: retention node döngüsünden ÖNCE olmalı"
    elif order == "last":
        assert r[-1] > n[-1], "last: retention node döngüsünden SONRA olmalı"
    else:
        assert r[0] < n[0] and r[-1] > n[-1], (
            "both/varsayılan: biri döngüden ÖNCE, biri SONRA olmalı "
            "(3600 s bütçesi sonda kalan forget'i açlığa iter)")


def test_retention_node_basina_degil_dongu_disinda(monkeypatch):
    """DAVRANIŞ: node sayısı artınca retention çağrı sayısı ARTMAZ (döngü dışı)."""
    iki = _retention_kos(monkeypatch, None, node_sayisi=2)
    bez = _retention_kos(monkeypatch, None, node_sayisi=5)
    r2 = sum(1 for o in iki if o[0] == "retention")
    r5 = sum(1 for o in bez if o[0] == "retention")
    assert r2 == r5 == 2, (
        f"retention node başına çağrılıyor olabilir: 2 node→{r2}, 5 node→{r5} "
        "(döngü dışında 2 olmalı)")


# ─── v2.7.8 FAİL-SAFE KAPISI (17 Eyl 2026 — DONE-CHECK denetim bulgusu) ─────
# Ölçülen kusur (fix ÖNCESİ): knob'a TANINMAYAN değer gelince ('xyz' / 'FIRST'
# / '' / 'first,last') iki dal da tutmuyordu → retention 0 kez çağrılıyordu,
# yani v2.7.5'te kapatılan "retention açlığı" bir yazım hatasıyla SESSİZCE geri
# geliyordu (fail-open). Bu kapı hem normalize'ı hem fail-safe düşüşü ölçer.
@pytest.mark.parametrize("deger,beklenen", [
    ("FIRST", 1),      # büyük harf → normalize
    (" Both ", 2),     # boşluklu/karışık harf → normalize
    ("LAST", 1),       # büyük harf → normalize
])
def test_retention_knob_normalize_edilir(monkeypatch, deger, beklenen):
    """DAVRANIŞ: knob büyük harf/boşluk toleranslı (normalize) çalışır."""
    olaylar = _retention_kos(monkeypatch, deger)
    r = [i for i, o in enumerate(olaylar) if o[0] == "retention"]
    assert len(r) == beklenen, (
        f"{deger!r} normalize edilmedi: {beklenen} çağrı beklenirdi, "
        f"{len(r)} alındı")


@pytest.mark.parametrize("deger", ["xyz", "", "first,last", "0", "true", "both "])
def test_retention_taninmayan_knob_fail_safe(monkeypatch, deger):
    """DAVRANIŞ: tanınmayan knob retention'ı KAPATMAZ → fail-safe 'both' (2)."""
    olaylar = _retention_kos(monkeypatch, deger)
    r = [i for i, o in enumerate(olaylar) if o[0] == "retention"]
    n = [i for i, o in enumerate(olaylar) if o[0] == "backup"]
    assert len(r) == 2, (
        f"{deger!r}: fail-safe 'both' beklenirdi (2 çağrı), {len(r)} alındı — "
        "tanınmayan knob değeri retention'ı sessizce kapatıyor (fail-open)")
    assert r[0] < n[0] < r[-1], (
        f"{deger!r}: fail-safe turunda biri döngüden ÖNCE biri SONRA olmalı")


# ─── KENAR DURUM KAPILARI (17 Eyl 2026 — son denetim önerisi) ───────────────
def test_retention_node_listesi_bosken_de_uygulanir(monkeypatch):
    """DAVRANIŞ: tüm node'lar restic:False olsa da retention ÇALIŞIR.

    Yedeklenecek node kalmasa bile kendi snapshot'larımız budanmalı — aksi
    halde "node yok" günü retention sessizce atlanır ve birikim sürer.
    """
    olaylar = []
    cfg = {"dirs": {"a": {"path": "/sahte/a", "restic": False},
                    "b": {"path": "/sahte/b", "restic": False}}}
    monkeypatch.setattr(sm, "_restic_retention",
                        lambda c, d=False: olaylar.append(("retention",)))
    monkeypatch.setattr(sm, "_restic",
                        lambda args, **kw: olaylar.append(("backup", args[1])))
    monkeypatch.setattr(sm.os.path, "exists", lambda p: True)
    monkeypatch.delenv("SYNC_RETENTION_ORDER", raising=False)
    sm.cmd_restic_backup(cfg, dry_run=False)
    assert [o[0] for o in olaylar] == ["retention", "retention"], (
        "node kalmadığında retention atlanmamalı (snapshot birikimi)")
    assert not [o for o in olaylar if o[0] == "backup"], (
        "restic:False node yedeklenmemeli (skip_nodes filtresi)")


def test_node_dongusu_hatasinda_en_az_bir_retention_turu(monkeypatch):
    """SINIR (belgelenmiş): node döngüsünde istisna → 'first' turu TAMAMLANMIŞ olur.

    Tasarım sınırı: sonda kalan 'last' çağrısına ulaşılmaz (istisna yayılır).
    Bu yüzden varsayılan 'both' seçildi — bütçe/istisna durumunda bile en az
    bir retention turu çalışmış olur (v2.7.5 açlığının kökü buydu).
    """
    olaylar = []
    cfg = {"dirs": {"a": {"path": "/sahte/a"}}}

    def patlat(args, **kw):
        raise RuntimeError("simule node hatasi")

    monkeypatch.setattr(sm, "_restic_retention",
                        lambda c, d=False: olaylar.append("retention"))
    monkeypatch.setattr(sm, "_restic", patlat)
    monkeypatch.setattr(sm.os.path, "exists", lambda p: True)
    monkeypatch.delenv("SYNC_RETENTION_ORDER", raising=False)
    with pytest.raises(RuntimeError):
        sm.cmd_restic_backup(cfg, dry_run=False)
    assert olaylar.count("retention") == 1, (
        "istisna halinde ilk (first) retention tamamlanmış olmalı — en az bir "
        "tur garantisi; ikinci çağrıya ulaşılmaz (belgelenmiş sınır)")
