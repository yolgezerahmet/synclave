#!/usr/bin/env python3
"""Hafıza birleştirme testleri — MEMORY.md / USER.md § kayıtları APPEND-ONLY union.

Neden (13 Eyl 2026 olayı): iki makine aynı anda hafıza dosyasına kayıt eklediğinde
GDrive pull yolu çakışma kopyası (.conflict.TS) üretiyordu — bilgi kaybolmasa da
dosya birikiyordu. İlk birleştirme denemesi kayıtları GERİ YAZIYORDU (tam rewrite)
ve üç kayıp riski taşıyordu (gpt-5.6-sol kritik denetimi, 5 bulgu):
  1) uzunluk filtresi (len>20) kısa kayıtları siliyordu,
  2) ilk-140-karakter önek anahtarı farklı kayıtları tekilleştiriyordu,
  3) read→rewrite arası eşzamanlı append son-yazan-kazanır ile siliniyordu.

Bu testler GERÇEK üretim fonksiyonunu (`synclave.sync_motor.memory_merge_append`,
dönüş: `(eklenen, uzak_yerelin_alt_kumesi)`) ve çağrı yerini (`gdrive_pull_latest`)
doğrular. Kullanıcı kuralı: "bellek içeriği ASLA silinmez".
"""
import ast
import inspect
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import synclave.sync_motor as sm

REPO = Path(__file__).resolve().parent.parent
KAYNAK = (REPO / "sync_motor.py").read_text(encoding="utf-8")


def _yaz(p, *kayitlar):
    """§ ayırıcılı dosya yaz (üretim biçimi: kayıtlar '\\n§\\n' ile dizilir)."""
    p.write_text("\n§\n".join(kayitlar) + "\n", encoding="utf-8")
    return p


UZUN_A = "A makinesi: kanal seçimi için RSSI ölçümü 12 saniyede tamamlandı, kayıt uzun."
UZUN_B = "B makinesi: yedek rotasyonu 30 dakikalık retry-lock ile tamamlandı, uzun kayıt."
KISA = "kısa kayıt"          # 10 karakter — eski filtre (len>20) bunu SİLİYORDU


# ─── kayıt ayrıştırma ───────────────────────────────────────────────────────

def test_kayitlar_ayristirilir():
    assert sm.memory_records(f"{UZUN_A}\n§\n{UZUN_B}\n") == [UZUN_A, UZUN_B]


def test_kisa_kayit_elenmez():
    """Regresyon: uzunluk filtresi kısa kaydı birleştirme dışına atıyordu."""
    recs = sm.memory_records(f"{UZUN_A}\n§\n{KISA}\n§\n{UZUN_B}\n")
    assert KISA in recs, "kısa § kaydı ayrıştırmada elendi"
    assert len(recs) == 3


def test_crlf_ve_bosluklu_ayirici_toleransi():
    assert sm.memory_records(f"{UZUN_A}\r\n§\r\n{UZUN_B}\r\n") == [UZUN_A, UZUN_B]
    assert sm.memory_records(f"{UZUN_A}\n  §  \n{UZUN_B}\n") == [UZUN_A, UZUN_B]


def test_satir_ortasi_ayrac_bolmez():
    """Kayıt İÇİNDEKİ '§' (satır ortası) kayıt sınırı sayılmaz."""
    tek = "kayıt içinde § işareti geçiyor ama satır başında değil"
    assert sm.memory_records(tek + "\n") == [tek]


def test_bos_kayitlar_atlanir():
    assert sm.memory_records(f"{UZUN_A}\n§\n\n§\n   \n§\n{UZUN_B}\n") == [UZUN_A, UZUN_B]


# ─── append davranışı ───────────────────────────────────────────────────────

def test_yeni_kayit_append_edilir_ve_mevcut_korunur(tmp_path):
    p = _yaz(tmp_path / "MEMORY.md", UZUN_A)
    onceki = p.read_bytes()
    eklenen, alt = sm.memory_merge_append(p, f"{UZUN_A}\n§\n{UZUN_B}\n".encode())
    assert (eklenen, alt) == (1, False)
    veri = p.read_bytes()
    assert veri.startswith(onceki), "mevcut bayt öneki değişti (tam rewrite yapılmış)"
    assert sm.memory_records(p.read_text(encoding="utf-8")) == [UZUN_A, UZUN_B]


def test_kisa_kayit_birlestirmede_korunur(tmp_path):
    """Kritik regresyon: kısa kayıt birleştirme SONRASI da dosyada olmalı."""
    p = _yaz(tmp_path / "MEMORY.md", UZUN_A, KISA)
    eklenen, _ = sm.memory_merge_append(p, f"{UZUN_A}\n§\n{UZUN_B}\n".encode())
    assert eklenen == 1
    sonrasi = p.read_text(encoding="utf-8")
    assert KISA in sonrasi, "birleştirme kısa kaydı sildi"
    assert UZUN_B in sonrasi


def test_onek_cakismasi_iki_kaydi_da_korur(tmp_path):
    """İlk 140 karakteri AYNI, kuyruğu farklı iki kayıt → ikisi de kalmalı."""
    ortak = "X" * 200
    a, b = ortak + " | kuyruk A", ortak + " | kuyruk B"
    p = _yaz(tmp_path / "MEMORY.md", UZUN_A, a)
    eklenen, _ = sm.memory_merge_append(p, f"{UZUN_A}\n§\n{b}\n".encode())
    assert eklenen == 1, "önek çakışması yeni kaydı düşürdü"
    recs = sm.memory_records(p.read_text(encoding="utf-8"))
    assert a in recs and b in recs, "önek anahtarı iki farklı kaydı tekilleştirdi"


def test_ayrisan_kayitlar_birlesir(tmp_path):
    """Her iki makinenin ÖZEL kaydı varsa ikisi de dosyada kalır (union)."""
    p = _yaz(tmp_path / "MEMORY.md", UZUN_A)
    eklenen, _ = sm.memory_merge_append(p, f"{UZUN_B}\n".encode())
    assert eklenen == 1
    assert sm.memory_records(p.read_text(encoding="utf-8")) == [UZUN_A, UZUN_B]


def test_uzak_alt_kume_ise_cakisma_degil(tmp_path):
    """Uzak kayıtların tamamı yerelde varsa: ekleme YOK, kopya YOK (alt küme)."""
    p = _yaz(tmp_path / "MEMORY.md", UZUN_A, UZUN_B)
    onceki = p.read_bytes()
    mtime = p.stat().st_mtime_ns
    eklenen, alt = sm.memory_merge_append(p, f"{UZUN_A}\n".encode())
    assert (eklenen, alt) == (0, True)
    assert p.read_bytes() == onceki
    assert p.stat().st_mtime_ns == mtime, "alt-küme durumunda dosya yazıldı"


def test_uzak_dogru_alt_kume_karari_verir(tmp_path):
    """Yerel ⊂ uzak ise alt küme DENMEZ (eklenen > 0)."""
    p = _yaz(tmp_path / "MEMORY.md", UZUN_A)
    eklenen, alt = sm.memory_merge_append(p, f"{UZUN_A}\n§\n{UZUN_B}\n".encode())
    assert eklenen == 1 and alt is False


def test_yapisiz_tek_kayitli_dosya_append_edilir(tmp_path):
    """§ yok ama içerik var: tek kayıt geçerli kabul edilir, kayıp olmaz."""
    p = tmp_path / "MEMORY.md"
    p.write_text("tek parça içerik, ayraç yok\n", encoding="utf-8")
    eklenen, alt = sm.memory_merge_append(p, b"baska makinenin icerigi\n")
    assert (eklenen, alt) == (1, False)
    icerik = p.read_text(encoding="utf-8")
    assert "tek parça içerik" in icerik and "baska makinenin icerigi" in icerik


def test_bos_dosyaya_birlesme_yapilmaz(tmp_path):
    p = tmp_path / "MEMORY.md"
    p.write_text("", encoding="utf-8")
    assert sm.memory_merge_append(p, f"{UZUN_A}\n§\n{UZUN_B}\n".encode()) == (0, False)
    assert p.read_text(encoding="utf-8") == ""


def test_uzakta_kayit_yoksa_fail_closed(tmp_path):
    """Uzak boş/ayraçsız-boş ise karar verilemez → çakışma akışı (fail-closed)."""
    p = _yaz(tmp_path / "MEMORY.md", UZUN_A)
    assert sm.memory_merge_append(p, b"   \n") == (0, False)
    assert sm.memory_records(p.read_text(encoding="utf-8")) == [UZUN_A]


def test_idempotent_ikinci_cagri_eklemez(tmp_path):
    p = _yaz(tmp_path / "MEMORY.md", UZUN_A)
    paket = f"{UZUN_A}\n§\n{UZUN_B}\n".encode()
    assert sm.memory_merge_append(p, paket)[0] == 1
    boyut = p.stat().st_size
    assert sm.memory_merge_append(p, paket) == (0, True)   # artık uzak ⊆ yerel
    assert p.stat().st_size == boyut, "idempotent çağrı dosyayı büyüttü"


def test_paket_icindeki_tekrarlar_bir_kez_eklenir(tmp_path):
    p = _yaz(tmp_path / "MEMORY.md", UZUN_A)
    eklenen, _ = sm.memory_merge_append(
        p, f"{UZUN_B}\n§\n{UZUN_B}\n§\n{UZUN_B}\n".encode())
    assert eklenen == 1
    assert sm.memory_records(p.read_text(encoding="utf-8")).count(UZUN_B) == 1


def test_newline_ile_bitmeyen_dosyada_ayrac_dogru_kalir(tmp_path):
    p = tmp_path / "MEMORY.md"
    p.write_text(UZUN_A, encoding="utf-8")          # son satır sonu YOK
    assert sm.memory_merge_append(p, f"{UZUN_B}\n".encode())[0] == 1
    assert sm.memory_records(p.read_text(encoding="utf-8")) == [UZUN_A, UZUN_B]


def test_str_ve_bytes_kabul_edilir(tmp_path):
    p1 = _yaz(tmp_path / "a.md", UZUN_A)
    p2 = _yaz(tmp_path / "b.md", UZUN_A)
    assert sm.memory_merge_append(p1, f"{UZUN_B}\n")[0] == 1
    assert sm.memory_merge_append(p2, f"{UZUN_B}\n".encode())[0] == 1
    assert p1.read_text(encoding="utf-8") == p2.read_text(encoding="utf-8")


def test_yazma_hatasi_sessiz_ve_bozmaz(tmp_path, monkeypatch):
    """Yazma başarısızsa (0, False) döner → çakışma akışı; içerik bozulmaz."""
    p = _yaz(tmp_path / "MEMORY.md", UZUN_A)
    gercek_open = open

    def sahte_open(dosya, mod="r", *a, **kw):
        if "a" in mod and str(dosya) == str(p):
            raise OSError("disk dolu")
        return gercek_open(dosya, mod, *a, **kw)

    monkeypatch.setattr("builtins.open", sahte_open)
    assert sm.memory_merge_append(p, f"{UZUN_B}\n".encode()) == (0, False)
    monkeypatch.undo()
    assert sm.memory_records(p.read_text(encoding="utf-8")) == [UZUN_A]


def test_okunamayan_dosya_fail_closed(tmp_path):
    assert sm.memory_merge_append(tmp_path / "yok.md", f"{UZUN_A}\n".encode()) == (0, False)


# ─── kaynak kapıları (port sırasında sessiz gerileme olmasın) ───────────────

def test_birlesme_tam_rewrite_yapmaz():
    """memory_merge_append mevcut dosyayı YENİDEN YAZMAMALI (yalnız 'a' modu)."""
    src = inspect.getsource(sm.memory_merge_append)
    assert "os.replace" not in src, "append-only garantisi bozuldu (rewrite eklendi)"
    assert '"w"' not in src and "'w'" not in src, "yazma modu 'a' olmalı"
    assert '"a"' in src


def test_cagri_yeri_boyut_farki_dalinda():
    """Birleştirme YALNIZ çakışma dallarında ve yalnız MERGE dosyaları için."""
    govde = KAYNAK.split("def gdrive_pull_latest(cfg, node):", 1)[1].split("\ndef ", 1)[0]
    assert "MEMORY_MERGE_FILES" in govde, "çekme yolu birleştirme kapısını kullanmıyor"
    akis = govde.split("for member in tf:", 1)[1]        # arşiv açma döngüsü
    assert "_hafiza_elle(member, name, dest)" in akis
    assert akis.index("os.path.getsize(dest) != member.size") < akis.index(
        "_hafiza_elle(member, name, dest)"), "birleştirme boyut-farkı dalı dışında"
    # Birleşme olmazsa çakışma akışı hâlâ yerinde (fail-closed)
    assert "_write_conflict(dest, src_machine)" in akis
    # Aynı-boyut farklı-içerik dalında da denemeli (hafıza için yarım güvence olmaz)
    assert akis.count("_hafiza_elle(member, name, dest)") == 2


def test_birlesme_dosya_listesi_dar():
    assert sm.MEMORY_MERGE_FILES == {"MEMORY.md", "USER.md"}, (
        "birleştirme kapsamı genişledi — veri kaybı riski için yeniden denetim şart")


def test_memory_merge_append_ast_ile_tek_yazma():
    """Fonksiyonda tek yazma açması olmalı (oku + append); çoklu yazma yolu yok."""
    agac = ast.parse(KAYNAK)
    hedef = next((n for n in agac.body
                  if isinstance(n, ast.FunctionDef) and n.name == "memory_merge_append"), None)
    assert hedef is not None, "memory_merge_append modül düzeyinde tanımlı değil"
    acmalar = [n for n in ast.walk(hedef) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name) and n.func.id == "open"]
    assert len(acmalar) == 2, f"beklenen 2 open (oku + append), bulunan {len(acmalar)}"


def test_hafiza_modulleri_birlesmeden_etkilenmez():
    """A2A/hafıza export yolları bu birleştirmeyi kullanmamalı (tek sahip)."""
    assert "memory_merge_append" not in (
        REPO / "synclave" / "sync_memory.py").read_text(encoding="utf-8")
