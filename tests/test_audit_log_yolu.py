#!/usr/bin/env python3
"""Audit log yolu tek-kaynak kapısı (v2.7.1).

Neden (14 Eyl 2026 — iki GERÇEK kırmızı self-check testi):

`append_audit_event` audit dizininde log dosyasının YANINA kalıcı bir kilit
dosyası bırakır (`<tarih>.jsonl.lock` — 29 Ağu'daki "oku+ekle atomik" düzeltmesi).
Kökteki iki self-check betiği log dosyasını `os.listdir(audit_dir)[0]` ile
keşfediyordu; liste sırası dosya sistemine bağlı olduğu için bazen BOŞ kilit
dosyası seçiliyordu ve iki ayrı hata doğuyordu:

  * `test_sync_memory.py` → `f.readlines()` boş → `lines[0]` → IndexError
  * `test_memory_fixes.py` → `_audit_last_hash(boş dosya)` = "0"*64 ≠ h2 → FAIL

Sonuç: kapı KARARSIZDI — aynı kod bir makinede yeşil, başka bir makinede/CI'da
kırmızı (retry/backoff ve Windows işleri YEŞİLKEN suite kırmızı kalıyordu).

Bu test üç şeyi zorlar:
  1. Yol tek kaynaktan gelir (`audit_log_path`) ve `.lock` ASLA dönmez.
  2. Üretim fonksiyonları (`append_audit_event` / `verify_audit_chain`) aynı
     yolu kullanır; kilit kardeşi zinciri etkilemez.
  3. Betikler listelemeye dayalı keşfe GERİ DÖNEMEZ (kök neden kalıcı kapı).
     Kapsam sınırı: kapı doğrudan `listdir(...)[0]` regresyonunu yakalar;
     obfuscation sınıfı (`getattr(os, "listdir")`, `os.scandir`, `Path.iterdir`)
     kapsam DIŞIDIR (gpt-5.6-sol denetimi, 14 Eyl 2026). Amaç semantik
     eksiksizlik değil, bilinen anti-pattern'in sessizce geri gelmesini
     engellemek.
"""
import ast
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import synclave.sync_memory as smem  # noqa: E402

# Kırmızıya düşen betikler (public: kök, private: tests/manual/).
SELF_CHECK = ("test_memory_fixes.py", "test_sync_memory.py")

# Yasak desen: `<ifade>.listdir(<...>)[0]` ile dosya keşfi (AST ile, KOD düzeyinde).
def _listdir_sifir_kesfi(src):
    """`listdir(...)[0]` biçiminde dosya keşfini AST'de arar → satır numaraları.

    Regex DEĞİL AST: düzeltmenin gerekçesini anlatan yorum satırındaki örnek
    desen kapıyı yanlış kırmızıya çevirmemeli (14 Eyl 2026'da tam bu oldu —
    betikler zaten düzeltilmişken yorumdaki `os.listdir(...)[0]` örneği kapıyı
    kırdı). Kapı kodu denetler, metni değil.
    """
    bulunan = []
    for dugum in ast.walk(ast.parse(src)):
        if not isinstance(dugum, ast.Subscript):
            continue
        dilim = dugum.slice
        if not (isinstance(dilim, ast.Constant) and dilim.value == 0):
            continue
        cagri = dugum.value
        if (isinstance(cagri, ast.Call) and isinstance(cagri.func, ast.Attribute)
                and cagri.func.attr == "listdir"):
            bulunan.append(dugum.lineno)
    return bulunan


def _betik(yol_adi):
    for d in (REPO, REPO / "tests" / "manual"):
        p = d / yol_adi
        if p.is_file():
            return p
    return None


# ─── 1. Yol tek kaynak + .lock değil ────────────────────────────────────────
def test_yardimci_log_dosyasini_dondurur(tmp_path):
    p = smem.audit_log_path(str(tmp_path))
    assert p.endswith(".jsonl"), p
    assert not p.endswith(".jsonl.lock"), "kilit yolu log yolu olarak döndü"
    assert os.path.dirname(p) == str(tmp_path)
    beklenen = datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl"
    assert os.path.basename(p) == beklenen


def test_ayni_ifade_mukerrer_degil():
    """Tek kaynak: tarihli dosya adı ifadesi üretimde yalnız yardımcıda kurulur."""
    src = (REPO / "synclave" / "sync_memory.py").read_text(encoding="utf-8")
    assert 'strftime("%Y-%m-%d") + ".jsonl"' in src
    # İfade tam olarak bir kez geçmeli (kopyalanırsa sapma riski geri gelir).
    assert src.count('strftime("%Y-%m-%d") + ".jsonl"') == 1


# ─── 2. Kök neden kanıtı: boş .lock kardeşi ─────────────────────────────────
def test_kilit_kardesi_olusur_ve_bostur(tmp_path):
    ad = str(tmp_path / "audit")
    smem.append_audit_event(ad, {"node_id": "H1", "event_type": "test"})
    log = smem.audit_log_path(ad)
    assert os.path.exists(log)
    lock = log + ".lock"
    assert os.path.exists(lock), "kilit kardeşi bekleniyordu (kök neden)"
    assert os.path.getsize(lock) == 0, "kilit dosyası veri taşımamalı"
    # Yardımcı log'u, listdir[0] ise riski işaret eder.
    assert smem._audit_last_hash(log) != "0" * 64


def test_eski_kesif_mantigi_yanlis_dosyayi_secer(tmp_path):
    """Eski `listdir(...)[0]` mantığı en kötü sırada YANLIŞ dosyayı seçer.

    Deterministik: dosya sistemi sırasına GÜVENİLMEZ, en kötü sıra elle verilir
    (`["<tarih>.jsonl.lock", "<tarih>.jsonl"]`) ve eski seçim mantığı bu liste
    üzerinde modellenir.
    """
    ad = str(tmp_path / "audit")
    smem.append_audit_event(ad, {"node_id": "H1", "event_type": "test"})
    log = smem.audit_log_path(ad)
    en_kotu_sira = [log + ".lock", log]          # kilit önce gelirse
    eski_secim = en_kotu_sira[0]                 # eski betik mantığı
    assert eski_secim == log + ".lock"
    # Boş dosyada zincir başı hash'i döner → gerçek hash değil (yanlış FAIL).
    assert smem._audit_last_hash(eski_secim) == "0" * 64
    assert smem._audit_last_hash(log) != "0" * 64
    assert smem.audit_log_path(ad) == log


# ─── 3. Üretim yolları yardımcıyla aynı yolu kullanır ───────────────────────
def test_append_ve_verify_yardimciyla_ayni_yol(tmp_path):
    ad = str(tmp_path / "audit")
    h1 = smem.append_audit_event(ad, {"node_id": "H1", "event_type": "a"})
    h2 = smem.append_audit_event(ad, {"node_id": "H1", "event_type": "b"})
    log = smem.audit_log_path(ad)
    assert smem._audit_last_hash(log) == h2 != h1
    v = smem.verify_audit_chain(ad)
    assert v["ok"] and v["events"] == 2, v
    # Kilit kardeşi tek başına zinciri bozmamalı (boş dosya zincire girmez).
    satirlar = [ln for ln in Path(log).read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(satirlar) == 2
    assert Path(log).with_name(Path(log).name + ".lock").stat().st_size == 0


def test_zincir_bozulmasi_hala_yakalanir(tmp_path):
    ad = str(tmp_path / "audit")
    smem.append_audit_event(ad, {"node_id": "H1", "event_type": "a"})
    smem.append_audit_event(ad, {"node_id": "H1", "event_type": "b"})
    log = smem.audit_log_path(ad)
    satirlar = [ln for ln in Path(log).read_text(encoding="utf-8").splitlines() if ln.strip()]
    ev0 = json.loads(satirlar[0])
    ev0["event_type"] = str(ev0.get("event_type", "a")) + "_tampered"
    satirlar[0] = json.dumps(ev0, ensure_ascii=False)
    Path(log).write_text("\n".join(satirlar) + "\n", encoding="utf-8")
    v = smem.verify_audit_chain(ad)
    assert not v["ok"], "bozulmuş zincir yeşil döndü"


# ─── 4. Betikler listelemeye dayalı keşfe dönemez ───────────────────────────
@pytest.mark.parametrize("ad", SELF_CHECK)
def test_self_check_betikleri_listeleme_kullanmaz(ad):
    p = _betik(ad)
    if p is None:
        pytest.skip(f"{ad} bu kopyada yok (ikiz/private klon)")
    src = p.read_text(encoding="utf-8")
    bulunan = _listdir_sifir_kesfi(src)
    assert not bulunan, (
        f"{ad}: `listdir(...)[0]` ile dosya keşfi geri döndü (satır {bulunan}) — "
        "audit dizininde .lock kardeşi vardır, kapı kararsızlaşır"
    )
    assert "audit_log_path(" in src, f"{ad}: yol üretim yardımcısından alınmıyor"


# ─── 5. Gün devri (UTC gece yarısı) ─────────────────────────────────────────
def test_gun_devri_verify_hedefi_kaymaz(tmp_path, monkeypatch):
    """UTC gece yarısını aşan 'yaz + doğrula': doğrulama YAZILAN dosyayı hedefler.

    Bulgu (14 Eyl 2026, gpt-5.6-sol kritik denetimi): yazma ve doğrulama AYRI
    çağrılardır; varsayılan yol "bugün"ü seçtiği için gün devrinde ertesi günün
    dosyasına bakar ve SAĞLAM zincir "log yok" ile kırmızı görünür. Çağıran,
    yazdığı dosyanın yolunu bir kez üretip `log_path` ile geçirirse doğrulama
    doğru dosyada yapılır; varsayılan davranış değişmez.
    """
    import datetime as _dt

    class _Saat:
        gun = 14

        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(2026, 9, cls.gun, 23, 59, 59, tzinfo=tz)

    monkeypatch.setattr(smem, "datetime", _Saat)
    ad = str(tmp_path / "audit")
    smem.append_audit_event(ad, {"node_id": "H1", "event_type": "gece"})
    yazilan = smem.audit_log_path(ad)
    assert os.path.basename(yazilan) == "2026-09-14.jsonl"

    _Saat.gun = 15                                        # gece yarısı geçti
    assert not smem.verify_audit_chain(ad)["ok"], "varsayılan yol ertesi günü hedefler"
    v = smem.verify_audit_chain(ad, log_path=yazilan)      # açık yol: doğru dosya
    assert v["ok"] and v["events"] == 1, v
