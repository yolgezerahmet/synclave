#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_versions_cmd.py — A modülü (versiyon etiketleme + diff + rollback dry-run)
testleri (2026-08-29)

Kapsam (PREWALK TODO):
  1. _tag_version: etiket yazma (rclone mock), geçersiz etiket RED, aynı tag RED
  2. _diff_versions: iki tar arası dosya farkı (mock rclone cat | tar)
  3. cmd_rollback --dry-run: değişecek dosya + çakışma sayısı, HİÇBİR ŞEY yazmaz
  4. cmd_versions --tag / --diff dispatch

KISIT: gerçek rclone/GDrive ÇALIŞTIRILMAZ — subprocess.run mock'lanır.
Kullanım: python3 test_versions_cmd.py
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

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

import sync_motor as motor

TMP = tempfile.mkdtemp(prefix="vercmd_test_")
PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ✓ {name}")


def fail(name, detail=""):
    print(f"  ✗ FAIL: {name} {detail}")
    sys.exit(1)


def make_cfg():
    return {
        "identity": {"user_id": "cumulusnet", "machine_id": "test-node-1"},
        "gdrive": {"user_root": "gdrive:hermes-sync/cumulusnet"},
        "dirs": {"kernel": {"path": os.path.join(TMP, "kernel")}},
        "machine": "H1",
    }


def make_version_tar(node, fname, files):
    """GDrive mock: yerel tmp'de versiyon tar.gz üretir."""
    src = os.path.join(TMP, "vsrc", fname)
    os.makedirs(src, exist_ok=True)
    for f, content in files.items():
        p = os.path.join(src, f)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as wf:
            wf.write(content)
    tarp = os.path.join(TMP, "hub", node, fname)
    os.makedirs(os.path.dirname(tarp), exist_ok=True)
    with tarfile.open(tarp, "w:gz") as tar:
        tar.add(src, arcname="")
    return tarp


# ─── Mock rclone ──────────────────────────────────────────────
calls = []
GDRIVE_HUB = "gdrive:hermes-sync/cumulusnet/versiyonlu"
kernel_dir = os.path.join(TMP, "kernel")
os.makedirs(kernel_dir, exist_ok=True)
with open(os.path.join(kernel_dir, "main.c"), "w") as f:
    f.write("int main() { return 0; }")


class FakeProc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def fake_run(cmd, **kw):
    calls.append(cmd)
    # rclone lsjson --hash: hub'da dosyalar + hash
    if cmd[0] == "rclone" and cmd[1] == "lsjson":
        node = cmd[2].rsplit("/", 1)[-1]
        node_dir = os.path.join(TMP, "hub", node)
        entries = []
        if os.path.isdir(node_dir):
            for fn in sorted(os.listdir(node_dir)):
                if fn.endswith(".tar.gz"):
                    p = os.path.join(node_dir, fn)
                    entries.append({"Path": fn, "Size": os.path.getsize(p),
                                    "Hash": hashlib_hex(p)})
        return FakeProc(0, json.dumps(entries))
    # rclone lsf tags dir
    if cmd[0] == "rclone" and cmd[1] == "lsf" and "tags" in cmd[2]:
        tags_dir = os.path.join(TMP, "hub", cmd[2].split("/tags")[0].rsplit("/", 1)[-1], "tags")
        if os.path.isdir(tags_dir):
            return FakeProc(0, "\n".join(os.listdir(tags_dir)) + "\n")
        return FakeProc(0, "")
    if cmd[0] == "rclone" and cmd[1] == "lsf":
        node = cmd[2].rsplit("/", 1)[-1]
        node_dir = os.path.join(TMP, "hub", node)
        if os.path.isdir(node_dir):
            return FakeProc(0, "\n".join(os.listdir(node_dir)) + "\n")
        return FakeProc(0, "")
    # rclone copyto: yerel → hub (tags) veya hub → yerel (rollback indirme)
    if cmd[0] == "rclone" and cmd[1] == "copyto":
        src, dst = cmd[2], cmd[3]
        if dst.startswith(GDRIVE_HUB) or "gdrive:" in dst:
            # push: src yerel, dst gdrive:...  → yerel hub mock'a kopyala
            parts = dst.replace(GDRIVE_HUB + "/", "").split("/")
            rel = os.path.join(TMP, "hub", *parts)
            os.makedirs(os.path.dirname(rel), exist_ok=True)
            shutil.copy(src, rel)
        else:
            # pull: src gdrive, dst yerel → hub mock'tan kopyala
            parts = src.replace(GDRIVE_HUB + "/", "").split("/")
            rel = os.path.join(TMP, "hub", *parts)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy(rel, dst)
        return FakeProc(0)
    if cmd[0] == "rclone" and cmd[1] == "cat":
        # rclone cat <hub>/<node>/<file> | tar tzf - (bash -c ile gelir)
        # shlex.quote sonrası tırnaksız olabilir — sağlam parse
        inner = cmd[2]
        remote = inner.split("'")[1] if "'" in inner else \
            inner.split("|")[0].strip().split("rclone cat ", 1)[1]
        parts = remote.replace(GDRIVE_HUB + "/", "").split("/")
        rel = os.path.join(TMP, "hub", *parts)
        if os.path.exists(rel):
            with tarfile.open(rel, "r:gz") as tar:
                names = [m.name for m in tar.getmembers() if m.isfile()]
            return FakeProc(0, "\n".join(names) + "\n")
        return FakeProc(0, "")
    if cmd[0] == "bash" and cmd[1] == "-c":
        # rclone cat '<hub>/<node>/<file>' | tar tzf - (shlex.quote olabilir)
        inner = cmd[2]
        remote = inner.split("'")[1] if "'" in inner else \
            inner.split("|")[0].strip().split("rclone cat ", 1)[1]
        parts = remote.replace(GDRIVE_HUB + "/", "").split("/")
        rel = os.path.join(TMP, "hub", *parts)
        if os.path.exists(rel):
            with tarfile.open(rel, "r:gz") as tar:
                names = [m.name for m in tar.getmembers() if m.isfile()]
            return FakeProc(0, "\n".join(names) + "\n")
        return FakeProc(0, "")
    return FakeProc(1, "", f"unknown mock cmd: {cmd[:3]}")


def hashlib_hex(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


orig_run = subprocess.run
subprocess.run = fake_run
try:
    # ─── 1. TAG ────────────────────────────────────────────────
    print("── TAG ──")
    # versiyon tar'ları üret
    v1 = make_version_tar("kernel", "kernel_20260801_100000.tar.gz",
                          {"main.c": "v1", "new.c": "new"})
    v2 = make_version_tar("kernel", "kernel_20260802_100000.tar.gz",
                          {"main.c": "v2", "deleted.c": "del"})
    # gerçek hub yerine mock dizini: GDRIVE_HUB → TMP/hub eşlemesi
    # _tag_version subprocess mock'u ile çalışır
    vers = ["kernel_20260801_100000.tar.gz", "kernel_20260802_100000.tar.gz"]
    rc = motor._tag_version(make_cfg(), "kernel", "kernel-v2.3-dgk", vers,
                            hub=GDRIVE_HUB)
    if rc != 0:
        fail("_tag_version rc", str(rc))
    ok("_tag_version: etiket yazıldı")
    tag_path = os.path.join(TMP, "hub", "kernel", "tags", "kernel-v2.3-dgk.txt")
    if not os.path.exists(tag_path):
        fail("tag dosyası oluşmadı", tag_path)
    meta = json.load(open(tag_path))
    if meta["version"] != "kernel_20260802_100000.tar.gz":
        fail("tag en son versiyonu göstermiyor", str(meta))
    if not meta.get("remote_hash"):
        fail("tag remote_hash eksik", str(meta.get("remote_hash")))
    ok(f"tag: en son versiyon + remote_hash {str(meta.get('remote_hash'))[:8]}…")

    # aynı tag tekrar → RED
    rc2 = motor._tag_version(make_cfg(), "kernel", "kernel-v2.3-dgk", vers,
                             hub=GDRIVE_HUB)
    if rc2 == 0:
        fail("aynı tag RED etmedi")
    ok("_tag_version: aynı tag → RED (üzerine yazmaz)")

    # geçersiz tag → RED
    rc3 = motor._tag_version(make_cfg(), "kernel", "BAD TAG!", vers,
                             hub=GDRIVE_HUB)
    if rc3 == 0:
        fail("geçersiz tag RED etmedi")
    ok("_tag_version: geçersiz etiket → RED")

    # ─── 2. DIFF ───────────────────────────────────────────────
    print("── DIFF ──")
    calls.clear()
    rc = motor._diff_versions(make_cfg(), "kernel",
                              "kernel_20260801_100000.tar.gz,kernel_20260802_100000.tar.gz",
                              hub=GDRIVE_HUB)
    if rc != 0:
        fail("_diff_versions rc", str(rc))
    # output'u yakalamak için stdout redirect — basitçe rc + çağrı kontrolü
    cat_calls = [c for c in calls if c[0] == "bash"]
    if len(cat_calls) != 2:
        fail("diff iki tar okumadı", str(len(cat_calls)))
    ok("_diff_versions: iki tar üye listesi okundu")

    # ─── 3. ROLLBACK --dry-run ─────────────────────────────────
    print("── ROLLBACK DRY-RUN ──")
    # hedef klasörde farklı içerik olsun (çakışma senaryosu)
    with open(os.path.join(kernel_dir, "main.c"), "w") as f:
        f.write("int main() { return 42; }")  # v1'den farklı
    # v1 geri al → main.c değişecek, new.c yeni eklenecek
    rc = motor.cmd_rollback(make_cfg(), "kernel",
                            "kernel_20260801_100000.tar.gz",
                            hub=GDRIVE_HUB, dry_run=True)
    if rc != 0:
        fail("rollback dry-run rc", str(rc))
    # hiçbir şey değişmedi mi? main.c hâlâ 42 olmalı
    content = open(os.path.join(kernel_dir, "main.c")).read()
    if "42" not in content:
        fail("dry-run main.c'yi değiştirdi", content)
    ok("rollback --dry-run: ön-inceleme, HİÇBİR ŞEY yazmadı")

    # ─── 4. VERSIONS dispatch (--tag / --diff) ─────────────────
    print("── VERSIONS DISPATCH ──")
    # cmd_versions --tag direkt çağrı
    rc = motor.cmd_versions(make_cfg(), node="kernel", hub=GDRIVE_HUB,
                            tag="kernel-release-1")
    if rc != 0:
        fail("cmd_versions --tag rc", str(rc))
    ok("cmd_versions --tag: dispatch çalıştı")

    # ─── 5. GERÇEK rollback (dry-run DEĞİL) — force ile çakışma yok ──
    print("── ROLLBACK (force) ──")
    rc = motor.cmd_rollback(make_cfg(), "kernel",
                            "kernel_20260802_100000.tar.gz",
                            hub=GDRIVE_HUB, force=True)
    if rc != 0:
        fail("rollback force rc", str(rc))
    content = open(os.path.join(kernel_dir, "main.c")).read()
    if "v2" not in content:
        fail("rollback main.c'yi v2 yapmadı", content)
    ok("rollback --force: v2 içerik geri alındı")

finally:
    subprocess.run = orig_run

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n✅ TÜM TESTLER GEÇTİ — {PASS} PASS")
sys.exit(0)
