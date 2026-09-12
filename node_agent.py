#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
node_agent.py — Çoklu Makinede Otonom Eşitleme + Yedekleme Ajanı (v1.0)
========================================================================
Hermes H1 (VPS/Linux) ↔ H2 (Windows/Desktop) ↔ gelecekteki makineler.

NE YAPAR (her koşuda, sırayla):
  1. DURUM TOPLA  — makine kimliği, sync motor durumu, çakışmalar, son koşular
  2. EŞİTLE       — sync_motor.py both (GDrive hub karşılıklı) + --skip-unchanged
  3. YEDEKLE      — sync_motor.py backup (GDrive versiyonlu, timestamp, silinmez)
  4. RAPORLA      — status.json'u GDrive hub'a yazar (merkezden tüm makineler okunur)
  5. PANEL        — ~/.hermes/state/sync_last_run.json (web paneli /api/status)

ÇALIŞTIRMA MODLARI:
  node_agent.py once         — tek koşu (cron / Task Scheduler çağırır)
  node_agent.py status --json— sadece durum raporu (koşu yapmaz)
  node_agent.py hub-check    — GDrive hub erişilebilirliği + merkez görünümü
  node_agent.py doctor       — makine kurulum sağlığı (python/rclone/git/sync_motor)

ZAMANLAYICI (otonomluk):
  Linux  : cron/systemd → node_agent.py once   (ör: her 90dk)
  Windows: Task Scheduler → node_agent.py once (ör: her 90dk)
  VEYA kendi döngüsü: node_agent.py daemon --interval 5400 (her 90dk, sonsuz)

GÜVENLİK:
  - .env/*.key/*.pem asla paketlenmez (sync_motor kuralı)
  - Aynı MAKİNEDE iki ajan eşzamanlı koşamaz (cumulus_node_agent.lock —
    fcntl/msvcrt, non-blocking; kilit doluysa hub raporu adımı ATLANIR).
    Kapsam sınırı: dosya kilidi yalnızca aynı dosya sistemindeki ajanları
    koordine eder. Farklı makineler makineye ÖZEL status.json yazar (paylaşımlı
    değiştirilebilir dosya yok); eşitleme sırası sync_motor'un kendi kilidiyle
    (MOTOR_LOCK) korunur. Ajan kilidi sync_motor'un kilidiyle AYNI dosya
    DEĞİLDİR (aynı olsaydı: ajan kilit tutarken motor alt-süreci kendi kilidini
    alamaz → sync sessizce atlanırdı).
  - Çakışma asla üzerine yazmaz — .conflict.<ts> olarak korunur
  - GDrive versiyonlu yedekler ASLA SİLİNMEZ

Geliştiren: CumulusNET Mühendislik — 2026
"""

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:                                    # Linux/macOS
    import fcntl
except ImportError:                     # Windows — fcntl yok
    fcntl = None
try:                                    # Windows
    import msvcrt
except ImportError:                     # Linux/macOS — msvcrt yok
    msvcrt = None

# ── Yapılandırma ──────────────────────────────────────────────
MOTOR_DIR = Path(__file__).resolve().parent          # sync_motor dizini
MOTOR = MOTOR_DIR / "sync_motor.py"
SMART = MOTOR_DIR / "smart_sync.py"
CONFIG = MOTOR_DIR / "config.json"
# Ajan kilidi (v2.6.0) — sync_motor'un MOTOR_LOCK'undan AYRI DOSYA.
# Aynı dosya olsaydı döngüsel kilitlenme: ajan kilit tutarken motor alt-süreci
# kendi kilidini alamaz → sync sessizce atlanır. Dizin kuralı motorla AYNIdır
# (POSIX: /tmp, Windows: %TEMP%) → Windows'ta '/tmp' varsayımı kalkar.
def _agent_lock_path() -> str:
    """Platform farkındalıklı ajan kilit yolu (sync_motor ile aynı dizin kuralı)."""
    if os.name == "nt":
        base = (os.environ.get("TEMP") or os.environ.get("TMP")
                or os.path.expanduser("~"))
        return os.path.join(base, "cumulus_node_agent.lock")
    return "/tmp/cumulus_node_agent.lock"


LOCK = _agent_lock_path()
RUN_STATE = Path(os.path.expanduser("~/.hermes/state/sync_last_run.json"))

# H1 merkez görünümü: gdrive:hermes-sync/<user>/<machine>/status.json
GDRIVE_HUB = "gdrive:hermes-sync"
DEFAULT_USER = "hahmet"

# ── Adım bütçesi (v2.5.1) ─────────────────────────────────────
# Neden: dış kapı (cron `run-node-agent.sh`) 3600s'te script'i ÖLDÜRÜR. İç
# zaman aşımı dış kapıya EŞİTSE (eski: backup timeout=3600) adım kendi hatasını
# hiçbir zaman yazamaz: 14 ardışık koşu boş log + "Script timed out after
# 3600s" ile düştü ve HİÇBİR teşhis kalmadı (ölçüm 11 Eyl 2026: sync 6.7 dk'da
# bitti, backup 53+ dk sürdü → dış kapı vurdu). Bu yüzden her iç bütçe dış
# kapının ALTINDA ve toplamları dış kapının altında tutulur.
# Regresyon kapısı: tests/test_node_agent_butce.py
DIS_KAPI_S = 3600
BUTCE_SYNC_S = 900          # ölçülen: 'both' ~7 dk (6.7 dk) → 15 dk bol pay
BUTCE_BACKUP_S = 1800       # restic/GDrive; aşılırsa RAPOR EDİLİR (sessiz ölüm yok)
BUTCE_MEMORY_S = 240
BUTCE_DIGER_S = 300         # status + tasks + state + hub raporu payı
BUTCE_VARSAYILAN_S = 1800   # motor() varsayılanı — çağrı açık timeout vermezse
BUTCE_KISA_S = 120          # kısa probe'lar (conflicts listesi gibi)

# Log dosyasına yönlendirildiğinde Python stdout'u BLOK tamponlar → dış kapı
# tarafından öldürülen koşu BOŞ log bırakır. Satır tamponu şart (teşhis).
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass


def machine_id() -> str:
    """Makine kimliği: hostname (H1=cumulusnet-hermes-1, H2=sistemg16)."""
    try:
        return socket.gethostname().lower().split(".")[0]
    except Exception:
        return platform.node().lower().split(".")[0]

def is_windows() -> bool:
    return os.name == "nt"

def now_iso() -> str:
    return datetime.now().isoformat()

def run(cmd, timeout=600, cwd=None):
    """Alt süreç çalıştır; (rc, stdout, stderr) döner."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=timeout, cwd=str(cwd or MOTOR_DIR))
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "", f"TIMEOUT {timeout}s"
    except Exception as e:
        return -1, "", str(e)

def motor(*args, timeout=BUTCE_VARSAYILAN_S):
    """sync_motor.py komutu sarmalayıcı — varsayılan da DIŞ KAPININ altında."""
    return run([sys.executable, str(MOTOR), *args], timeout=timeout)

def load_config():
    try:
        return json.load(open(CONFIG, encoding="utf-8", errors="replace"))
    except Exception:
        return {}

def read_run_state():
    try:
        if RUN_STATE.exists():
            return json.load(open(RUN_STATE, encoding="utf-8", errors="replace")).get("history", [])
    except Exception:
        pass
    return []

# ── Durum toplama ─────────────────────────────────────────────
def collect_status() -> dict:
    cfg = load_config()
    hist = read_run_state()
    last = hist[-1] if hist else None
    rc, out, err = motor("conflicts", timeout=BUTCE_KISA_S)
    conflicts = [l.strip() for l in out.splitlines() if ".conflict" in l] if rc == 0 else []
    return {
        "ts": now_iso(),
        "machine": machine_id(),
        "os": f"{platform.system()} {platform.release()}",
        "host": socket.gethostname(),
        "python": platform.python_version(),
        "is_windows": is_windows(),
        "sync_motor": "ok" if MOTOR.exists() else "EKSIK",
        "config_nodes": list(cfg.get("dirs", {}).keys()) if cfg else [],
        "conflicts": conflicts[:20],
        "conflict_count": len(conflicts),
        "son_kosu": last,
        "gdrive": hub_check() if (os.environ.get("NODE_AGENT_SKIP_GDRIVE") != "1") else "skip",
    }

def hub_check() -> str:
    """GDrive hub erişilebilirliği — status.json yazılabilir mi?"""
    rc, out, err = run(["rclone", "lsd", GDRIVE_HUB, "--max-depth", "1"], timeout=60)
    if rc == 0:
        return "ok"
    # hub henüz yok — yaratılabilir mi?
    rc2, _, err2 = run(["rclone", "mkdir", f"{GDRIVE_HUB}/{DEFAULT_USER}/{machine_id()}"], timeout=60)
    return "ok" if rc2 == 0 else f"YOK: {err.strip()[:80]} {err2.strip()[:80]}"

# ── Tek-instance kilidi (v2.6.0) ──────────────────────────────
# Non-blocking: kilit doluysa çağıran ATLAR (bekleme yok — Task Scheduler/cron
# sonraki tick'te tekrar dener; sınırsız bekleme ajanı ve zamanlayıcıyı kilitler).
_KILIT_BAYAT_S = 7200          # kilit API'si yokken: bu yaştan eski kayıt bayat
KILITSIZ = object()            # kilit altyapısı yok → çalışmayı ENGELLEME (sentinel)

def _pid_canli(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)                     # Windows: OpenProcess tabanlı
        return True
    except OSError:
        return False


def _lock_kaydi_yaz(fd) -> None:
    """Kilit kaydı (PID + ISO zaman) — teşhis; yazılamazsa kilit DÜŞMEZ."""
    try:
        fd.seek(0)
        fd.truncate()
        fd.write(f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}\n")
        fd.flush()
    except OSError:
        pass


def _lock_acquire():
    """Ajan kilidini non-blocking al.

    Dönüş: fd (kilit alındı) | None (kilit DOLU — çağıran adımı atlar) |
    KILITSIZ (kilit alt yapısı yok — kilit olmadan devam).

    fcntl.flock (Linux/macOS) → msvcrt.locking (Windows). Süreç ölürse OS
    kilidi otomatik düşer; bu yüzden bayat dosya kalıcı blok yaratmaz.
    Kilit API'si hiç yoksa: pid kaydı + canlılık + 2h yaş sınırı (fail-closed).
    """
    try:
        fd = open(LOCK, "a+", encoding="utf-8")
    except OSError as e:                    # kilit alt yapısı yok → ENGELLEME
        print(f"  ⚠ ajan kilidi açılamadı ({e}) — kilit olmadan devam")
        return KILITSIZ
    if fcntl is not None:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fd.close()
            return None
        _lock_kaydi_yaz(fd)
        return fd
    if msvcrt is not None:
        try:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fd.close()
            return None
        _lock_kaydi_yaz(fd)
        return fd
    # Kilit API'si yok → kayıt tabanlı guard (canlı pid + taze kayıt ⇒ RED).
    # Dürüst sınır: bu yol ATOMİK DEĞİL (okuma ile yazma arasında yarış olabilir);
    # yalnızca fcntl ve msvcrt'in İKİSİ de yoksa devreye girer — Linux/Windows'ta
    # ikisinden biri her zaman vardır. Yarış olsa bile hub'a yazılan dosya makineye
    # ÖZELdir (status_<machine>.json) → kayıp güncelleme değil, aynı içeriğin
    # tekrar yazımı olur.
    try:
        fd.seek(0)
        parcalar = fd.read().split()
        if len(parcalar) >= 2:
            pid, ts = int(parcalar[0]), datetime.fromisoformat(parcalar[1])
            yas = (datetime.now() - ts).total_seconds()
            # yas < 0 (ileri tarihli kayıt / saat kayması) da RED edilir: bayatlık
            # ÜST sınırı gevşetir, gelecek tarihli kayıt gevşetmez (fail-closed).
            if pid != os.getpid() and _pid_canli(pid) and yas < _KILIT_BAYAT_S:
                fd.close()
                return None
    except (ValueError, OSError):
        pass                                # bozuk/eksik kayıt → devral
    _lock_kaydi_yaz(fd)
    return fd


def _lock_release(fd) -> None:
    """Kilidi bırak (her yolda çağrılır — finally). KILITSIZ/None → no-op."""
    if fd is None or fd is KILITSIZ:
        return
    try:
        if fcntl is not None:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    finally:
        try:
            fd.close()
        except OSError:
            pass


# ── Rapor yazma ───────────────────────────────────────────────
def _gecici_sil(p: Path) -> None:
    """Geçici rapor dosyasını sil — istisna yolları dâhil her yolda."""
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def write_hub_status(status: dict) -> bool:
    """status.json'u GDrive'a yaz — merkezden tüm makineler görünür.

    Yazma YALNIZCA rclone copyto etrafında ajan kilidiyle korunur (v2.6.0):
    aynı makinede ikinci ajan koşuyorsa bu adım ATLANIR (bekleme yok).
    Motor kilidi tutulmaz — ajan sync_motor alt-sürecini bu adımdan sonra
    çağırır ve motor kendi kilidini alabilmelidir (döngüsel kilitlenme yok).
    Geçici dosya her yolda silinir (istisna dâhil) — denetim bulgusu #6.
    """
    tmp = Path(tempfile_dir()) / f"status_{machine_id()}.json"
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2))
    dst = f"{GDRIVE_HUB}/{DEFAULT_USER}/{machine_id()}/status.json"
    kilit = _lock_acquire()
    if kilit is None:                       # başka ajan yazıyor → bu tick atla
        _gecici_sil(tmp)
        print("  ⏭ hub raporu atlandı: başka ajan kilidi tutuyor")
        return False
    try:
        rc, _, err = run(["rclone", "copyto", str(tmp), dst,
                          "--ignore-checksum", "--no-traverse"], timeout=120)
    finally:
        _lock_release(kilit)
        _gecici_sil(tmp)
    if rc != 0:
        print(f"  ⚠ hub rapor yazılamadı: {err.strip()[:120]}")
        return False
    print(f"  ✅ durum → gdrive:{DEFAULT_USER}/{machine_id()}/status.json")
    return True

def tempfile_dir() -> str:
    import tempfile
    return tempfile.mkdtemp(prefix="nodeagent_")

# ── Eylemler ──────────────────────────────────────────────────
def motor_version() -> str:
    """sync_motor.py sürümü — eski sürümler --skip-unchanged/backup bilmez."""
    try:
        rc, out, err = run([sys.executable, str(MOTOR), "version"], timeout=60)
        if rc == 0 and "v" in out:
            return out.strip().split("v")[-1]
    except Exception:
        pass
    return "0.0"

def run_sync() -> tuple:
    """Eşitle: both (--skip-unchanged yalnız v1.6.2+; eski sürümler desteklemez)."""
    ver = motor_version()
    use_delta = _ver_tuple(ver) >= (1, 6, 2)
    args = ["both", "--skip-unchanged"] if use_delta else ["both"]
    print(f"  🔄 EŞİTLE: sync_motor {' '.join(args)}  (motor v{ver})")
    rc, out, err = motor(*args, timeout=BUTCE_SYNC_S)
    if rc == 0:
        print("  ✅ eşitleme tamam")
    else:
        print(f"  ⚠ eşitleme rc={rc}: {err.strip()[:200]}")
        print(f"    çıktı son: {out.strip()[-300:]}")
    return rc, out, err

def _ver_tuple(v: str) -> tuple:
    try:
        parts = v.split(".")
        return tuple(int(p) for p in parts[:3])
    except Exception:
        return (0, 0, 0)

def run_backup() -> tuple:
    """Yedekle: GDrive versiyonlu (yalnız v1.6.3+; eski sürümde no-op)."""
    if _ver_tuple(motor_version()) < (1, 6, 3):
        print("  💾 YEDEK: motor eski (no-op — GDrive snapshot both içinde)")
        return 0, "", "backup yalnız v1.6.3+ — no-op"
    print("  💾 YEDEK: sync_motor backup")
    rc, out, err = motor("backup", timeout=BUTCE_BACKUP_S)
    if rc == 0:
        print("  ✅ yedek tamam")
    else:
        print(f"  ⚠ yedek rc={rc}: {err.strip()[:200]}")
    return rc, out, err

def run_memory() -> tuple:
    """Ortak hafıza (D modülü v2.1+): export → hub push → pull/import → fact_store."""
    if _ver_tuple(motor_version()) < (2, 1, 0):
        print("  🧠 ORTAK HAFIZA: motor eski (no-op — memory yalnız v2.1+)")
        return 0, "", "memory yalnız v2.1+ — no-op"
    print("  🧠 ORTAK HAFIZA: sync_motor memory")
    rc, out, err = motor("memory", timeout=BUTCE_MEMORY_S)
    if rc == 0:
        print("  ✅ ortak hafıza tamam")
    else:
        print(f"  ⚠ ortak hafıza rc={rc}: {err.strip()[:200]}")
    return rc, out, err

def run_state() -> tuple:
    """Ortak akıl (E modülü v2.1+): state.json'a HLC saatli durum bloğu yaz.

    sync_common_knowledge modülünü doğrudan kullanır (rclone mock'suz gerçek
    GDrive hub). Hata → fail-closed (rc 1) ama diğer once adımlarını bozmaz.
    """
    try:
        sys.path.insert(0, str(MOTOR_DIR))
        import sync_common_knowledge as ck
        st = ck.update_state(machine_id(), {
            "last_sync": "ok",
            "last_run": now_iso(),
        })
        blocks = st.get("nodes", {})
        print(f"  🧠 ORTAK AKIL: state.json güncellendi ({len(blocks)} makine blok)")
        return 0, "", ""
    except Exception as e:
        print(f"  ⚠ ortak akıl rc=1: {str(e)[:200]}")
        return 1, "", str(e)

def run_tasks() -> tuple:
    """ORTAK GÖREV DAĞITIMI + FAILOVER (v2.2): pending görevleri al, claim et.

    Görevi İŞLEMEZ — sadece sahiplenir (işleme inbox_worker / Hermes yapar);
    sahibi düşmüş running görevler stale devralma ile bu makineye geçer.
    """
    try:
        sys.path.insert(0, str(MOTOR_DIR))
        import sync_common_knowledge as ck
        tasks = ck.list_tasks()
        pending = [t for t in tasks if t.get("status") == "pending"]
        claimed = 0
        for t in pending:
            r = ck.claim_task(t["task_id"], owner=machine_id(), allow_stale=True)
            if r.get("status") == "running" and r.get("owner") == machine_id():
                claimed += 1
                print(f"    🤝 {t['task_id']} → {machine_id()} (claim)")
        # düşen sahibin running görevlerini devral (failover)
        for t in tasks:
            if t.get("status") == "running" and t.get("owner") != machine_id():
                r = ck.claim_task(t["task_id"], owner=machine_id(), allow_stale=True)
                if r.get("status") == "running" and r.get("owner") == machine_id():
                    print(f"    🔄 {t['task_id']} failover → {machine_id()} (attempt={r.get('attempt')})")
        print(f"  📋 GÖREVLER: {len(pending)} pending, {claimed} claim, failover devralma yapıldı")
        return 0, "", ""
    except Exception as e:
        print(f"  ⚠ görev rc=1: {str(e)[:200]}")
        return 1, "", str(e)


def _adim(ad, fn):
    """Adımı SÜRE ölçerek çalıştır — log'da 'başladı / bitti rc süre' görünür.

    Neden (v2.5.1): dış kapı 3600s'te koşuyu öldürdüğünde HANGİ adımın takıldığı
    bilinmiyordu (boş log + 14 ardışık "Script timed out"). Satır tamponlu
    stdout + adım süresi birlikte teşhisi mümkün kılar: log'un son satırı
    ölüm anındaki adımı gösterir.
    """
    t0 = time.time()
    print(f"  ▶ {ad} başladı", flush=True)
    try:
        rc, out, err = fn()
    except Exception as e:                      # adım hatası koşuyu düşürmez
        rc, out, err = 1, "", str(e)
    sure = time.time() - t0
    print(f"  ◀ {ad} bitti rc={rc} {sure:.1f}s", flush=True)
    return rc, out, err, sure


def run_once(do_sync=True, do_backup=True, do_memory=True, report=True):
    """Tek otonom koşu — cron/Task Scheduler bu fonksiyonu çağırır.

    Adım bütçeleri (BUTCE_*) dış kapının (DIS_KAPI_S=3600s) ALTINDADIR: bir adım
    bütçesini aşarsa rc=-1 ile RAPOR EDİLİR ve koşu rapor/state adımlarına
    devam eder (sessiz ölüm yok).
    """
    status = collect_status()
    print(f"╔{'═'*52}╗")
    print(f"║ NODE AGENT — {machine_id().upper()} ({platform.system()})  {now_iso()[:19]} ║")
    print(f"╚{'═'*52}╝")

    if do_sync:
        rc, out, err, sure = _adim("sync", run_sync)
        status["sync"] = {"rc": rc, "ts": now_iso(), "sure_s": round(sure, 1),
                          "out_tail": out.strip()[-200:], "err_tail": err.strip()[-200:]}
    if do_backup:
        rc, out, err, sure = _adim("backup", run_backup)
        status["backup"] = {"rc": rc, "ts": now_iso(), "sure_s": round(sure, 1),
                            "out_tail": out.strip()[-200:], "err_tail": err.strip()[-200:]}
    if do_memory:
        rc, out, err, sure = _adim("memory", run_memory)
        status["memory"] = {"rc": rc, "ts": now_iso(), "sure_s": round(sure, 1),
                            "out_tail": out.strip()[-200:], "err_tail": err.strip()[-200:]}
    # ortak akıl (E): state.json HLC bloğu — sync'ten bağımsız, her koşuda
    # v2.2: ortak görev dağıtımı + failover (run_state'ten sonra)
    run_tasks()
    if do_sync or do_backup or do_memory:
        rc, out, err = run_state()
        status["state"] = {"rc": rc, "ts": now_iso(),
                           "out_tail": out.strip()[-200:], "err_tail": err.strip()[-200:]}

    # son-koşu kaydı (web paneli okur)
    try:
        hist = read_run_state()
        rec = {"ts": now_iso(), "komut": "node_agent", "rc": 0,
               "node": machine_id(), "machine": machine_id(),
               "extra": {"sync": status.get("sync", {}).get("rc"),
                         "backup": status.get("backup", {}).get("rc")}}
        hist.append(rec)
        RUN_STATE.parent.mkdir(parents=True, exist_ok=True)
        json.dump({"history": hist[-50:]}, open(RUN_STATE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        status["son_kosu"] = rec     # rapora güncel kaydı yaz
    except Exception as e:
        print(f"  ⚠ son-koşu kaydı: {e}")

    if report:
        write_hub_status(status)
    return 0

# ── Daemon ────────────────────────────────────────────────────
def run_daemon(interval: int):
    """Sonsuz döngü — her interval saniyede bir koşu (otonomluk için alternatif)."""
    print(f"  ⏳ daemon: her {interval}s'de bir koşu (Ctrl+C ile durur)")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"  ⚠ koşu hatası: {e}")
        time.sleep(interval)

# ── Doctor ────────────────────────────────────────────────────
def doctor():
    print("═══ NODE AGENT DOCTOR ═══")
    print(f"  makine      : {machine_id()} ({platform.system()})")
    print(f"  python      : {platform.python_version()}")
    checks = [
        ("python3", sys.executable),
        ("rclone", "rclone"),
        ("git", "git"),
        ("sync_motor.py", str(MOTOR)),
        ("smart_sync.py", str(SMART)),
        ("config.json", str(CONFIG)),
    ]
    ok = True
    for name, path in checks:
        if name.endswith(".py") or name.endswith(".json"):
            exists = Path(path).exists()
            print(f"  {'✅' if exists else '❌'} {name}: {path} {'VAR' if exists else 'YOK'}")
            ok = ok and exists
        else:
            rc, _, err = run([path, "--version"] if name != "python3" else [path, "--version"],
                             timeout=30)
            print(f"  {'✅' if rc == 0 else '❌'} {name}: {path} "
                  f"{'çalışıyor' if rc == 0 else (err.strip()[:60] or f'rc={rc}')}")
            ok = ok and rc == 0
    print(f"  {'✅ DOKTOR: hazır' if ok else '❌ DOKTOR: eksik var'}")
    return 0 if ok else 1

# ── Ana ───────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(prog="node_agent", description="Otonom eşitleme+yedekleme ajanı")
    ap.add_argument("komut", nargs="?", default="status",
                    choices=["once", "status", "daemon", "hub-check", "doctor", "mesh"])
    ap.add_argument("--interval", type=int, default=5400, help="daemon: saniye (varsayılan 5400=90dk)")
    ap.add_argument("--no-sync", action="store_true", help="once: eşitleme atla")
    ap.add_argument("--no-backup", action="store_true", help="once: yedek atla")
    ap.add_argument("--no-memory", action="store_true", help="once: ortak hafıza atla (v2.1)")
    ap.add_argument("--no-report", action="store_true", help="once: hub raporu atla")
    ap.add_argument("--json", action="store_true", help="status: JSON çıktı")
    args = ap.parse_args(argv)

    if args.komut == "once":
        return run_once(do_sync=not args.no_sync,
                        do_backup=not args.no_backup,
                        do_memory=not args.no_memory,
                        report=not args.no_report)
    if args.komut == "status":
        st = collect_status()
        if args.json:
            print(json.dumps(st, ensure_ascii=False, indent=2))
        else:
            print(f"makine    : {st['machine']} ({st['os']})")
            print(f"sync_motor: {st['sync_motor']}")
            print(f"nodes     : {', '.join(st['config_nodes']) or '(config yok)'}")
            print(f"conflicts : {st['conflict_count']}")
            print(f"gdrive    : {st['gdrive']}")
            print(f"son_kosu  : {st['son_kosu']}")
        return 0
    if args.komut == "daemon":
        return run_daemon(args.interval)
    if args.komut == "mesh":
        # P2P mesh durumu (v1.7.0)
        try:
            from sync_p2p import p2p_status
            print(p2p_status(load_config()))
        except Exception as e:
            print(f"mesh hata: {e}")
            print("ipucu: sync_p2p.py kurulu mu? (cumulus-sync-motor içinde)")
        return 0
    if args.komut == "hub-check":
        print(json.dumps({"machine": machine_id(), "hub": hub_check()}, ensure_ascii=False, indent=2))
        return 0
    if args.komut == "doctor":
        return doctor()
    return 0


if __name__ == "__main__":
    sys.exit(main())
