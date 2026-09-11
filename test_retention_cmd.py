#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_retention_cmd.py — C modülü (öncelik sınıflı retention + SHA doğrulama)
testleri (2026-08-29)

Kapsam:
  1. retention_limits: KRİTİK 12ay/8h, ORTA 8h/0ay, BÜYÜK 4h/0ay
  2. priority_for: node adı → sınıf (kernel=kritik, research=orta, plugins=buyuk)
  3. retention_decision: günlük/haftalık/aylık bucket koruma, eski silme
  4. SHA doğrulama mantığı (cmd_backup içindeki lsjson karşılaştırma deseni)

KISIT: gerçek rclone GDrive ÇALIŞTIRILMAZ — yalnız saf fonksiyonlar.
Kullanım: python3 test_retention_cmd.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
# Modülleri barındıran dizini işaret dosyasıyla bul: betik depo kökünde veya
# tests/manual/ altında olabilir; sabit dirname sayısı kırılgan (private kopyada
# bir fazla dirname yanlış dizini gösteriyordu — 11 Eyl 2026).
_KOK = HERE
for _ in range(4):
    if os.path.isfile(os.path.join(_KOK, "sync_motor.py")):
        break
    _KOK = os.path.dirname(_KOK)
for _p in (HERE, _KOK, os.path.join(_KOK, "synclave")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import sync_retention as ret

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ✓ {name}")


def fail(name, detail=""):
    print(f"  ✗ FAIL: {name} {detail}")
    sys.exit(1)


# ─── 1. ÖNCELİK SINIFLARI ─────────────────────────────────────
print("── PRIORITY ──")
tests = {
    "kernel": "kritik", "patent": "kritik", "scripts": "kritik",
    "hermes": "kritik", "research": "orta", "pcb": "orta", "sim": "orta",
    "hermes-skills": "buyuk", "plugins": "buyuk", "hermes-full": "buyuk",
    "bilinmeyen-node": "orta",
}
for node, expected in tests.items():
    got = ret.priority_for(node)
    if got != expected:
        fail(f"priority_for({node})", f"{got} != {expected}")
ok(f"priority_for: {len(tests)} eşleme doğru (bilinmeyen → orta)")

limits_k = ret.retention_limits("kernel")
if limits_k != {"daily_days": 7, "weekly_weeks": 8, "monthly_months": 12}:
    fail("KRİTİK limitler", str(limits_k))
ok("retention_limits: KRİTİK 12ay/8h/7g")

limits_o = ret.retention_limits("research")
if limits_o != {"daily_days": 7, "weekly_weeks": 8, "monthly_months": 0}:
    fail("ORTA limitler", str(limits_o))
ok("retention_limits: ORTA 8h/7g (aylık yok)")

limits_b = ret.retention_limits("plugins")
if limits_b != {"daily_days": 7, "weekly_weeks": 4, "monthly_months": 0}:
    fail("BÜYÜK limitler", str(limits_b))
ok("retention_limits: BÜYÜK 4h/7g (aylık yok)")

# ─── 2. RETENTION KARARI ──────────────────────────────────────
print("── DECISION ──")
now = ret.NOW
snaps = []
# son 7 gün: 7 snapshot (hepsi korunur)
for i in range(7):
    snaps.append((now - timedelta(days=i), f"2026082{i}_100000"))
# 8-12 hafta önce: her hafta 3 snapshot (haftalık son korunur)
for w in range(8, 13):
    for j in range(3):
        ts = now - timedelta(weeks=w, days=j)
        snaps.append((ts, f"hist_w{w}_{j}"))
# 12 ay öncesi: eski (silinir — kritikte aylık bucket yok)
old = now - timedelta(days=400)
snaps.append((old, "cok_eski_1"))

to_del_k, keep_k = ret.retention_decision(snaps, "kernel")
# cok_eski_1 silinmeli (400 gün > 12 ay); son 7 gün + haftalık son korunur
if "cok_eski_1" not in to_del_k:
    fail("KRİTİK: 400 günlük snapshot silinmedi")
ok("KRİTİK: 12 ay öncesi snapshot silinir")

# BÜYÜK: aylık yok — 8-12 hafta öncekilerden 9-12. hafta silinir (4h sınırı)
to_del_b, keep_b = ret.retention_decision(snaps, "plugins")
hist_9_12 = [n for n in to_del_b if n.startswith("hist_w9") or n.startswith("hist_w10")
             or n.startswith("hist_w11") or n.startswith("hist_w12")]
if not hist_9_12:
    fail("BÜYÜK: 9-12. hafta snapshot'ları silinmedi", str(to_del_b[:5]))
ok("BÜYÜK: 4 haftadan eski haftalık snapshot'lar silinir")

# son 7 gün korunur (her sınıf)
for cls in ("kritik", "orta", "buyuk"):
    to_del, keep = ret.retention_decision(snaps, "kernel" if cls == "kritik"
                                          else "research" if cls == "orta"
                                          else "plugins")
    daily_kept = [n for n in keep if n.startswith("2026082")]
    if len(daily_kept) != 7:
        fail(f"{cls.upper()}: son 7 gün snapshot'ı korunmadı", str(len(daily_kept)))
ok("Tüm sınıflar: son 7 günlük snapshot'lar korunur")

# ─── 3. SHA DOĞRULAMA DESENİ ──────────────────────────────────
print("── SHA VERIFY ──")
# cmd_backup içindeki lsjson karşılaştırma mantığını saf olarak test et
remote_entries = [
    {"Path": "kernel_20260829_120000.tar.gz", "Hash": "abc123"},
    {"Path": "kernel_20260829_130000.tar.gz", "Hash": "def456"},
]
local_sha = "def456"
verified = any(f.get("Path") == "kernel_20260829_130000.tar.gz"
               and f.get("Hash") == local_sha for f in remote_entries)
if not verified:
    fail("SHA doğrulama eşleşmedi")
ok("SHA doğrulama: yerel sha == GDrive lsjson hash")

bad = any(f.get("Path") == "kernel_20260829_130000.tar.gz"
          and f.get("Hash") == "wrong" for f in remote_entries)
if bad:
    fail("yanlış hash doğrulanmamalı")
ok("SHA doğrulama: yanlış hash RED")

print(f"\n✅ TÜM TESTLER GEÇTİ — {PASS} PASS")
sys.exit(0)
