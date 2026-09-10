#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cumulus Sync Motoru — Profesyonel Sürüm
========================================
GitHub + Google Drive + Tailscale üzerinden iki yönlü, veri kaybına
karşı güvenli (non-destructive) dosya senkronizasyon motoru.

Özellikler
----------
* İKİ YÖNLÜ: push + pull (H1 VPS ↔ H2 Desktop aralıklı çalışabilir)
* NON-DESTRUCTIVE: asla üzerine yazmaz; çakışmalar .conflict.TS ile korunur
* VERSİYONLU: her senkron GDrive'da timestamp'li snapshot oluşturur
* AKILLI FİLTRE: küçük/değerli dosyalar (kernel .c/.h, scripts) GitHub
  manifest'ine; büyük dosyalar (PCB, patent) GDrive'a yönlendirilir
* FARKINDALIK: her iki taraf diğerinin değişikliklerini görür
* MAKİNE TESPİTİ: H1/H2 otomatik algılanır (hostname + OS)
* LOGLAMA: seviyeli log + dosya kaydı + opsiyonel renkli çıktı
* TEST: birim testler (tests/test_sync_motor.py)

Kurulum
-------
    pip install -r requirements.txt   # requests (opsiyonel)

Yapılandırma
------------
    cp config.example.json config.json
    # config.json'u kendi makinenize göre düzenleyin

Kullanım
--------
    python3 sync_motor.py status      # durum + farkındalık
    python3 sync_motor.py push        # yerel → merkez (GitHub + GDrive)
    python3 sync_motor.py pull        # merkez → yerel (çakışmasız)
    python3 sync_motor.py both        # push + pull (önerilen)
    python3 sync_motor.py conflicts   # çakışma dosyalarını listele
    python3 sync_motor.py init        # ilk kurulum

Lisans: MIT (bkz. LICENSE)
Geliştiren: CumulusNET Mühendislik — 2026
"""

import argparse
import filecmp
import glob
try:
    import fcntl
except ImportError:      # Windows (H2): fcntl yok → msvcrt ile lock
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ── v2.1 (29 Ağu 2026): sync_memory modülü (D — ortak hafıza) ──
# sync_memory.py aynı dizinde; farklı cwd'den çalışınca da bulunabilsin.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import sync_memory as smem

__version__ = "2.3.1"
__author__ = "CumulusNET Engineering"
__license__ = "MIT"


# ═══════════════════════════════════════════════════════════════
# LOGGING KURULUMU
# ═══════════════════════════════════════════════════════════════

LOG_COLORS = {
    "DEBUG": "\033[36m",    # cyan
    "INFO": "\033[32m",     # green
    "WARNING": "\033[33m",  # yellow
    "ERROR": "\033[31m",    # red
    "CRITICAL": "\033[1;31m",
    "RESET": "\033[0m",
}


class ColoredFormatter(logging.Formatter):
    def format(self, record):
        prefix = ""
        if sys.stdout.isatty():
            color = LOG_COLORS.get(record.levelname, "")
            prefix = f"{color}"
        msg = super().format(record)
        reset = LOG_COLORS["RESET"] if sys.stdout.isatty() else ""
        return f"{prefix}{msg}{reset}"


def setup_logging(level=logging.INFO, logfile=None):
    root = logging.getLogger("sync_motor")
    root.setLevel(level)
    root.handlers.clear()

    # GPT-5.6 P1 (15 Ağu): token/secret log redaction — çıktıya sızmasın
    import re as _re
    _SENSITIVE_PATTERNS = [_re.compile(r"ghp_[A-Za-z0-9_]{30,}"),
                           _re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
                           _re.compile(r"(?i)(token|secret|password)=\S+")]

    class RedactFilter(logging.Filter):
        def filter(self, record):
            try:
                msg = record.getMessage()
                for pat in _SENSITIVE_PATTERNS:
                    msg = pat.sub("[REDACTED]", msg)
                record.msg = msg
                record.args = ()
            except Exception:
                pass
            return True

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ColoredFormatter("%(levelname)-8s %(message)s"))
    console.addFilter(RedactFilter())
    root.addHandler(console)

    if logfile:
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s"))
        fh.addFilter(RedactFilter())
        root.addHandler(fh)

    return root


log = logging.getLogger("sync_motor")


# ═══════════════════════════════════════════════════════════════
# YAPILANDIRMA
# ═══════════════════════════════════════════════════════════════

DEFAULT_CONFIG = {
    "identity": {
        "user_id": "cumulusnet",      # GDrive alanı: gdrive:hermes-sync/<user_id>/
        "machine_id": "",             # boş = otomatik (hostname)
    },
    "github": {
        "repo": "yolgezerahmet/cumulus-sync",
        "branch": "main",
        "manifest_file": "sync_manifest.json",
    },
    "gdrive": {
        "root": "gdrive:hermes-sync",      # EVRENSEL: her kullanıcı kendi alanında
        "versioned_dir": "gdrive:hermes-sync/cumulusnet/versiyonlu",
    },
    "network": {
        "h1_http": "http://100.92.2.47:9090",
        "h2_http": "http://100.76.82.46:9090",
        "timeout_s": 10,
    },
    "machines": {
        "h1_hostnames": ["CumulusNET-Hermes-1", "hal-server-801964"],
        "h2_hostnames": ["H2-Windows-RTX5070Ti"],
        "h3_hostnames": ["hermesagent03", "H3-LOCAL-HERMES"],
        "openclaw_hostnames": ["openclaw"],
    },
    "dirs": {
        "kernel": {
            "paths": [
                "/root/.config/superpowers/worktrees/cumulusos/canonical-full-product-gates",
                "/root/cumulusos"
            ],
            "include": ["*.c", "*.h", "*.md", "Makefile"],
            "exclude_dirs": [".git", "build", "__pycache__", ".backup", ".sync_backup"],
            "max_size_kb": 512,
            "gdrive": True,
        },
        "patent": {
            "path": "/root/patent_docs",
            "include": ["*.md", "*.txt"],
            "exclude_dirs": [".git"],
            "max_size_kb": 512,
            "gdrive": True,
        },
        "scripts": {
            "path": "/root/.hermes/scripts",
            "include": ["*.py", "*.sh", "*.ps1", "*.md"],
            "exclude_dirs": ["__pycache__"],
            "max_size_kb": 512,
            "gdrive": True,
        },
        "pcb": {
            "path": "/root/pcb/projects",
            "include": ["*.kicad_sch", "*.kicad_pcb", "*.md", "*BOM*"],
            "exclude_dirs": [".git", "production", "gerber"],
            "max_size_kb": 2048,
            "gdrive": True,
        },
        "hermes": {
            "path": "~/.hermes",
            "include": ["*.yaml", "*.yml", "*.json", "*.md", "*.py", "*.sh"],
            "exclude_dirs": ["cache", "logs", "audio_cache", "state",
                             "profiles", "models", "venv", "__pycache__",
                             "kanban", "plugins", "agents", "backups",
                             "sessions", "skills", "scripts"],
            "max_size_kb": 1024,
            "gdrive": True,
        },
        "hermes-skills": {
            "path": "~/.hermes/skills",
            "include": ["*.md", "*.py", "*.sh"],
            "exclude_dirs": ["__pycache__"],
            "max_size_kb": 2048,
            "gdrive": True,
        },
        "openclaw": {
            "path": "~/.openclaw",
            "include": ["*.md", "*.json", "*.yaml", "*.yml", "*.py"],
            "exclude_dirs": ["cache", "logs", "sessions", "workspace",
                             "skills", "scripts", "__pycache__", "models"],
            "max_size_kb": 1024,
            "gdrive": True,
        },
        "hermes-full": {
            "path": "~/.hermes",
            "include": ["*.yaml", "*.yml", "*.json", "*.md", "*.py", "*.sh"],
            "exclude_dirs": ["cache", "logs", "audio_cache", "models",
                             "venv", "__pycache__", "state", "kanban"],
            "max_size_kb": 2048,
            "gdrive": True,
        },
    },
    # AKILLI KURULUM KATALOĞU (v1.6):
    # - check: araç varlık kontrolü (HERHANGİ BİRİ kuruluysa "kurulu" sayılır)
    # - gpu: True ise GPU öncelikli — öneri listesinde öne alınır ve GPU'suz
    #   makinelere kurulum ÖNERİLMEZ (kaynak kontrolü)
    # - min_cpus/min_ram_gb/min_disk_gb: kurulum için asgari kaynak eşikleri
    # - install: onay sonrası çalıştırılacak kurulum komutu (kendi config'inizden
    #   gelir; kabuk operatörleri için string form kullanılır — run_cmd shell=True)
    # Kurulum ASLA otomatik değildir: 'propose' öneri sunar, 'apply --yes' (veya
    # interaktif onay) sonrası çalışır. Üzerine yazma YOKTUR (zaten kuruluysa RED).
    "tools": {
        "cuda-toolkit": {
            "check": ["nvcc", "/usr/local/cuda/bin/nvcc"],
            "gpu": True, "min_ram_gb": 8, "min_disk_gb": 25, "min_cpus": 4,
            "desc": "NVIDIA CUDA toolkit — GPU hesaplama (vLLM/torch)",
            "install": ("curl -fsSL https://developer.download.nvidia.com/compute/"
                        "cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb "
                        "-o /tmp/cuda-keyring.deb && sudo dpkg -i /tmp/cuda-keyring.deb "
                        "&& sudo apt-get update && sudo apt-get -y install cuda-toolkit-12-8"),
        },
        "vllm": {
            "check": ["vllm"],
            "gpu": True, "min_ram_gb": 16, "min_disk_gb": 30, "min_cpus": 4,
            "desc": "vLLM — yüksek verimli LLM serving (GPU zorunlu)",
            "install": "pip install vllm",
        },
        "ollama": {
            "check": ["ollama"],
            "gpu": False, "min_ram_gb": 8, "min_disk_gb": 20, "min_cpus": 2,
            "desc": "Ollama — yerel LLM runtime (CPU/GPU)",
            "install": "curl -fsSL https://ollama.com/install.sh | sh",
        },
        "docker": {
            "check": ["docker"],
            "gpu": False, "min_ram_gb": 4, "min_disk_gb": 10, "min_cpus": 2,
            "desc": "Docker — konteyner runtime (nRF SDK dahil)",
            "install": "curl -fsSL https://get.docker.com | sh",
        },
        "zephyr-sdk": {
            "check": ["west"],
            "gpu": False, "min_ram_gb": 4, "min_disk_gb": 15, "min_cpus": 2,
            "desc": "Zephyr/NCS build zinciri (west)",
            "install": "pip install west",
        },
        "kicad-cli": {
            "check": ["kicad-cli"],
            "gpu": False, "min_ram_gb": 2, "min_disk_gb": 3, "min_cpus": 2,
            "desc": "KiCad komut satırı — PCB DRC/ERC/üretim",
            "install": "sudo apt-get -y install kicad",
        },
        "arm-none-eabi-gcc": {
            "check": ["arm-none-eabi-gcc"],
            "gpu": False, "min_ram_gb": 1, "min_disk_gb": 1, "min_cpus": 1,
            "desc": "ARM gömülü derleyici (Cortex-M)",
            "install": "sudo apt-get -y install gcc-arm-none-eabi",
        },
        "qemu-system-arm": {
            "check": ["qemu-system-arm"],
            "gpu": False, "min_ram_gb": 1, "min_disk_gb": 1, "min_cpus": 1,
            "desc": "QEMU ARM emülasyonu",
            "install": "sudo apt-get -y install qemu-system-arm",
        },
        "ns3": {
            "check": ["ns3"],
            "gpu": False, "min_ram_gb": 2, "min_disk_gb": 2, "min_cpus": 2,
            "desc": "ns-3 ağ simülatörü",
            "install": "sudo apt-get -y install ns3",
        },
    },
    "state": {
        "manifest_local": "~/.hermes/state/sync_motor_manifest.json",
        "logfile": "~/.hermes/state/sync_motor.log",
    },
}


def detect_machine(hostname=None, machines=None):
    """H1 mi H2 mi H3 mü OpenClaw mu? hostname + OS kombinasyonu ile.

    29 Ağu 2026 FIX (H2 bulgusu): h3_hostnames HİÇ KONTROL EDİLMİYORDU ve liste
    daima DEFAULT_CONFIG'ten okunuyordu (kullanıcının config.json'undaki machines
    bloğu yoksayılıyordu). Sonuç: H3 (hermesagent03, Linux) OS fallback'ine düşüp
    kendini "H1" sanıyordu → retention_machine="H1" ayarında H1 VE H3 birlikte
    prune deniyor, aynı restic repoyu kilitliyorlardı.
    `machines` verilmezse eski davranış korunur (geriye uyumlu).
    """
    hostname = hostname or (os.uname().nodename if os.name != "nt"
                            else os.environ.get("COMPUTERNAME", ""))
    hn = hostname.lower()
    m = machines or DEFAULT_CONFIG["machines"]

    for h1 in m.get("h1_hostnames", []):
        if h1.lower() in hn:
            return "H1"
    for h2 in m.get("h2_hostnames", []):
        if h2.lower() in hn:
            return "H2"
    for h3 in m.get("h3_hostnames", []):
        if h3.lower() in hn:
            return "H3"
    for oc in m.get("openclaw_hostnames", []):
        if oc.lower() in hn:
            return "OPENCLAW"
    # OS fallback
    return "H2" if os.name == "nt" else "H1"


def load_config(path=None):
    """config.json yükle (yoksa default). Makineye göre dirs düzelt."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "config.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                user_cfg = json.load(f)
            # Derin birleştirme: kullanıcı config'i varsa 'dirs' TAMAMEN kullanıcının
            # değeriyle değiştirilir (DEFAULT_CONFIG node'ları Windows'ta path'siz
            # kalıp gereksiz yere koşmaz / geri gelmez — hermes-full örneği).
            for section, values in user_cfg.items():
                if section == "dirs" and isinstance(values, dict):
                    cfg["dirs"] = values
                elif isinstance(values, dict) and section in cfg:
                    cfg[section].update(values)
                else:
                    cfg[section] = values
        except Exception as e:
            log.warning(f"config.json okunamadı ({e}), default kullanılıyor")

    # Makine tespiti
    # 29 Ağu 2026: kullanıcının config.json'undaki machines bloğu artık kullanılır
    # (önce daima DEFAULT_CONFIG okunuyordu → H3 tanınmıyordu).
    cfg["machine"] = detect_machine(machines=cfg.get("machines"))
    cfg["is_h1"] = cfg["machine"] == "H1"

    # Kimlik: user_id + machine_id (GDrive yolu buna bağlı)
    user_id = cfg.get("identity", {}).get("user_id", "default")
    machine_id = cfg.get("identity", {}).get("machine_id", "")
    if not machine_id:
        machine_id = (os.uname().nodename if os.name != "nt"
                      else os.environ.get("COMPUTERNAME", "unknown"))
        machine_id = machine_id.replace(" ", "_").lower()
        cfg["identity"]["machine_id"] = machine_id
    # GDrive versiyonlu yol: gdrive:hermes-sync/<user_id>/<machine_id>/versiyonlu
    cfg["gdrive"]["versioned_dir"] = (
        f"gdrive:hermes-sync/{user_id}/{machine_id}/versiyonlu")
    cfg["gdrive"]["user_root"] = f"gdrive:hermes-sync/{user_id}"

    # Windows'ta dizin yollarını çevir — SADECE config.json'da tanımlanmamışsa.
    # (Öncesi patent'i her zaman C:\ProjectCumulus ile eziyordu; kullanıcı
    # config'inde gerçek yol varken yanlış dizine bakıyordu — H2 29 Ağu 2026.)
    if os.name == "nt":
        _d = cfg.get("dirs", {})
        if "kernel" in _d and "paths" not in _d["kernel"]:
            _d["kernel"]["paths"] = ([_d["kernel"]["path"]] if _d["kernel"].get("path")
                                     else [r"C:\cumulusos"])
        if "patent" in _d and not _d["patent"].get("path"):
            _d["patent"]["path"] = r"C:\ProjectCumulus"
        if "scripts" in _d and not _d["scripts"].get("path"):
            _d["scripts"]["path"] = str(Path.home() / ".hermes" / "scripts")
        if "openclaw" in _d and not _d["openclaw"].get("path"):
            _d["openclaw"]["path"] = str(Path.home() / ".openclaw")

    # Yol genişlet (~ → home)
    for k in ("manifest_local", "logfile"):
        cfg["state"][k] = os.path.expanduser(cfg["state"][k])

    return cfg


# ═══════════════════════════════════════════════════════════════
# ÇEKİRDEK: HASH + DOSYA YARDIMCILARI
# ═══════════════════════════════════════════════════════════════

def sha256_file(path):
    """Büyük dosyalar için chunked SHA256."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def matches_glob(path, patterns):
    """Dosya adı glob pattern'lerden birine uyuyor mu?"""
    import fnmatch
    return any(fnmatch.fnmatch(os.path.basename(path), p) for p in patterns)


def should_exclude_dir(dirname, exclude_dirs):
    return dirname in exclude_dirs


# ═══════════════════════════════════════════════════════════════
# ENVANTER TARAMA
# ═══════════════════════════════════════════════════════════════

def scan_directory(label, dir_cfg):
    """
    Bir dizini tara; include pattern + boyut filtresi uygula.
    Çoklu yol desteği: dir_cfg['paths'] veya tek 'path'.
    GÜVENLİK: .env, *.key, token içeren dosyalar ASLA kapsama alınmaz.
    Dönen: {relpath: {sha, size, mtime, machine}}
    Çoklu yol çakışması: aynı göreli yol iki kökte varsa → çakışma
    uyarısı loglanır, son kökün kaydı KULLANILMAZ (veri kaybı önlemi).
    """
    # Çoklu yol veya tek yol
    paths = dir_cfg.get("paths") or [dir_cfg["path"]]
    include = dir_cfg.get("include", ["*"])
    exclude_dirs = dir_cfg.get("exclude_dirs", [])
    max_bytes = dir_cfg.get("max_size_kb", 512) * 1024

    # GÜVENLİK: hassas dosya kalıpları — asla manifest'e girmez
    SECRET_PATTERNS = (".env", ".env.*", "*.key", "*.pem", "*.p12",
                       "id_rsa", "id_ed25519", "*.token", "secrets",
                       "credentials", "service-account", "*-sa-key")
    # GÜVENLİK: hassas dizin adları — yol bileşeninden reddedilir
    SECRET_DIRS = ("secrets", "credentials", "service-account",
                   ".aws", ".ssh", "private", "keys", "tokens")
    # GPU POLİTİKASI (26 Ağu, OceanAPI #5): H2 model ağırlıkları senkron DIŞINDA
    # Büyük model dosyaları yalnız metadata olarak işaretlenir, içerik taşınmaz
    GPU_SKIP_PATTERNS = ("*.gguf", "*.safetensors", "*.pt", "*.pth",
                         "*.ckpt", "*.onnx", "*.bin", "*.h5", "*.pb",
                         "*.tflite", "*.model", "*.lora", "*.adapter")
    GPU_MAX_KB = 51200  # 50MB üzeri model dosyası senkron dışı (metadata-only)
    # GPT-5.6 P0 (15 Ağu): İÇERİK taraması — dosya adı filtresi yetmez;
    # riskli adaylarda token/anahtar PREFIX'leri taranır (≤64KB, performans).
    # NOT (8 Eyl konsensüs): literal alt dize kontrolü ("in") kullanılır —
    # REGEX yazmayın, çalışmaz — çünkü bytes literal olarak kalır ve gerçek
    # anahtarla asla eşleşmez (bkz. tests/test_secret_scan.py regresyon kapısı).
    # Provider prefix'leri: sk- (DeepSeek/OceanAPI/OpenAI), cr_ (QCode),
    # yzk_ (YapayZekaLab), nvapi- (NVIDIA), fc- (Firecrawl), AIza (Google),
    # ghp_/github_pat_ (GitHub PAT), AKIA (AWS), xoxb-/xoxp- (Slack).
    CONTENT_SCAN_NAMES = ("config.json", "config.yaml", "settings.yaml", "settings.yml",
                          "tokens.db", "rclone.conf", "backup.tar",
                          "credentials.txt", "secrets.txt", "token.db")
    CONTENT_PATTERNS = (b"BEGIN PRIVATE KEY", b"BEGIN OPENSSH PRIVATE KEY",
                        b"-----BEGIN", b"ghp_", b"github_pat_", b"AKIA",
                        b"xoxb-", b"xoxp-", b"sk-", b"cr_",
                        b"yzk_", b"nvapi-", b"fc-", b"AIza")

    def is_secret(fname):
        import fnmatch
        return any(fnmatch.fnmatch(fname, p) for p in SECRET_PATTERNS)

    def is_gpu_skip(fname, fsize):
        """GPU/model dosyası politikası: büyük model ağırlıkları senkron dışı."""
        import fnmatch
        if any(fnmatch.fnmatch(fname, p) for p in GPU_SKIP_PATTERNS):
            return True
        if fsize > GPU_MAX_KB * 1024 and os.path.splitext(fname)[1].lower() in (
                ".bin", ".dat", ".raw", ".npy", ".npz"):
            return True
        return False

    def content_scan(fpath, fname):
        """Yalnızca riskli aday isimlerinde içerik taraması (performans korunur)."""
        base_name = os.path.basename(fname).lower()
        if base_name not in CONTENT_SCAN_NAMES:
            return False
        try:
            with open(fpath, "rb") as f:
                head = f.read(65536)
            return any(p in head for p in CONTENT_PATTERNS)
        except OSError:
            return False

    inventory = {}
    for base in paths:
        base = os.path.expanduser(base)
        if not os.path.isdir(base):
            log.debug(f"{label}: dizin yok — {base}")
            continue

        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs
                       if not should_exclude_dir(d, exclude_dirs)
                       and d.lower() not in SECRET_DIRS]
            for fname in files:
                fpath = os.path.join(root, fname)
                # GÜVENLİK: secret dosyaları atla (ad + İÇERİK taraması)
                if is_secret(fname) or content_scan(fpath, fname):
                    log.debug(f"Güvenlik: {fname} atlandı (ad veya içerik)")
                    continue
                try:
                    stat = os.stat(fpath)
                except OSError:
                    continue
                # Boyut filtresi
                if stat.st_size > max_bytes:
                    continue
                # Pattern filtresi
                if not matches_glob(fpath, include):
                    continue
                rel = os.path.relpath(fpath, base)
                key = f"{label}/{rel}"
                # Çoklu yol çakışması: aynı anahtar zaten varsa
                if key in inventory:
                    # SHA aynıysa → aynı içerik, sorun yok (sessiz)
                    # SHA farklıysa → GERÇEK çakışma (uyar + ilki koru)
                    existing_sha = inventory[key].get("sha")
                    new_sha = sha256_file(fpath)
                    if existing_sha != new_sha:
                        log.debug(f"Çoklu yol farkı: {key} iki kökte "
                                  f"FARKLI içerik — ilki korunuyor "
                                  f"(worktree + ana repo farklı branch)")
                    continue
                inventory[key] = {
                    "sha": sha256_file(fpath),
                    "size": stat.st_size,
                    "mtime": int(stat.st_mtime),
                    "machine": detect_machine(),
                }
    return inventory


def scan_all(cfg):
    """Tüm yapılandırılmış dizinleri tara, birleşik envanter döndür."""
    inventory = {}
    for label, dir_cfg in cfg["dirs"].items():
        inventory.update(scan_directory(label, dir_cfg))
    return inventory


# ═══════════════════════════════════════════════════════════════
# MANİFEST
# ═══════════════════════════════════════════════════════════════

def load_manifest(cfg):
    path = cfg["state"]["manifest_local"]
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return json.load(f)
        except Exception:
            pass
    return {"version": __version__, "files": {}, "last_sync": None,
            "machine": cfg["machine"]}


def save_manifest(cfg, mf):
    # GPT-5.6 P1 (15 Ağu): manifest meta — rollback/replay koruması başlangıcı
    mf.setdefault("schema", 2)
    mf["machine_id"] = detect_machine()
    mf["created_at"] = datetime.now().isoformat()
    mf["generation"] = int(mf.get("generation", 0)) + 1
    path = cfg["state"]["manifest_local"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mf, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)   # atomik yaz


def detect_changes(cfg):
    """Yerel envanter vs manifest → yeni/değişen/silinen."""
    local = scan_all(cfg)
    mf = load_manifest(cfg)
    known = mf.get("files", {})

    new, changed, deleted = [], [], []
    for path, info in local.items():
        if path not in known:
            new.append(path)
        elif known[path].get("sha") != info["sha"]:
            changed.append(path)
    for path in known:
        if path not in local:
            deleted.append(path)

    return new, changed, deleted, local


# ═══════════════════════════════════════════════════════════════
# GITHUB MERKEZ (gh CLI veya API)
# ═══════════════════════════════════════════════════════════════

def gh_available():
    out, rc = run_cmd("gh --version")
    return rc == 0


# ─── Geçici hata + idempotent okuma tespiti (v2.1.1 — 30 Ağu 2026) ──
# run_cmd'de retry YALNIZCA idempotent OKUMA komutlarına uygulanır
# (cat/lsf/lsjson/lsd/status). Yazma komutlarına (copy/copyto/backup)
# ASLA retry YOK — çift yazma/kısmi durum fail-closed korunur.
_RETRY_READ_TOKENS = {"cat", "lsf", "lsjson", "lsd", "status", "ping",
                      "listremotes", "direxists", "about"}
# Yazma alt-komutları — dosya adı okuma kelimesine benzese bile (örn.
# 'rclone copy status <dest>') asla retry açılmaz (çift yazma fail-closed).
_RETRY_WRITE_TOKENS = {
    "copy", "copyto", "move", "mv", "backup", "push", "sync",
    "delete", "purge", "rm", "upload", "put", "send", "restore",
}
# rc==-1 (exception) durumunda KALICI hatalar geçici sayılmaz.
_RETRY_FATAL = ("no such file", "not found", "command not found",
                "permission denied", "is a directory")
_RETRY_NET_MARKERS = (
    "connection reset",
    "connection refused",
    "connection timed out",
    "network is unreachable",
    "temporary failure",
    "tls handshake timeout",
    "i/o timeout",
    "service unavailable",
    "timeout",
)
_RETRY_TRANSIENT = _RETRY_NET_MARKERS + ("timed out", "temporary", "reset")


def _is_idempotent_read(cmd_text: str) -> bool:
    """Komut metninin ilk 3 token'ında idempotent okuma alt-komutu var mı?

    Güvenlik: alt komut (2. token) bir YAZMA kelimesiyse asla retry açılmaz —
    'rclone copy status dest' gibi dosya adı okuma kelimesine benzeyen
    yazma komutları retry kapsamına girmez (OceanAPI denetimi #2/#3).
    """
    toks = str(cmd_text).split()
    if not toks:
        return False
    if len(toks) >= 2 and toks[1].lower() in _RETRY_WRITE_TOKENS:
        return False
    for t in toks[:3]:
        if t.lower() in _RETRY_READ_TOKENS:
            return True
    return False


def _is_transient_rc(rc: int, err: str) -> bool:
    e = (err or "").lower()
    if rc == -1:
        # exception — dosya/komut yokluğu gibi KALICI hatalar geçici değil
        return not any(m in e for m in _RETRY_FATAL)
    if re.search(r"\b5\d\d\b", e):
        return True
    return any(m in e for m in _RETRY_TRANSIENT)


def _lsd_names(out: str) -> list:
    """rclone lsd çıktısından dizin adlarını ayıkla (her satırın son token'ı).

    Pipeline'sız kullanım için: `| tail -1` / `| wc -l` yerine burada parse
    edilir — böylece rc GERÇEK rclone rc'si olur ve retry tetiklenebilir
    (Windows cmd.exe'de pipefail yok, CRLF ve boş satırlar tolere edilir).
    'Name' başlığı (lsjson tarzı çıktı) dizin adı sayılmaz.
    """
    names = []
    for ln in (out or "").splitlines():          # splitlines CRLF'i de böler
        toks = ln.split()
        if len(toks) < 2:
            continue
        # rclone lsd biçimi: "<boyut> <tarih> <saat> <boyut> <ad...>" — ad 4.
        # alandan sonra başlar ve boşluk içerebilir (son token almak adı
        # kırpardı — QCode denetimi #3).
        name = " ".join(toks[4:]) if len(toks) >= 5 else toks[-1]
        if name and name.lower() != "name":
            names.append(name)
    return names


def run_cmd(cmd, timeout=60, shell=False, retries=0):
    """
    Komut çalıştır — (stdout, returncode).
    Güvenlik: shell=True KAPALI — komut enjeksiyonuna karşı.
    shell=True gereken pipe komutları için shell=True AÇIKÇA verilir
    ve giriş değerleri config'den (kullanıcının kendi dosyası) gelir.

    retries>0: yalnızca idempotent OKUMA komutu (cat/lsf/status...) ve
    geçici hata (timeout/network/5xx) durumunda 1 retry (3s bekle).
    Yazma komutları retries>0 verilse bile retry YAPMAZ.
    """

    def _exec():
        try:
            if shell:
                r = subprocess.run(cmd, shell=True, capture_output=True,
                                   text=True, errors="replace", timeout=timeout)
            elif isinstance(cmd, (list, tuple)):
                # LIST komut doğrudan geç (split ETME — split list'te patlar)
                r = subprocess.run(list(cmd), capture_output=True,
                                   text=True, errors="replace", timeout=timeout)
            else:
                r = subprocess.run(cmd.split(), capture_output=True,
                                   text=True, errors="replace", timeout=timeout)
            return r.stdout.strip(), r.returncode, (r.stderr or "")
        except subprocess.TimeoutExpired:
            return "timeout", -1, f"TIMEOUT {timeout}s"
        except Exception as e:
            return str(e), -1, ""

    cmd_text = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
    can_retry = _is_idempotent_read(cmd_text)
    for attempt in range(retries + 1):
        t0 = time.monotonic()
        out, rc, err = _exec()
        dur = time.monotonic() - t0
        if rc == 0:
            return out, rc
        transient = _is_transient_rc(rc, err)
        if attempt < retries and can_retry and transient:
            log.warning(f"sync hata: {cmd_text[:120]} rc={rc} {dur:.1f}s retry={attempt + 1}/{retries}")
            time.sleep(3)
            continue
        if rc != 0:
            log.warning(f"sync hata: {cmd_text[:120]} rc={rc} {dur:.1f}s retry={attempt}")
        return out, rc


# ═══════════════════════════════════════════════════════════════
# AKILLI KURULUM — KAYNAK FARKINDALIĞI (v1.6)
# ═══════════════════════════════════════════════════════════════
# Felsefe: eşitleme SIRASINDA hiçbir kurulum otomatik yapılmaz.
# Kaynak node'da kurulu araçlar, hedef node için ÖNERİ olarak sunulur;
# öneri CPU/GPU/RAM/disk kaynak kontrolünden geçer. Kullanıcı onayı
# (apply --yes veya interaktif) sonrası kurulur. Üzerine yazma YOKTUR.

def resource_probe():
    """Yerel kaynakları ölç: CPU çekirdek, RAM (GB), disk boş (GB), GPU.

    GPU tespiti: nvidia-smi → lspci (VGA/3D/Display sınıfı) → vulkaninfo.
    Hiçbiri yoksa gpu=False. Ölçülemeyen değer 0 döner (fail-closed:
    'bilinmiyor' yerine 'yetersiz' kabul edilir → kurulum önerilmez).
    """
    res = {
        "cpus": os.cpu_count() or 0,
        "ram_gb": 0.0,
        "disk_gb": 0.0,
        "gpu": False,       # genel GPU (lspci/vulkan dahil)
        "nvidia": False,    # NVIDIA GPU (nvidia-smi doğrulanmış — CUDA için şart)
        "gpu_name": None,
    }
    # RAM — Linux /proc/meminfo; macOS sysctl; Windows GlobalMemoryStatusEx
    try:
        if os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        res["ram_gb"] = round(int(line.split()[1]) / 1024 / 1024, 1)
                        break
        elif os.name == "posix":
            out, rc = run_cmd("sysctl -n hw.memsize", timeout=5)
            if rc == 0 and out.isdigit():
                res["ram_gb"] = round(int(out) / 1e9, 1)
        elif os.name == "nt":
            import ctypes
            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = _MS()
            ms.dwLength = ctypes.sizeof(_MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            res["ram_gb"] = round(ms.ullTotalPhys / 1e9, 1)
    except Exception:
        pass
    # Disk — kullanıcı ev dizininin birimi (bulunamazsa kök)
    try:
        res["disk_gb"] = round(shutil.disk_usage(
            os.path.expanduser("~")).free / 1e9, 1)
    except Exception:
        try:
            res["disk_gb"] = round(shutil.disk_usage("/").free / 1e9, 1)
        except Exception:
            pass
    # GPU
    try:
        out, rc = run_cmd(
            "nvidia-smi --query-gpu=name --format=csv,noheader", timeout=10)
        if rc == 0 and out:
            res["gpu"] = True
            res["nvidia"] = True
            res["gpu_name"] = out.splitlines()[0].strip()[:60]
        else:
            out2, rc2 = run_cmd(
                "lspci 2>/dev/null | grep -iE 'vga|3d|display'",
                timeout=10, shell=True)
            if rc2 == 0 and out2:
                res["gpu"] = True
                res["gpu_name"] = out2.splitlines()[0].strip()[:60]
            else:
                out3, rc3 = run_cmd("vulkaninfo --summary", timeout=10)
                if rc3 == 0 and out3:
                    res["gpu"] = True
                    res["gpu_name"] = "vulkan"
    except Exception:
        pass
    return res


def tool_installed(tool_cfg):
    """Araç kurulu mu? check listesindeki HERHANGİ BİRİ bulunursa 'kurulu'."""
    checks = tool_cfg.get("check") or []
    if not checks:
        return False
    for c in checks:
        if shutil.which(c):
            return True
    return False


def scan_tools(cfg):
    """Config'teki tools kataloğunu tara → {ad: kurulum durumu}."""
    tools = cfg.get("tools", {})
    state = {}
    for name, t in tools.items():
        state[name] = {
            "installed": tool_installed(t),
            "gpu": bool(t.get("gpu", False)),
            "min_ram_gb": t.get("min_ram_gb", 0),
            "min_disk_gb": t.get("min_disk_gb", 0),
            "min_cpus": t.get("min_cpus", 1),
        }
    return state


def propose_install(tool_name, tool_cfg, local_res, local_tools, remote_tools):
    """Uzak node'da kurulu bir aracı bu makineye kurmayı değerlendir.

    Dönüş: (durum, mesaj)
      ALREADY            — zaten kurulu (kurma — üzerine asla yazma)
      NOT_ON_SOURCE      — kaynak node'da kurulu değil (öneri yok)
      GPU_MISSING        — GPU gerekli, bu makinede GPU yok
      DISK_INSUFFICIENT  — boş disk min_disk_gb'dan az
      RAM_INSUFFICIENT   — RAM min_ram_gb'dan az
      CPU_INSUFFICIENT   — çekirdek sayısı min_cpus'tan az
      INSTALLABLE        — kaynaklar yeterli, kurulabilir (öneri)
    """
    local_inst = bool(local_tools.get(tool_name, {}).get("installed", False))
    if local_inst:
        return "ALREADY", "zaten kurulu"
    remote_inst = bool(remote_tools.get(tool_name, {}).get("installed", False))
    if not remote_inst:
        return "NOT_ON_SOURCE", "kaynak node'da kurulu değil"

    if tool_cfg.get("gpu") and not local_res.get("nvidia"):
        return "GPU_MISSING", "NVIDIA GPU gerekli — bu makinede CUDA uyumlu GPU yok"
    min_disk = float(tool_cfg.get("min_disk_gb", 0) or 0)
    free_disk = float(local_res.get("disk_gb", 0) or 0)
    if free_disk < min_disk:
        return ("DISK_INSUFFICIENT",
                f"boş disk {free_disk:.1f}GB < {min_disk:.0f}GB gerekli")
    min_ram = float(tool_cfg.get("min_ram_gb", 0) or 0)
    ram = float(local_res.get("ram_gb", 0) or 0)
    if ram < min_ram:
        return "RAM_INSUFFICIENT", f"RAM {ram:.1f}GB < {min_ram:.0f}GB gerekli"
    min_cpus = int(tool_cfg.get("min_cpus", 1) or 1)
    cpus = int(local_res.get("cpus", 0) or 0)
    if cpus < min_cpus:
        return "CPU_INSUFFICIENT", f"CPU {cpus} < {min_cpus} gerekli"
    return "INSTALLABLE", "kurulabilir"


def gh_ensure_repo(cfg):
    repo = cfg["github"]["repo"]
    # PIPE YOK: gh RC doğrudan alınır (pipe RC'yi yutuyordu)
    # shell=True: --jq .name + 2>&1 .split() ile parçalanıyor
    out, rc = run_cmd(
        f"gh repo view {repo} --json name --jq .name 2>&1",
        timeout=30, shell=True)
    if rc == 0 and out and "not found" not in out.lower():
        log.info(f"GitHub repo hazır: {repo}")
        return True
    out, rc = run_cmd(
        f"gh repo create {repo} --private "
        f"--description 'Hermes Sync manifest merkezi'",
        timeout=30, shell=True)
    if rc == 0:
        log.info(f"GitHub repo oluşturuldu: {repo}")
        return True
    log.error(f"Repo oluşturulamadı: {out[:120]}")
    return False


def gh_push_manifest(cfg, mf):
    """Manifest'i GitHub'a API ile push et (repo clone'suz).
    Doğrudan urllib ile çağrılır — shell argüman limiti sorununu aşar.
    GitHub content API limiti ~1MB — manifest küçük tutulmalı.

    v1.8.0 (28 Ağu, OceanAPI #1): SHARD manifest — 27MB tek dosya yerine
    node başına ayrı JSON. Sadece değişen node'un shard'ı push edilir.
    """
    import base64
    import urllib.request
    import urllib.error

    repo = cfg["github"]["repo"]
    token = _gh_token()
    if not token:
        log.error("gh token alınamadı — gh auth status kontrol")
        return False

    # Shard'lama: her node ayrı dosya
    import hashlib
    ts = datetime.now().isoformat(timespec="seconds")
    pushed = 0
    for node, node_files in _split_by_node(mf):
        if not node_files:
            continue
        shard = {
            "node": node,
            "ts": ts,
            "revision": hashlib.sha256(json.dumps(node_files, sort_keys=True).encode()).hexdigest()[:12],
            "file_count": len(node_files),
            "files": node_files,
        }
        content = json.dumps(shard, indent=1, ensure_ascii=False)
        b64 = base64.b64encode(content.encode()).decode()
        mfile = f"manifest/sync_manifest.{node}.json"

        # Mevcut SHA
        sha = None
        url_get = f"https://api.github.com/repos/{repo}/contents/{mfile}"
        req_get = urllib.request.Request(url_get,
                headers={"Authorization": f"Bearer {token}", "User-Agent": "sync-motor"})
        try:
            with urllib.request.urlopen(req_get, timeout=20) as resp:
                data = json.loads(resp.read().decode())
                sha = data.get("sha")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log.warning(f"shard GET {e.code} {node}")
        except Exception as e:
            log.warning(f"shard GET: {e}")

        payload = {"message": f"sync {ts} {node}", "content": b64}
        if sha:
            payload["sha"] = sha
        url_put = f"https://api.github.com/repos/{repo}/contents/{mfile}"
        req_put = urllib.request.Request(url_put,
                data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {token}", "User-Agent": "sync-motor",
                         "Content-Type": "application/json"},
                method="PUT")
        try:
            with urllib.request.urlopen(req_put, timeout=30) as resp:
                resp.read()
                pushed += 1
        except Exception as e:
            log.warning(f"shard PUT {node}: {e}")
    log.info(f"Shard push: {pushed} node güncellendi")
    return pushed > 0

def _split_by_node(mf):
    """Manifest'i node öneklerine göre böl (shard)."""
    nodes = sorted({p.split("/")[0] for p in (mf.get("files", {}) or {})})
    for node in nodes:
        prefix = f"{node}/"
        node_files = {p[len(prefix):]: info for p, info in (mf.get("files", {}) or {}).items()
                      if p.startswith(prefix)}
        yield node, node_files


def gh_fetch_manifest(cfg):
    """GitHub'dan uzak manifest çek (urllib ile).
    v1.8.0 (28 Ağu): SHARD destekli — manifest/*.json dosyalarını birleştir.
    Eski tek dosya (sync_manifest.json) da desteklenir (geriye uyum)."""
    import base64
    import urllib.request
    import urllib.error

    repo = cfg["github"]["repo"]
    mfile = cfg["github"]["manifest_file"]
    token = _gh_token()
    if not token:
        return None

    merged = {"files": {}}

    # 1) Shard'ları dene (manifest/ dizini)
    url = f"https://api.github.com/repos/{repo}/contents/manifest"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "User-Agent": "sync-motor"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            shard_list = json.loads(resp.read().decode())
        for item in shard_list:
            if item.get("type") != "file" or not item["name"].endswith(".json"):
                continue
            dl_url = item.get("download_url")
            if not dl_url:
                continue
            req2 = urllib.request.Request(
                dl_url, headers={"Authorization": f"Bearer {token}",
                                 "User-Agent": "sync-motor"})
            with urllib.request.urlopen(req2, timeout=60) as resp2:
                shard = json.loads(resp2.read().decode())
            node = shard.get("node", item["name"].replace("sync_manifest.", "").replace(".json", ""))
            for fpath, finfo in (shard.get("files", {}) or {}).items():
                merged["files"][f"{node}/{fpath}"] = finfo
        if merged["files"]:
            return merged
    except urllib.error.HTTPError:
        pass  # manifest/ yok — eski tek dosyaya düş
    except Exception as e:
        log.debug(f"shard manifest GET: {e}")

    # 2) Eski tek dosya (geriye uyum)
    url = f"https://api.github.com/repos/{repo}/contents/{mfile}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}",
                      "User-Agent": "sync-motor"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        # Büyük manifest için content boş olabilir (GitHub 1MB API limiti)
        # → download_url (raw) ile çek, auth gerekmez (private ise token)
        dl_url = data.get("download_url")
        if dl_url:
            req2 = urllib.request.Request(
                dl_url, headers={"Authorization": f"Bearer {token}",
                                 "User-Agent": "sync-motor"})
            with urllib.request.urlopen(req2, timeout=60) as resp2:
                content = resp2.read().decode()
            return json.loads(content)
        # Fallback: content base64 (küçük manifest)
        content = base64.b64decode(data["content"]).decode()
        return json.loads(content)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log.debug("Uzak manifest yok (ilk kurulum)")
        else:
            log.warning(f"manifest GET {e.code}")
        return None
    except Exception as e:
        log.warning(f"manifest GET: {e}")
        return None


def _gh_token():
    """gh auth token'ı al (gh auth token komutu)."""
    out, rc = run_cmd("gh auth token", timeout=15)
    token = out.strip()
    return token if rc == 0 and token and " " not in token else None


# ═══════════════════════════════════════════════════════════════
# GDRIVE (BÜYÜK DOSYALAR, VERSİYONLU)
# ═══════════════════════════════════════════════════════════════

def rclone_available():
    out, rc = run_cmd("rclone version")
    return rc == 0


def _pack_node(base, pkg, include, exclude_dirs, limit=5000):
    """Node dizinini .tar.gz'e paketle — saf Python, kabuk yok.

    include: fnmatch desenleri (dosya adı üzerinde)
    exclude_dirs: atlanacak dizin adları
    Dönüş: 0 başarı, 255 hata/boş (eski kabuk sözleşmesiyle uyumlu).
    """
    import tarfile as _tf
    import fnmatch
    exc = {d.lower().strip("*/") for d in (exclude_dirs or [])}
    picked = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d.lower() not in exc]
        for f in files:
            if any(fnmatch.fnmatch(f, p) for p in (include or ["*"])):
                picked.append(os.path.join(root, f))
                if len(picked) >= limit:
                    break
        if len(picked) >= limit:
            break
    if not picked:
        return 255
    try:
        with _tf.open(pkg, "w:gz") as tf:
            for fp in picked:
                try:
                    tf.add(fp, arcname=os.path.relpath(fp, base))
                except OSError:
                    continue          # kilitli/okunamayan dosya paketi bozmasın
    except Exception as e:
        log.debug(f"_pack_node: {e}")
        return 255
    return 0 if os.path.exists(pkg) and os.path.getsize(pkg) > 0 else 255


def gdrive_snapshot(cfg, node=None):
    """
    Seçilen node'ların GDrive'da versiyonlu snapshot'ını al.
    Per-node klasör: versiyonlu/<node>/<timestamp>/
    Üzerine ASLA yazmaz — her seferinde yeni timestamp klasörü.
    """
    if not rclone_available():
        log.warning("rclone yok — GDrive snapshot atlandı")
        return None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    workdir = os.path.join(tempfile.gettempdir(), "sync_motor_snapshot")
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)

    # Hangi node'lar?
    nodes = [node] if node else list(cfg["dirs"].keys())
    results = {}

    for label in nodes:
        dir_cfg = cfg["dirs"].get(label)
        if not dir_cfg or not dir_cfg.get("gdrive", False):
            continue
        # Çoklu yol desteği
        paths = dir_cfg.get("paths") or [dir_cfg.get("path", "")]
        base = None
        for p in paths:
            pexp = os.path.expanduser(p)
            if os.path.isdir(pexp):
                base = pexp
                break
        if not base:
            log.debug(f"{label}: dizin yok")
            continue

        # Node başına ayrı paket — include pattern'leri ile (tar değil)
        pkg = f"{workdir}/{label}.tar.gz"
        include = dir_cfg.get("include", ["*"])
        excl = dir_cfg.get("exclude_dirs", [])
        base_expanded = os.path.expanduser(base)

        # find ile filtrele: include pattern'lerine uyan dosyaları topla
        # (29GB dizinlerde tar yerine find çok daha hızlı)
        find_expr = []
        for pat in include:
            find_expr.append(f'-name "{pat}"')
        find_cmd = " -o ".join(find_expr)
        excl_find = " ".join(f"-not -path '*/{d}/*'" for d in excl)

        # Paketleme: Python tarfile (platformdan bağımsız).
        # Eski hal find|head|tar boru hattıydı — Windows'ta shell=True cmd.exe'ye
        # düşüyor, find/head/tar bulunmuyordu (rc=255). Aynı semantik korunur:
        # include pattern eşleşmesi, exclude_dirs budama, 5000 dosya sınırı.
        rc = _pack_node(base_expanded, pkg, include, excl, limit=5000)
        if rc != 0 or not os.path.exists(pkg) or os.path.getsize(pkg) == 0:
            log.warning(f"{label}: paket oluşturulamadı "
                        f"(rc={rc}, boyut={os.path.exists(pkg) and os.path.getsize(pkg)})")
            continue

        # Node'a özel GDrive hedef
        target = f"{cfg['gdrive']['versioned_dir']}/{label}/{ts}"
        out, rc = run_cmd(
            f'rclone copy "{pkg}" "{target}" --ignore-checksum '
            f'--no-traverse', timeout=180, shell=True)
        if rc == 0:
            sz = os.path.getsize(pkg) // 1024
            results[label] = f"{target} ({sz}KB)"
            log.info(f"GDrive: {label} → versiyonlu/{label}/{ts} ({sz}KB)")

    return results if results else None


# ═══════════════════════════════════════════════════════════════
# TAILSCALE / HTTP FARKINDALIK
# ═══════════════════════════════════════════════════════════════

def http_get(url, timeout=8):
    """Basit HTTP GET — (içerik, durum)."""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "sync-motor"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore"), resp.status
    except Exception as e:
        return str(e), -1


def peer_status(cfg):
    """Karşı tarafın durumu — farkındalık."""
    peer_url = (cfg["network"]["h2_http"] if cfg["is_h1"]
                else cfg["network"]["h1_http"])
    _, code = http_get(f"{peer_url}/", cfg["network"]["timeout_s"])
    return "ONLINE" if code == 200 else "OFFLINE"


def announce(cfg, msg):
    """Karşı tarafa durum notu bırak (her iki 9090'a)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"SYNC_NOTE_{ts}.txt"
    local = "/tmp/hermes_uploads" if cfg["is_h1"] else "/tmp"
    os.makedirs(local, exist_ok=True)
    try:
        with open(os.path.join(local, fname), "w", encoding="utf-8", errors="replace") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except OSError:
        pass
    # Karşı tarafa form-POST dene
    peer_url = (cfg["network"]["h2_http"] if cfg["is_h1"]
                else cfg["network"]["h1_http"])
    try:
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode({"file": fname}).encode()
        req = urllib.request.Request(f"{peer_url}/", data=data)
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass
    return fname


# ═══════════════════════════════════════════════════════════════
# ÇAKIŞMA YÖNETİMİ (NON-DESTRUCTIVE)
# ═══════════════════════════════════════════════════════════════

def list_conflicts(cfg):
    """Tüm yapılandırılmış dizinlerde .conflict.* dosyalarını bul."""
    found = []
    for label, dir_cfg in cfg["dirs"].items():
        paths = dir_cfg.get("paths") or [dir_cfg.get("path", "")]
        for p in paths:
            base = os.path.expanduser(p)
            if not os.path.isdir(base):
                continue
            for root, dirs, files in os.walk(base):
                for f in files:
                    if ".conflict." in f:
                        found.append(os.path.join(root, f))
    return found


# ═══════════════════════════════════════════════════════════════
# KOMUTLAR
# ═══════════════════════════════════════════════════════════════

def cmd_push(cfg, node=None, dry_run=False, skip_unchanged=False):
    print(f"\n  🔄 PUSH — {cfg['machine']}"
          + (f" [node: {node}]" if node else " [tüm node'lar]")
          + (" [DRY-RUN]" if dry_run else ""))
    if node and node not in cfg["dirs"]:
        log.error(f"Bilinmeyen node: {node} — mevcut: {list(cfg['dirs'].keys())}")
        return
    _skip_guard_done = False
    if skip_unchanged:
        fp = node_fingerprint(cfg, node)
        last = load_last_push(cfg).get(node)
        if last == fp:
            print(f"    ⏭ skip (değişiklik yok): {node}")
            return

    if node:
        # Tek node: sadece o dizini tara
        new, changed, deleted, local = detect_changes_node(cfg, node)
    else:
        new, changed, deleted, local = detect_changes(cfg)

    # DRY-RUN: ne yapılacağını göster, hiçbir şeye dokunma (v1.4)
    if dry_run:
        print(f"  DRY-RUN: {len(new)} yeni, {len(changed)} değişen, "
              f"{len(deleted)} silinen push edilecek")
        for f in (new + changed)[:10]:
            print(f"    + {f}")
        if len(new) + len(changed) > 10:
            print(f"    ... ve {len(new)+len(changed)-10} dosya daha")
        for f in deleted[:10]:
            print(f"    - {f}")
        print("  DRY-RUN: manifest, GitHub ve GDrive'a HİÇBİR ŞEY yazılmadı")
        return

    # AKILLI GATE: kernel node'da kod değiştiyse önce build doğrula
    affected_kernel = (node in (None, "kernel")) and bool(new or changed)
    if affected_kernel and "kernel" in cfg["dirs"]:
        log.info("Kernel değişikliği tespit — build gate çalışıyor...")
        if not verify_build(cfg):
            log.error("Build FAIL — bozuk kod eşitlenmiyor! "
                      "Hata düzeltilmeden push YAPILMAYACAK.")
            # Manifest'e build_break kaydı (farkındalık)
            mf = load_manifest(cfg)
            mf["build_break"] = {
                "time": datetime.now().isoformat(),
                "machine": cfg["machine"],
                "new": len(new), "changed": len(changed),
            }
            save_manifest(cfg, mf)
            return  # PUSH DURDURULDU — veri bütünlüğü korundu
        else:
            log.info("Build PASS — kod sağlıklı, push devam")

    if not new and not changed and not deleted:
        log.info("Değişiklik yok — push atlandı")
    else:
        log.info(f"Push: {len(new)} yeni, {len(changed)} değişen, "
                 f"{len(deleted)} silinen")

    # Manifest güncelle + GitHub'a yaz
    mf = load_manifest(cfg)
    if node:
        # Node'a özel anahtar: files.<node>
        node_files = mf.setdefault("node_files", {})
        node_files[node] = {k: v for k, v in local.items()
                            if k.startswith(f"{node}/")}
        mf["last_sync"] = datetime.now().isoformat()
        mf["machine"] = cfg["machine"]
    else:
        mf["files"] = local
        mf["last_sync"] = datetime.now().isoformat()
        mf["machine"] = cfg["machine"]

    # AKILLI (v1.6): push sırasında kaynak + araç durumunu manifest'e ekle.
    # Karşı node 'pull' + 'propose' ile bunları okur; kurulum önerisi
    # CPU/GPU/RAM/disk kontrolünden geçirilir. Kurulum ASLA otomatik değil.
    mf["resources"] = resource_probe()
    mf["tools_state"] = scan_tools(cfg)
    mf["probe_time"] = datetime.now().isoformat()
    save_manifest(cfg, mf)

    if gh_available():
        if gh_ensure_repo(cfg):
            gh_push_manifest(cfg, mf)
    else:
        log.warning("gh CLI yok — GitHub push atlandı (manifest yerel)")

    # GDrive versiyonlu snapshot (node'a özel)
    gdrive_snapshot(cfg, node=node)

    # Karşı tarafa bildir
    if len(new) + len(changed) > 0:
        announce(cfg, f"Push{(' ['+node+']') if node else ''}: "
                      f"{len(new)} yeni, {len(changed)} değişen ({cfg['machine']})")


def detect_changes_node(cfg, node):
    """Tek node için değişiklik tespiti."""
    dir_cfg = cfg["dirs"].get(node)
    if not dir_cfg:
        return [], [], [], {}
    local = scan_directory(node, dir_cfg)
    mf = load_manifest(cfg)
    node_files = mf.get("node_files", {}).get(node, {})
    # Ayrıca eski format files.<node/...> kontrolü
    legacy = {k: v for k, v in mf.get("files", {}).items()
              if k.startswith(f"{node}/")}
    known = {**legacy, **node_files}

    new, changed, deleted = [], [], []
    for path, info in local.items():
        if path not in known:
            new.append(path)
        elif known[path].get("sha") != info["sha"]:
            changed.append(path)
    for path in known:
        if path not in local:
            deleted.append(path)
    return new, changed, deleted, local


    # v1.6.2: delta push — başarılı push sonrası parmak izini kaydet
    if node and not dry_run:
        save_last_push(cfg, node, node_fingerprint(cfg, node))
    return 0


def cmd_add_node(cfg, name, path, include="*", max_kb=1024):
    """
    Yeni node ekle — config.json'a yazar. Node sayısı SINIRSIZ.
    Kullanım: python3 sync_motor.py add-node docs --path ~/docs --include "*.md"
    """
    import json as _json
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "config.json")
    # Mevcut config'i yükle (varsa)
    user_cfg = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8", errors="replace") as f:
                user_cfg = _json.load(f)
        except Exception:
            user_cfg = {}

    # Node ekle
    dirs = user_cfg.setdefault("dirs", {})
    if name in dirs:
        log.warning(f"Node zaten var: {name} (güncelleniyor)")
    dirs[name] = {
        "path": os.path.expanduser(path),
        "include": include.split(",") if isinstance(include, str) else include,
        "exclude_dirs": [".git", "build", "__pycache__"],
        "max_size_kb": max_kb,
        "gdrive": True,
    }

    with open(config_path, "w", encoding="utf-8") as f:
        _json.dump(user_cfg, f, indent=2, ensure_ascii=False)
    log.info(f"Node eklendi: {name} → {path} (config.json)")
    log.info("Şimdi: python3 sync_motor.py push --node " + name)


def cmd_share(cfg, node, target_user):
    """
    Node'u başka kullanıcıyla PAYLAŞ — GDrive'a kopyalar.
    A kullanıcısının node'u → gdrive:hermes-sync/<target_user>/shared/<node>/
    Kullanım: python3 sync_motor.py share kernel --to ahmet
    """
    import shutil as _shutil
    if node not in cfg["dirs"]:
        log.error(f"Bilinmeyen node: {node}")
        return

    # En son versiyonu paylaşım alanına kopyala
    src_root = cfg["gdrive"]["versioned_dir"]
    dst_root = f"gdrive:hermes-sync/{target_user}/shared"
    out, rc = run_cmd(
        f'rclone copy "{src_root}/{node}/" "{dst_root}/{node}/" '
        f'--ignore-checksum --no-traverse', timeout=180, shell=True)
    if rc == 0:
        log.info(f"Node paylaşıldı: {node} → {target_user}/shared/{node}")
        log.info(f"Hedef kullanıcı çeker: sync_motor.py pull --from-shared {node}")
    else:
        log.error(f"Paylaşım başarısız: {out[:100]}")


def cmd_nodes(cfg):
    """Tüm node'ları + GDrive versiyon geçmişini listele."""
    print("\n  📦 NODE'LAR")
    for label, dir_cfg in cfg["dirs"].items():
        paths = dir_cfg.get("paths") or [dir_cfg.get("path", "")]
        base = None
        for p in paths:
            pexp = os.path.expanduser(p)
            if os.path.isdir(pexp):
                base = pexp
                break
        exists = bool(base)
        gd = dir_cfg.get("gdrive", False)
        # GDrive versiyon sayısı
        ver_count = "?"
        if gd and rclone_available():
            out, rc = run_cmd(
                f'rclone lsd {cfg["gdrive"]["versioned_dir"]}/{label}',
                timeout=30, retries=1)
            ver_count = str(len(_lsd_names(out))) if rc == 0 else "?"
        print(f"  {'🟢' if exists else '🔴'} {label:12s} "
              f"{'GDrive:'+str(ver_count)+' ver' if gd else 'lokal'}")
    print()


def cmd_select(cfg):
    """İnteraktif node seçimi — hangi node eşitlensin?"""
    print("\n  🎯 NODE SEÇİMİ")
    labels = list(cfg["dirs"].keys())
    for i, label in enumerate(labels, 1):
        dir_cfg = cfg["dirs"][label]
        paths = dir_cfg.get("paths") or [dir_cfg.get("path", "")]
        exists = False
        for p in paths:
            if os.path.isdir(os.path.expanduser(p)):
                exists = True
                break
        print(f"  [{i}] {'🟢' if exists else '🔴'} {label}")
    print(f"  [0] TÜMÜ")
    print(f"  [q] Çık")

    try:
        choice = input("\n  Seçim: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if choice in ("q", ""):
        return
    if choice == "0":
        log.info("TÜM node'lar seçildi — both çalıştırılıyor")
        cmd_push(cfg, node=None)
        cmd_pull(cfg)
        return
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(labels):
            node = labels[idx]
            log.info(f"Node seçildi: {node}")
            cmd_push(cfg, node=node)
            cmd_pull(cfg)
        else:
            log.error("Geçersiz seçim")
    except ValueError:
        log.error("Geçersiz seçim")


def cmd_status(cfg):
    print("\n" + "═" * 60)
    print(f"  CUMULUS SYNC MOTOR v{__version__} — DURUM")
    print(f"  Makine: {cfg['machine']} ({ {'H1': 'H1 VPS', 'H2': 'H2 Desktop', 'H3': 'H3 Node', 'OPENCLAW': 'OpenClaw'}.get(cfg['machine'], cfg['machine']) })")
    print("═" * 60)

    peer = peer_status(cfg)
    print(f"  Karşı taraf: {'🟢 ONLINE' if peer == 'ONLINE' else '🔴 OFFLINE'}")

    new, changed, deleted, local = detect_changes(cfg)
    print(f"\n  Yerel envanter: {len(local)} dosya (filtrelenmiş)")
    print(f"  Yeni: {len(new)} | Değişen: {len(changed)} | Silinen: {len(deleted)}")

    mf = load_manifest(cfg)
    print(f"  Manifest: {len(mf.get('files', {}))} kayıt, "
          f"son sync: {mf.get('last_sync', '—')}")

    remote = gh_fetch_manifest(cfg)
    if remote:
        rfiles = remote.get("files", {})
        local_paths = set(local.keys())
        remote_only = [p for p in rfiles if p not in local_paths]
        print(f"  GitHub manifest: {len(rfiles)} kayıt, "
              f"uzaktan gelecek: {len(remote_only)}")
    else:
        print(f"  GitHub manifest: erişilemedi (gh auth kontrol)")

    conflicts = list_conflicts(cfg)
    print(f"  Çakışma: {len(conflicts)}")
    print("═" * 60 + "\n")


def gdrive_pull_latest(cfg, node):
    """
    GDrive'dan node'un EN SON versiyonunu çek ve doğrula.
    Non-destructive: hedef dizine yazar ama çakışan dosyalar .conflict.TS.
    """
    if not rclone_available():
        log.warning("rclone yok — GDrive pull atlandı")
        return False

    # En son versiyon klasörünü bul — pipeline YOK: rc GERÇEK rclone rc'si.
    # (Eski `| tail -1` rc'yi tail'den alıyordu → retry hiç tetiklenmiyor ve
    #  ağ hatası 'versiyon yok' gibi görünüyordu — v2.1.2 düzeltmesi.)
    out, rc = run_cmd(
        f'rclone lsd {cfg["gdrive"]["versioned_dir"]}/{node}',
        timeout=30, retries=1)
    if rc != 0:
        log.warning(f"{node}: GDrive versiyon listesi alınamadı "
                    f"(rc={rc}, retry sonrası — pull atlandı)")
        return False
    names = _lsd_names(out)
    if not names:
        log.info(f"{node}: ⚠ GDrive'da versiyon yok (pull atlandı — node yedeklenmemiş)")
        return False
    # En son timestamp klasörü
    latest = names[-1]
    if not latest or not latest.replace("_", "").isdigit():
        log.warning(f"{node}: geçersiz versiyon klasörü: {latest}")
        return False

    # Paketi çek
    pkg = f"/tmp/sync_pull_{node}.tar.gz"
    out, rc = run_cmd(
        f'rclone copy {cfg["gdrive"]["versioned_dir"]}/{node}/{latest}/ '
        f'/tmp/sync_pull_{node}/ --ignore-checksum --no-traverse '
        f'--drive-acknowledge-abuse',
        timeout=180, shell=True)
    if rc != 0:
        log.error(f"{node}: GDrive pull başarısız")
        return False

    # Paketi aç (hedef dizine, çakışma korumalı)
    dir_cfg = cfg["dirs"].get(node)
    paths = dir_cfg.get("paths") or [dir_cfg.get("path", "")]
    target = None
    for p in paths:
        if os.path.isdir(os.path.expanduser(p)):
            target = os.path.expanduser(p)
            break
    if not target:
        log.warning(f"{node}: hedef dizin yok")
        return False

    pkg_file = f"/tmp/sync_pull_{node}/{node}.tar.gz"
    if not os.path.exists(pkg_file):
        # Rclone dosya adını korudu
        import glob
        found = glob.glob(f"/tmp/sync_pull_{node}/*.tar.gz")
        pkg_file = found[0] if found else None
    if not pkg_file:
        log.warning(f"{node}: paket bulunamadı")
        return False

    # Aç — çakışanları koru
    import tarfile
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")  # µs — aynı isim çakışmaz
    src_machine = detect_machine()

    def _safe_target(root, name):
        """GPT-5.6 P0: tar member path'i node kökünden kaçamaz."""
        root_abs = os.path.abspath(root)
        candidate = os.path.abspath(os.path.join(root_abs, name))
        if candidate != root_abs and not candidate.startswith(root_abs + os.sep):
            raise ValueError(f"güvenlik: path kök dışına kaçıyor: {name}")
        return candidate

    def _write_conflict(dest, src_machine):
        """Atomik çakışma kopyası — yarım dosya yazılmaz."""
        conflict = f"{dest}.conflict.{ts}.{src_machine}"
        tmp = conflict + ".tmp"
        shutil.copy2(dest, tmp)
        os.replace(tmp, conflict)
        return conflict

    try:
        with tarfile.open(pkg_file, "r:gz") as tf:
            # SEKANSİYEL iterasyon: gzip akışı tek geçişte açılır.
            # Eski kod getmembers() + rastgele extractfile() yapıyordu;
            # gzip geriye seek desteklemediği için her üye baştan
            # açılıyordu → 420MB/5000 dosyalık arşivde O(n²) → saatlerce %100 CPU.
            for member in tf:
                if not member.isfile():
                    continue
                # Göreli yol — ./ önekini kaldır
                name = member.name.lstrip("./")
                if not name:
                    continue
                dest = _safe_target(target, name)
                # Çakışma kontrolü: hedef var + farklı içerik
                if os.path.exists(dest):
                    # Önce BOYUT: farklıysa içerik okumaya gerek yok (hızlı yol)
                    if os.path.getsize(dest) != member.size:
                        conflict = _write_conflict(dest, src_machine)
                        log.warning(f"Çakışma: {name} → {conflict}")
                        continue  # yereli koru, uzak yazılmaz
                    src = tf.extractfile(member)
                    if src:
                        content = src.read()
                        local_content = open(dest, "rb").read()
                        if content == local_content:
                            # İçerik AYNI — yeniden yazmaya gerek yok (hızlı yol)
                            continue
                        # Çakışma — yerel korunur, kopya .conflict.TS ile saklanır
                        conflict = _write_conflict(dest, src_machine)
                        log.warning(f"Çakışma: {name} → {conflict}")
                        continue  # yereli koru, uzak yazılmaz
                # Güvenli yaz
                dest_dir = os.path.dirname(dest)
                os.makedirs(dest_dir, exist_ok=True)
                src = tf.extractfile(member)
                if src:
                    with open(dest, "wb") as f:
                        f.write(src.read())
        log.info(f"{node}: GDrive {latest} çekildi (çakışma korumalı)")
        return True
    except Exception as e:
        log.error(f"{node}: paket açılamadı: {e}")
        return False


def verify_build(cfg):
    """Kernel pull sonrası build doğrulama — Cumulus kritik.
    PIPE BUG: 'make | tail' make RC'sini yutuyor → çıktı dosyaya yaz,
    RC doğrudan make'ten alınır."""
    kernel_cfg = cfg["dirs"].get("kernel")
    if not kernel_cfg:
        return True
    paths = kernel_cfg.get("paths") or [kernel_cfg.get("path", "")]
    for p in paths:
        pexp = os.path.expanduser(p)
        if not os.path.isdir(pexp):
            continue
        # Makefile var mı?
        if not os.path.exists(os.path.join(pexp, "Makefile")):
            continue
        log.info(f"Build doğrulama: {pexp}")
        # RC'yi doğrudan make'ten al — pipe YOK
        out, rc = run_cmd(
            f'cd "{pexp}" && make clean >/dev/null 2>&1 && '
            f'make >/tmp/sync_motor_build.log 2>&1; echo "RC=$?"',
            timeout=300, shell=True)
        # Son satırda RC=... var
        rc_line = [l for l in out.splitlines() if l.startswith("RC=")]
        build_rc = int(rc_line[-1].split("=")[1]) if rc_line else -1
        if build_rc != 0:
            log.error(f"Build BAŞARISIZ: {pexp} (RC={build_rc})")
            tail = "\n".join(out.splitlines()[-5:])
            log.error(f"Son çıktı: {tail[:200]}")
            return False
        log.info(f"Build PASS: {pexp}")
        break  # İlk geçerli dizin yeterli
    return True


def cmd_pull(cfg):
    print(f"\n  🔄 PULL — {cfg['machine']}")
    remote = gh_fetch_manifest(cfg)
    if not remote:
        log.error("Uzak manifest alınamadı")
        return

    local = scan_all(cfg)
    rfiles = remote.get("files", {})

    # GÜVENLİK (26 Ağu, OceanAPI #4): path traversal koruması
    # Manifest'ten gelen yollar güvenilmez — kök dışına çıkış REDDEDİLİR
    safe_dirs = set()
    for node, dc in (cfg.get("dirs") or {}).items():
        for p in (dc.get("paths") or [dc.get("path")] if dc else []):
            if p:
                safe_dirs.add(os.path.realpath(os.path.expanduser(p)))

    def _is_safe_path(relpath):
        if relpath.startswith(("/", "\\")) or ".." in relpath.split("/"):
            return False
        return True

    to_pull = []
    conflicts = []
    for path, rinfo in rfiles.items():
        if not _is_safe_path(path):
            log.warning(f"GÜVENLİK: tehlikeli yol reddedildi — {path}")
            continue
        if path not in local:
            to_pull.append(path)
        elif local[path].get("sha") != rinfo.get("sha"):
            lmachine = local[path].get("machine", "?")
            rmachine = rinfo.get("machine", "?")
            if lmachine != rmachine:
                conflicts.append(path)
            else:
                to_pull.append(path)

    if not to_pull and not conflicts:
        log.info("Güncel — çekilecek yok")
    else:
        log.info(f"Pull: {len(to_pull)} yeni, {len(conflicts)} çakışma tespiti")

    print("\n  Manifest durumu:")
    for p in to_pull[:15]:
        print(f"    🔄 {p}")
    for p in conflicts[:15]:
        print(f"    ⚠️ ÇAKIŞMA: {p} (yerel korunacak)")
    if len(to_pull) + len(conflicts) > 15:
        print(f"    ... +{len(to_pull)+len(conflicts)-15} daha")

    # Gerçek dosya transferi: kernel için git pull öner
    kernel_cfg = cfg["dirs"].get("kernel", {})
    kernel_paths = kernel_cfg.get("paths") or [kernel_cfg.get("path", "")]
    if any(os.path.isdir(os.path.expanduser(p)) for p in kernel_paths):
        log.info("Kernel dosyaları için: git pull "
                 "(repo: yolgezerahmet/cumulusos)")

    # GDrive'dan en son versiyonları çek (non-destructive)
    log.info("GDrive versiyon çekme...")
    pulled = 0
    for label in cfg["dirs"]:
        if cfg["dirs"][label].get("gdrive", False):
            if gdrive_pull_latest(cfg, label):
                pulled += 1
    if pulled:
        log.info(f"GDrive: {pulled} node güncellendi")

    # Manifest'e pull zamanı yaz
    mf = load_manifest(cfg)
    mf["last_pull"] = datetime.now().isoformat()
    mf["remote_known"] = remote.get("last_sync")
    save_manifest(cfg, mf)

    # Kernel pull sonrası build doğrulama (Cumulus kritik)
    if pulled or to_pull:
        log.info("Build doğrulama...")
        verify_build(cfg)


def cmd_conflicts(cfg):
    print("\n  ⚠️ ÇAKIŞMA DOSYALARI")
    found = list_conflicts(cfg)
    if not found:
        print("    Çakışma yok ✅")
        return
    for f in found:
        sz = os.path.getsize(f)
        print(f"    {f} ({sz//1024}KB)")
    print(f"\n  {len(found)} çakışma — .conflict.TS korunuyor, "
          f"otomatik çözüm: incele ve manuel birleştir")


def cmd_probe(cfg):
    """Yerel kaynakları ve kurulu araçları ölç; manifest'e yaz (push ile paylaşılır)."""
    res = resource_probe()
    tools = scan_tools(cfg)
    mf = load_manifest(cfg)
    mf["resources"] = res
    mf["tools_state"] = tools
    mf["probe_time"] = datetime.now().isoformat()
    save_manifest(cfg, mf)

    print("\n  🧭 PROBE — Yerel Kaynaklar ve Araçlar\n")
    gpu_line = str(res["gpu_name"]) if res["gpu"] else "YOK"
    print(f"  CPU : {res['cpus']} çekirdek")
    print(f"  RAM : {res['ram_gb']:.1f} GB")
    print(f"  DISK: {res['disk_gb']:.1f} GB boş")
    nvidia_tag = " | NVIDIA VAR" if res.get("nvidia") else " | NVIDIA YOK"
    print(f"  GPU : {gpu_line}{nvidia_tag}")
    print("\n  Kurulu araçlar (katalog):")
    for name, t in sorted(tools.items()):
        mark = "✅" if t["installed"] else "⬜"
        gpu_tag = " [GPU]" if t["gpu"] else ""
        print(f"    {mark} {name}{gpu_tag}")
    print("\n  Manifest'e yazıldı — 'push' ile karşı node'a gider.")
    return 0


def cmd_propose(cfg):
    """Karşı node'un kurulu araçlarını KAYNAK KONTROLLÜ öneri olarak sun.

    GPU öncelikli: GPU gerektiren araçlar listede öne alınır; GPU'suz
    makinede GPU zorunlu araçlar 'kaynak engelli' bölümüne düşer.
    Kurulum burada HİÇBİR ŞEY yapmaz — sadece öneri listesi basar.
    """
    mf = load_manifest(cfg)
    remote_tools = mf.get("tools_state", {})
    remote_res = mf.get("resources", {})
    if not remote_tools:
        print("\n  💡 ÖNERİ — uzak node manifest'inde araç durumu YOK.\n"
              "  Karşı node'da önce 'probe' + 'push', sonra burada 'pull' "
              "çalıştırın.")
        return 0
    local_res = resource_probe()
    local_tools = scan_tools(cfg)

    print("\n  💡 ÖNERİ — Kaynak Kontrollü Kurulum Önerileri\n")
    print(f"  Bu makine : CPU {local_res['cpus']} | RAM {local_res['ram_gb']:.1f}GB "
          f"| DISK {local_res['disk_gb']:.1f}GB | "
          f"GPU {'VAR' if local_res['gpu'] else 'YOK'}")
    print(f"  Kaynak node: CPU {remote_res.get('cpus','?')} | "
          f"RAM {remote_res.get('ram_gb','?')}GB | "
          f"GPU {'VAR' if remote_res.get('gpu') else 'YOK'}")

    results = []
    for name, t in cfg.get("tools", {}).items():
        status, msg = propose_install(name, t, local_res, local_tools,
                                      remote_tools)
        if status in ("ALREADY", "NOT_ON_SOURCE"):
            continue
        results.append((name, status, msg, bool(t.get("gpu"))))

    # GPU öncelikli sıralama: INSTALLABLE önce (GPU olanlar içinde ilk),
    # sonra kaynak engelliler; aynı grup içinde GPU olanlar öne gelir.
    pri = {"INSTALLABLE": 0, "GPU_MISSING": 1, "DISK_INSUFFICIENT": 2,
           "RAM_INSUFFICIENT": 3, "CPU_INSUFFICIENT": 4}
    results.sort(key=lambda r: (pri.get(r[1], 5), 0 if r[3] else 1, r[0]))

    installable = [r for r in results if r[1] == "INSTALLABLE"]
    blocked = [r for r in results if r[1] != "INSTALLABLE"]

    print("\n  ✅ KURULABİLİR (kaynaklar yeterli):")
    if not installable:
        print("    (yok — karşı node'da kurulu ekstra araç bulunmuyor)")
    for name, status, msg, gpu in installable:
        tag = " [GPU-ÖNCELİKLİ]" if gpu else ""
        print(f"    ▶ {name}{tag}")
        print(f"      {cfg['tools'][name].get('desc','-')}")
        print(f"      kur: {cfg['tools'][name].get('install','(tanımsız)')}")
    print("\n  ⚠️  KAYNAK ENGELLİ (bu makinede önerilmez — neden):")
    if not blocked:
        print("    (yok)")
    reason = {"GPU_MISSING": "GPU yok", "DISK_INSUFFICIENT": "disk yetmez",
              "RAM_INSUFFICIENT": "RAM yetmez",
              "CPU_INSUFFICIENT": "CPU yetmez"}
    for name, status, msg, gpu in blocked:
        print(f"    ⛔ {name} [{reason.get(status, status)}]: {msg}")
    print("\n  Kurmak için: sync_motor.py apply --tool <ad> [--yes]")
    return 0


def cmd_apply(cfg, tool_name, yes=False):
    """Önerilen bir aracı KULLANICI ONAYI SONRASI kur. Non-destructive.

    Kurallar:
      - zaten kuruluysa RED (üzerine asla yazma)
      - kaynak yetersizse RED (GPU yok / disk / RAM / CPU)
      - --yes yoksa interaktif onay istenir; onay yoksa HİÇBİR ŞEY çalışmaz
      - kurulum komutu config'ten gelir (kullanıcının kendi kataloğu)
    """
    tools = cfg.get("tools", {})
    if tool_name not in tools:
        print(f"\n  ❌ Bilinmeyen araç: {tool_name} — katalog: {list(tools)}")
        return 1
    t = tools[tool_name]

    # 1) Zaten kurulu mu? → RED (non-destructive garantisi)
    if tool_installed(t):
        print(f"\n  ⛔ {tool_name} ZATEN KURULU — kurulum reddedildi "
              "(üzerine yazılmaz).")
        return 1

    # 2) Kaynak kontrolü — uzak durum gerekmez: bu makine yeterli mi?
    res = resource_probe()
    local_tools = scan_tools(cfg)
    fake_remote = {tool_name: {"installed": True}}
    status, msg = propose_install(tool_name, t, res, local_tools, fake_remote)
    if status in ("GPU_MISSING", "DISK_INSUFFICIENT", "RAM_INSUFFICIENT",
                  "CPU_INSUFFICIENT"):
        print(f"\n  ⛔ {tool_name} kurulamaz — {msg}")
        return 1

    # 3) Komutu göster, onay al
    inst = t.get("install", "")
    print(f"\n  🔧 KURULUM: {tool_name}")
    print(f"    açıklama   : {t.get('desc','-')}")
    print(f"    gereksinim : {t.get('min_ram_gb',0)}GB RAM | "
          f"{t.get('min_disk_gb',0)}GB disk | "
          f"CPU {t.get('min_cpus',1)}+ | "
          f"GPU={'gerekli' if t.get('gpu') else 'gerekmez'}")
    print(f"    komut      : {inst}")
    if not inst:
        print("  ❌ install komutu tanımsız — kurulum yapılamaz.")
        return 1
    if not yes:
        try:
            ans = input("  Onaylıyor musunuz? [e/Evet / h/Hayır] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("e", "evet", "y", "yes"):
            print("  Kurulum İPTAL — hiçbir şey değiştirilmedi (non-destructive).")
            return 1
    print("  Kurulum başlıyor...")
    out, rc = run_cmd(inst, timeout=600, shell=True)
    if rc != 0:
        print(f"  ❌ Kurulum BAŞARISIZ (rc={rc}):\n    {str(out)[-400:]}")
        return 1
    print(f"  ✅ {tool_name} kuruldu.\n    {str(out)[-200:]}")
    cmd_probe(cfg)   # durumu tazele (manifest'e yazar)
    return 0


def cmd_doctor(cfg):
    """Ortam sağlık kontrolü: bağımlılıklar + yapılandırma + bağlantılar (v1.4)."""
    import shutil
    ok = True
    print("\n  🩺 DOCTOR — Ortam Sağlık Kontrolü\n")

    # 1. Bağımlılıklar
    for tool in ("rclone", "git", "gh", "python3"):
        found = shutil.which(tool) is not None
        print(f"  {'✅' if found else '❌'} {tool}: {'mevcut' if found else 'YOK'}")
        if tool in ("rclone", "git") and not found:
            ok = False

    # 2. Yapılandırma — node dizinleri
    dirs = cfg.get("dirs", {})
    print(f"\n  📁 Node'lar ({len(dirs)}):")
    for name, d in dirs.items():
        path = d.get("path", "")
        paths = d.get("paths") or []
        targets = ([path] if path else []) + list(paths)
        if not targets:
            print(f"  ❌ {name}: yol tanımsız")
            ok = False
            continue
        for t in targets:
            exists = os.path.isdir(os.path.expanduser(t))
            print(f"  {'✅' if exists else '❌'} {name}: {t}")
            if not exists:
                ok = False

    # 3. GDrive remote
    out, rc = run_cmd("rclone listremotes", timeout=30, retries=1)
    # rc==0 şartı (QCode denetimi #2): yarıda kesilen rclone stdout'a yazsa
    # bile 'tanımlı' denmez — fail-closed tanı.
    gdrive = rc == 0 and "gdrive:" in (out or "")
    print(f"\n  {'✅' if gdrive else '❌'} GDrive remote (rclone): "
          f"{'tanımlı' if gdrive else 'YOK'}")
    if not gdrive:
        ok = False

    # 4. GitHub repo erişimi
    repo = cfg.get("github", {}).get("repo", "")
    if repo:
        if not repo.startswith(("http://", "https://", "git@")):
            repo_url = f"https://github.com/{repo}.git"
        else:
            repo_url = repo
        out2, rc2 = run_cmd(f"git ls-remote {repo_url} HEAD", timeout=30, shell=True)
        print(f"  {'✅' if rc2 == 0 else '❌'} GitHub repo: {repo}")
        if rc2 != 0:
            ok = False
        # gh auth durumu (private repo için)
        gh_out, gh_rc = run_cmd("gh auth status", timeout=15, shell=True)
        gh_ok = gh_rc == 0
        print(f"  {'✅' if gh_ok else '⚠️'} gh auth: "
              f"{'oturum açık' if gh_ok else 'yok — private repo için gerekli'}")

    print(f"\n  SONUÇ: {'✅ SAĞLIKLI' if ok else '❌ SORUN VAR'}")
    return 0 if ok else 1


def cmd_init(cfg):
    print("\n  🚀 INIT — İlk Kurulum")
    if gh_available() and gh_ensure_repo(cfg):
        new, changed, deleted, local = detect_changes(cfg)
        mf = load_manifest(cfg)
        mf["files"] = local
        mf["last_sync"] = datetime.now().isoformat()
        mf["machine"] = cfg["machine"]
        save_manifest(cfg, mf)
        gh_push_manifest(cfg, mf)
        log.info(f"İlk manifest: {len(local)} dosya")
    else:
        log.warning("gh CLI yok veya repo hazırlanamadı — "
                    "manifest yerel tutulacak")
    gdrive_snapshot(cfg)
    log.info("Kurulum tamam — 'python3 sync_motor.py both' ile kullan")


# ═══════════════════════════════════════════════════════════════
# ANA
# ═══════════════════════════════════════════════════════════════


def node_fingerprint(cfg, node):
    """İçerik parmak izi: (relpath, boyut, mtime) özeti — delta push için."""
    import hashlib
    base = cfg["dirs"][node]["path"] if isinstance(cfg["dirs"][node], dict) else cfg["dirs"][node]
    h = hashlib.sha256()
    items = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            if any(sec in f for sec in (".env", ".key", ".pem")):
                continue
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
                items.append((os.path.relpath(p, base), st.st_size, int(st.st_mtime)))
            except OSError:
                pass
    items.sort()
    for it in items:
        h.update(str(it).encode())
    return h.hexdigest()

def _state_path(cfg):
    return os.path.join(cfg["state"]["dir"], "last_push.json")

def load_last_push(cfg):
    try:
        return json.load(open(_state_path(cfg), encoding="utf-8", errors="replace"))
    except Exception:
        return {}

def save_last_push(cfg, node, fp):
    d = load_last_push(cfg)
    d[node] = fp
    os.makedirs(cfg["state"]["dir"], exist_ok=True)
    json.dump(d, open(_state_path(cfg), "w", encoding="utf-8"), ensure_ascii=False)

def run_with_retry(fn, *a, retries=1, **kw):
    """Geçici ağ hatalarında 1 retry — otonom dayanıklılık."""
    for i in range(retries + 1):
        try:
            return fn(*a, **kw)
        except Exception as e:
            if i < retries and "Errno" in str(e) or "timeout" in str(e).lower():
                print(f"    ⏳ geçici hata ({e}) — retry {i+1}/{retries}")
                time.sleep(5)
            else:
                raise

def cmd_agent_status(cfg):
    """Hermes agent/otonom cron için JSON durum + öneri."""
    import json as _j
    conflicts = list_conflicts(cfg)
    fp_nodes = load_last_push(cfg)
    status = {
        "motor": f"sync_motor v{__version__}",
        "machine": cfg["machine"],
        "conflicts": len(conflicts),
        "conflict_files": conflicts[:10],
        "nodes": list(cfg["dirs"].keys()),
        "tracked_push": list(fp_nodes.keys()),
        "son_kosu": last_run_summary(),
    }
    # sağlık: çakışma / son push bilgisi / çözüm önerisi
    rec = []
    if conflicts:
        rec.append(f"ÇÖZ: {len(conflicts)} çakışma dosyası — incele + manuel birleştir (.conflict.TS korunur)")
    if not fp_nodes:
        rec.append("PUSH: henüz push yok — 'sync_motor.py both' çalıştır (GDrive hub karşılıklı)")
    else:
        rec.append("OK: push takibi aktif")
    lr = status["son_kosu"]
    if lr and lr.get("rc", 0) != 0:
        rec.append(f"SON KOŞU HATALI: {lr.get('komut')} rc={lr.get('rc')} @ {lr.get('ts','?')[:19]}")
    status["recommendation"] = " | ".join(rec) if rec else "OK — eylem gerekmiyor"
    print(_j.dumps(status, ensure_ascii=False, indent=2))


def last_run_summary():
    """~/.hermes/state/sync_last_run.json'dan son mutating koşu özeti."""
    try:
        if not os.path.exists(RUN_STATE):
            return None
        hist = json.load(open(RUN_STATE, encoding="utf-8", errors="replace")).get("history", [])
        if not hist:
            return None
        last = hist[-1]
        return {
            "ts": last.get("ts"),
            "komut": last.get("komut"),
            "rc": last.get("rc"),
            "node": last.get("node"),
            "machine": last.get("machine"),
        }
    except Exception:
        return None


# ── v1.6.3: GDrive VERSİYON TAKİPLİ YEDEK + LİSTE + GERİ ALMA ──

GDRIVE_VERS = "gdrive:cumulusos-backups/versiyonlu"

def _hub_base(args_hub=None):
    return args_hub or GDRIVE_VERS


# ── v1.6.4: TEK-INSTANCE KİLİT + SON-KOŞU RAPORU ──

def _motor_lock_path() -> str:
    """Kilit dosyası yolu — platform farkındalıklı (v2.1.1).

    Linux/macOS: /tmp/cumulus_sync.lock (eski davranış korunur).
    Windows: %TEMP%\\cumulus_sync.lock ('/tmp' yok).
    """
    if os.name == "nt":
        base = os.environ.get("TEMP") or os.environ.get("TMP") or os.path.expanduser("~")
        return os.path.join(base, "cumulus_sync.lock")
    return "/tmp/cumulus_sync.lock"


MOTOR_LOCK = _motor_lock_path()
RUN_STATE = os.path.expanduser("~/.hermes/state/sync_last_run.json")

# Bu komutlar GDrive/GitHub'a YAZAR → kilit zorunlu. Okuma komutları
# (status/conflicts/versions/agent-status/nodes/doctor) kilitsiz çalışır.
MUTATING_CMDS = {"push", "pull", "both", "backup", "rollback",
                 "init", "add-node", "share", "apply", "memory"}

def acquire_lock():
    """Aynı anda yalnız bir sync işlemi GDrive/GitHub'a yazsın.

    Linux: fcntl.flock(LOCK_EX|LOCK_NB) — ikinci koşu anında RED.
    Windows: msvcrt.locking — dosyanın ilk baytını kilitler.
    Dönüş: fd (kilit sahibi) veya None (başka sync aktif).
    """
    try:
        fd = open(MOTOR_LOCK, "w", encoding="utf-8")
    except OSError:
        return None
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:
            fd.seek(0)
            fd.write("\0")
            fd.flush()
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            fd.seek(0)
        else:
            # kilit desteği yok — yalnızca pid dosyası (en iyi çaba)
            if os.path.exists(MOTOR_LOCK) and os.path.getsize(MOTOR_LOCK) > 0:
                pid = open(MOTOR_LOCK, encoding="utf-8", errors="replace").read().split()[0]
                if pid.isdigit() and os.path.exists(f"/proc/{pid}"):
                    return None
        fd.seek(0, 2)
        fd.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
        fd.flush()
        return fd
    except OSError:
        try:
            fd.close()
        except Exception:
            pass
        return None

def record_run(cfg, komut, rc, node=None, extra=None):
    """Son koşu kaydı — web paneli/agent-status 'herhangi node üzerinden' okur."""
    try:
        hist = []
        if os.path.exists(RUN_STATE):
            try:
                hist = json.load(open(RUN_STATE, encoding="utf-8", errors="replace")).get("history", [])
            except Exception:
                hist = []
        hist.append({
            "ts": datetime.now().isoformat(),
            "komut": komut,
            "rc": rc,
            "node": node,
            "machine": cfg.get("machine"),
            "extra": extra or {},
        })
        hist = hist[-50:]  # son 50 koşu
        os.makedirs(os.path.dirname(RUN_STATE), exist_ok=True)
        json.dump({"history": hist}, open(RUN_STATE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠ son-koşu raporu yazılamadı: {e}")

def _tar_node(cfg, node, outdir, ts):
    """Node'u tar.gz yap → (tar_path, sha).

    H1 28 Ağu FIX + H2 29 Ağu Windows birleşik sürümü:
    - include: virgüllü glob (string) VEYA liste — dosya adı + relpath eşleşir
    - max_kb: tar öncesi kaba boyut kontrolü (×8 marj, tar sonrası kesin kontrol)
    - exclude_dirs: dizin adı budaması
    - sır filtre: .env/.key/.pem/.secret adları atlanır
    - isdir: Windows Türkçe Unicode yollarda os.path.exists False dönebilir
      (örn. "Cumulus Patent Dosyaları") — isdir/pathlib güvenilir.
    """
    import hashlib, fnmatch
    src = cfg["dirs"][node]
    base = src["path"] if isinstance(src, dict) else src
    include = (src.get("include") if isinstance(src, dict) else None)
    max_kb = (src.get("max_kb") if isinstance(src, dict) else None)
    if not os.path.isdir(base):
        return None, None
        return None, None
    include = src.get("include", ["*"]) if isinstance(src, dict) else ["*"]
    exclude_dirs = set(
        d.lower().strip("*/") for d in (src.get("exclude_dirs", []) if isinstance(src, dict) else [])
    )
    tarp = os.path.join(outdir, f"{node}_{ts}.tar.gz")
    if include is None:
        pats = []
    elif isinstance(include, str):
        pats = [x.strip() for x in include.split(",") if x.strip()]
    else:
        pats = [str(x).strip() for x in include if str(x).strip()]
    base_parent = os.path.dirname(base.rstrip("/")) or base
    # 28 Ağu v2: max_kb ÖN KONTROL — tar oluşturmadan boyutu hesapla, aşarsa atla
    # (önceden tar oluşturulup sonra siliniyordu: pcb 665MB/research 971MB her koşuda israf)
    if max_kb:
        _total = 0
        for _root, _dirs, _files in os.walk(base):
            _dirs[:] = [d for d in _dirs if d != ".git"]
            for _f in _files:
                if any(_sec in _f for _sec in (".env", ".key", ".pem")):
                    continue
                _p = os.path.join(_root, _f)
                _rel = os.path.relpath(_p, base_parent)
                if pats and not any(fnmatch.fnmatch(_rel, pat) or fnmatch.fnmatch(_f, pat) for pat in pats):
                    continue
                try:
                    _total += os.path.getsize(_p)
                except OSError:
                    pass
        # Ön kontrol = KABA ELEME (sıkıştırılmamış boyut, max_kb×8 marjı):
        # tar.gz metin/kod için ~10× sıkıştırır; hermes (config+state 2.4MB→631KB tar)
        # gibi sıkışabilir node'lar yanlış atlanmasın. Kesin kontrol tar sonrası yapılır.
        if _total // 1024 > int(max_kb) * 8:
            print(f"    ⚠ {node}: boyut {_total//1024}KB > max_kb {max_kb}KB×8 — atlandı (ön kontrol, tar oluşturulmadı)")
            return None, None
    with tarfile.open(tarp, "w:gz") as tar:
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d.lower() not in exclude_dirs and d != ".git"]
            for f in files:
                if any(sec in f.lower() for sec in (".env", ".key", ".pem", ".secret")):
                    continue
                if not any(fnmatch.fnmatch(f, p) for p in include):
                    continue
                p = os.path.join(root, f)
                rel = os.path.relpath(p, base_parent)
                if pats:
                    if not any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(f, pat) for pat in pats):
                        continue
                try:
                    tar.add(p, arcname=rel)
                except OSError as e:
                    print(f"    ⚠ atlandı (canlı dosya): {p} ({e})")
    size_kb = os.path.getsize(tarp) // 1024
    if max_kb and size_kb > int(max_kb):
        os.remove(tarp)
        print(f"    ⚠ {node}: tar {size_kb}KB > max_kb {max_kb}KB — atlandı (küçük node kuralı; hermes-full cron kapsar)")
        return None, None
    h = hashlib.sha256()
    with open(tarp, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return tarp, h.hexdigest()

RESTIC_REPO_URL = os.environ.get("RESTIC_REPO_URL", "rest:http://127.0.0.1:8443/")
RESTIC_PASS_ENV = "RESTIC_PASSWORD"

def _restic(args, timeout=3600, cwd=None):
    """restic CLI sarmalayıcı — env'den repo+şifre (.env'den okur)."""
    env = dict(os.environ)
    # ~/.hermes/.env'den RESTIC_* değerlerini yükle (cron ortamında .env export edilmeyebilir)
    try:
        with open(os.path.expanduser("~/.hermes/.env"), encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("RESTIC_") and "=" in line:
                    k, v = line.split("=", 1)
                    env.setdefault(k.strip(), v.strip())
    except Exception:
        pass
    if not env.get("RESTIC_REPOSITORY"):
        env["RESTIC_REPOSITORY"] = RESTIC_REPO_URL
    if not env.get(RESTIC_PASS_ENV):
        print("    ❌ RESTIC_PASSWORD .env'de yok — restic çalışmaz")
        return None, "RESTIC_PASSWORD yok"
    # restic binary yolunu bul (PATH + bilinen konumlar; Windows/Linux/H3)
    # 29 Ağu 2026 FIX (H2/Windows): expanduser("~/bin/restic") uzantısız döner,
    # Windows'ta dosya adı restic.exe olduğu için os.path.exists() False veriyordu.
    # Her adayın .exe varyantı da denenir.
    rbin = shutil.which("restic") or shutil.which("restic.exe")
    if not rbin:
        for cand in ("/usr/local/bin/restic", "/usr/bin/restic",
                     os.path.expanduser("~/bin/restic"),
                     os.path.expanduser("~/bin/restic.exe"),
                     os.path.expanduser("~/AppData/Local/restic/restic.exe"),
                     os.path.expanduser("~/AppData/Local/Microsoft/WinGet/Links/restic.exe")):
            if os.path.exists(cand):
                rbin = cand
                break
    if not rbin:
        print("    ❌ restic binary bulunamadı (PATH'te yok)")
        return None, "restic binary yok"
    r = subprocess.run([rbin, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       timeout=timeout, env=env, cwd=cwd)
    return r.returncode, r.stdout + r.stderr

# ─── AKILLI KANAL SEÇİCİ (v2.1, 29 Ağu 2026) ───────────────────────────
# Üç taşıyıcı: A2A (ajanlar arası görev/cevap, Tailscale HTTP) ·
# Syncthing (P2P dosya senkron, GDrive'suz) · GDrive (arşiv/versiyonlu).
# Seçim kuralı: görev/cevap → A2A; dosya değişikliği → Syncthing; arşiv → GDrive.
TRANSPORT_A2A = "a2a"
TRANSPORT_SYNCTHING = "syncthing"
TRANSPORT_GDRIVE = "gdrive"

A2A_NODES = {  # makine → Tailscale IP (a2a_cli hedefi)
    "H1": "100.92.2.47",
    "h3": "100.103.44.107",
    "hermesagent03": "100.103.44.107",
    "h2": "100.76.82.46",
    "sistemg16": "100.76.82.46",
}

def smart_transport(kind: str, target: str = ""):
    """Kanal seç — kind: task|file|archive."""
    if kind == "task":
        return TRANSPORT_A2A
    if kind == "file":
        return TRANSPORT_SYNCTHING
    return TRANSPORT_GDRIVE

def cmd_mesh(cfg, aksiyon, hedef="", gorev="", token="", dry_run=False):
    """sync_motor.py mesh send|status <hedef> <görev> — A2A üzerinden ajan konuşması."""
    if not token:
        # .env'den A2A_TOKEN oku (cron ortamı export etmeyebilir)
        try:
            with open(os.path.expanduser("~/.hermes/.env")) as _f:
                for _line in _f:
                    if _line.startswith("A2A_TOKEN="):
                        token = _line.split("=", 1)[1].strip()
                        break
        except Exception:
            pass
    if aksiyon == "status":
        for name, ip in A2A_NODES.items():
            out, rc = run_cmd(["python3", "/root/.hermes/scripts/a2a_cli.py",
                               "send-status", ip, "--token", token or os.environ.get("A2A_TOKEN", "")],
                              timeout=60)
            txt = out if isinstance(out, str) else (json.dumps(out, ensure_ascii=False) if not isinstance(out, (list, tuple)) else "\n".join(str(x) for x in out))
            try:
                d = json.loads(txt)
                r = d.get("result", {}).get("result", {})
                ozet = f"host={r.get('host','?')} disk={r.get('disk_gb','?')}GB"
            except Exception:
                ozet = str(txt).strip().splitlines()[-1] if str(txt).strip() else "erişilemedi"
            print(f"  {name:14s} ({ip}): {ozet}")
        return 0
    if aksiyon == "send":
        ip = A2A_NODES.get(hedef, hedef)
        out, rc = run_cmd(["python3", "/root/.hermes/scripts/a2a_cli.py",
                           "send", ip, gorev, "--token", token or os.environ.get("A2A_TOKEN", "")],
                          timeout=120)
        print(out.strip()[-400:] if out.strip() else "(çıktı yok)")
        return rc
    print("Kullanım: mesh send|status [hedef] [görev]")
    return 1

def cmd_restic_backup(cfg, node=None, dry_run=False):
    """Restic incremental backup (v2.0 — B modülü, 28 Ağu 2026).

    rclone serve restic gdrive:restic-backup --addr 127.0.0.1:8443
    (systemd servisi olarak çalışır; GDrive = object store, CDC dedup,
    çoklu makine aynı repo → cross-system dedup).

    Her node: tag:node + exclude .git/.env/.key/.pem.
    Retention: restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6.
    """
    nodes = [node] if node else list(cfg["dirs"].keys())
    print(f"\n  💾 RESTIC INCREMENTAL YEDEK — {RESTIC_REPO_URL}")
    # hermes node'u restic'ten HARİÇ: /root/.hermes (22GB+) exclude_dirs'la bile
    # büyük; hermes-full node'u ayrı zstd script ile GDrive'a gidiyor (hermes_full_backup.py).
    # include filtreleri restic'e yansımıyor — bu yüzden tam dizin yüklenirdi (H2 bulgusu).
    skip_nodes = {k for k, v in cfg["dirs"].items()
                  if isinstance(v, dict) and v.get("restic") is False}
    nodes = [n for n in nodes if n not in skip_nodes]
    for n in nodes:
        dst = cfg["dirs"][n]
        base = dst["path"] if isinstance(dst, dict) else dst
        if not os.path.exists(base):
            print(f"    ⚠ {n}: kaynak yok — atlandı"); continue
        if dry_run:
            print(f"    [DRY] {n}: restic backup {base} (tag:{n})")
            continue
        # Sabit güvenlik/gürültü dışlamaları
        exc = ["--exclude", ".git", "--exclude", ".env",
               "--exclude", "*.key", "--exclude", "*.pem",
               "--exclude", "*.pyc", "--exclude", "node_modules"]
        # 29 Ağu 2026 FIX (H2): config'teki exclude_dirs/max_size_kb YOKSAYILIYORDU.
        # Kanıt: hermes node'u = AppData/Local/hermes = 13 GB; config backups(5GB) ve
        # hermes-agent(3.9GB) dizinlerini dışlıyor ama restic hepsini yüklüyordu
        # (34 KiB/s GDrive'da ~2-4 gün). Artık config niyeti restic'e aktarılır.
        if isinstance(dst, dict):
            seen = {".git", "node_modules"}
            for d in (dst.get("exclude_dirs") or []):
                if d and d not in seen:
                    seen.add(d)
                    exc += ["--exclude", d]
            mkb = dst.get("max_size_kb")
            if mkb:
                exc += ["--exclude-larger-than", f"{int(mkb)}k"]
        rc, out = _restic(["backup", base, "--tag", n] + exc)
        if rc == 0:
            # özet satırlarını göster
            for line in out.splitlines():
                if line.startswith(("Files:", "Added to", "snapshot", "processed")):
                    print(f"    ✅ {n}: {line.strip()}")
        else:
            print(f"    ❌ {n}: {out.strip()[-200:]}")
    # retention (tüm repo) — SADECE birincil makinede (H2 bulgusu: üç makine
    # eşzamanlı prune aynı repo'yu kilitler; repo bozulabilir)
    ret_machine = os.environ.get("SYNC_RETENTION_MACHINE") or cfg.get("retention_machine", "")
    this_machine = cfg.get("machine", "")
    if not dry_run and (not ret_machine or this_machine == ret_machine):
        # forget her koşu (hızlı — snapshot siler); prune SADECE 04:00-05:00 arası
        # (--prune tüm repo'yu GC'ler, 55 snapshot'ta dakikalar sürer; her koşuda
        # yapılırsa H1 backup cron'u uzar ve diğer sync'ler kilit yüzünden atlanır)
        args = ["forget", "--keep-daily", "7", "--keep-weekly", "4",
                "--keep-monthly", "6", "--retry-lock", "5m"]
        _h = time.localtime().tm_hour
        if _h in (4,):
            args += ["--prune"]
        rc, out = _restic(args)
        print(f"    🧹 retention: {'OK' if rc == 0 else out.strip()[-150:]}" + ("" if "--prune" in args else " (prune 04:00'de)"))
    elif not dry_run:
        print(f"    🧹 retention: atlandı (bu makine yedekliyor, prune {ret_machine} yapar)")

def _ck_import():
    """sync_common_knowledge'i import et (ortak akıl)."""
    import importlib.util
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_common_knowledge.py")
    spec = importlib.util.spec_from_file_location("sck", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def cmd_discover(cfg, token="", dry_run=False):
    """OTOMATİK KEŞİF (v2.2): state.json'dan canlı node'ları bul + A2A durumu.

    Ortak durum dosyasına kim yazdıysa o ağda — sabit liste yerine
    state.json tek gerçek kaynak. Her makine bloğundan A2A sağlık sorgusu.
    """
    try:
        ck = _ck_import()
        state = ck.read_state()
    except Exception as e:
        print(f"  ⚠ ortak durum okunamadı: {e}")
        return 1
    blocks = state.get("nodes", {})
    print(f"\n  🔍 OTOMATİK KEŞİF — {len(blocks)} canlı makine (state.json)")
    A2A_NODES.update({})  # keşif sonrası dinamik eşleme
    for n in sorted(blocks):
        blk = blocks[n]
        ip = A2A_NODES.get(n, "")
        durum = "state'te var"
        if ip:
            out, rc = run_cmd(["python3", "/root/.hermes/scripts/a2a_cli.py",
                               "ping", ip, "--token", token or os.environ.get("A2A_TOKEN", "")],
                              timeout=30)
            durum = "A2A canlı" if ("ok" in str(out)) else f"A2A erişilemedi ({str(out)[:30]})"
        print(f"  • {n:22s} hlc={blk.get('hlc','-')[:20]} {durum}")
    return 0

def cmd_task(cfg, aksiyon, task_id="", title="", token="", dry_run=False):
    """ORTAK GÖREV DAĞITIMI (v2.2): add/list/claim/done — sync_common_knowledge."""
    try:
        ck = _ck_import()
    except Exception as e:
        print(f"  ⚠ sync_common_knowledge yüklenemedi: {e}")
        return 1
    if aksiyon == "list":
        tasks = ck.list_tasks()
        print(f"\n  📋 ORTAK GÖREVLER — {len(tasks)}")
        for t in tasks:
            print(f"    [{t.get('status','?'):7}] {t.get('task_id')} — {t.get('title','')} (owner={t.get('owner','-')})")
        return 0
    if aksiyon == "add":
        if not task_id or not title:
            print("Kullanım: task add --task-id <id> --path '<başlık>'")
            return 1
        t = ck.create_task(task_id, title)
        print(f"  ➕ task: {t['task_id']} ({t['status']})")
        return 0
    if aksiyon == "claim":
        # owner = state.json node adı (küçük harf) — "H1" state'te yok
        owner = os.environ.get("SYNC_NODE_NAME") or os.uname().nodename.lower()
        # FAILOVER: allow_stale=True — sahibi düştüyse/yaşlıysa devral
        t = ck.claim_task(task_id, owner=owner, allow_stale=True)
        print(f"  🤝 task {task_id}: {t.get('status')} (owner={owner}, attempt={t.get('attempt', 0)})")
        return 0
    if aksiyon == "done":
        owner = os.environ.get("SYNC_NODE_NAME") or os.uname().nodename.lower()
        t = ck.done_task(task_id, owner=owner)
        print(f"  ✅ task {task_id}: {t.get('status')} (owner={owner})")
        return 0
    print("Kullanım: task add|list|claim|done")
    return 1

def cmd_backup(cfg, node=None, hub=None, dry_run=False):
    """GDrive versiyon takipli yedek (timestamp snapshot; silmez).

    18 Ağu 2026 FIX v2 (disk %100 olayı — syncver_* 175GB birikimi):
    - Her node tar'ı upload SONRASI hemen silinir (tmp'de birikmesin).
    - STALE TEMİZLİĞİ FONKSİYON BAŞINA TAŞINDI (v1'de finally'deydi — süreç
      timeout/kill olunca çalışmıyordu; 18 Ağu 23:05'te 71GB yeni birikim
      kanıtladı). Artık her koşu başında önceki kalıntılar temizlenir.
    Kök neden: tar+rclone 30dk aşınca süreç kill oluyor, finally rmtree
    çalışmıyor → her başarısız koşu ~10GB tar bırakıyor.
    """
    hub = _hub_base(hub)
    print(f"\n  💾 GDRIVE VERSİYON YEDEK — {hub}")
    nodes = [node] if node else list(cfg["dirs"].keys())
    # v2 FIX: stale syncver_* dizinleri KOŞU BAŞINDA temizle (finally güvenilmez)
    # v3 FIX (24 Ağu): EŞZAMANLI koşuların dizinini silme — yalnız 10 dk'dan eski
    # kalanları temizle (aktif koşunun tmp'si taze olur, dokunulmaz).
    try:
        _now = time.time()
        for stale in glob.glob("/tmp/syncver_*"):
            if os.path.isdir(stale) and (_now - os.path.getmtime(stale)) > 600:
                shutil.rmtree(stale, ignore_errors=True)
                print(f"    🧹 stale temizlendi (koşu başı): {os.path.basename(stale)}")
    except Exception:
        pass
    tmp = tempfile.mkdtemp(prefix="syncver_")
    try:
        for n in nodes:
            tarp, sha = _tar_node(cfg, n, tmp, time.strftime("%Y%m%d_%H%M%S"))
            if not tarp:
                print(f"    ⚠ {n}: atlandı (kaynak yok veya max_kb — yukarıya bak)"); continue
            if dry_run:
                print(f"    [DRY] {n}: {os.path.basename(tarp)} ({os.path.getsize(tarp)//1024}KB) sha={sha[:12]}")
                continue
            r = subprocess.run(["rclone", "copyto", tarp,
                                f"{hub}/{n}/{os.path.basename(tarp)}",
                                "--ignore-checksum", "--no-traverse"],
                               capture_output=True, text=True, errors="replace")
            if r.returncode == 0:
                # C modülü (v2.1): upload sonrası SHA doğrulama —
                # GDrive'daki hash'i çek, yerel sha ile karşılaştır.
                verified = False
                rr = subprocess.run(["rclone", "lsjson",
                                     f"{hub}/{n}", "--hash", "--files-only"],
                                    capture_output=True, text=True,
                                    errors="replace", timeout=180)
                if rr.returncode == 0:
                    try:
                        for f in json.loads(rr.stdout or "[]"):
                            if f.get("Path") == os.path.basename(tarp):
                                verified = (f.get("Hash", "") == sha)
                                break
                    except Exception:
                        verified = False
                tag_txt = " ✅ SHA doğrulandı" if verified else " ⚠ SHA doğrulanamadı (lsjson hash kapalı olabilir)"
                print(f"    ✅ {n}: {os.path.basename(tarp)} sha={sha[:12]}{tag_txt}")
            else:
                print(f"    ❌ {n}: {r.stderr.strip()[:120]}")
            # FIX: upload bitti → tar'ı HEMEN sil (birikme yok)
            try:
                os.remove(tarp)
            except OSError:
                pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        # FIX: stale syncver_* dizinleri (önceki koşulardan kalan) temizle —
        # yalnız 10 dk'dan eski (eşzamanlı koşu koruması, v3 24 Ağu)
        try:
            _now2 = time.time()
            for stale in glob.glob("/tmp/syncver_*"):
                if os.path.isdir(stale) and (_now2 - os.path.getmtime(stale)) > 600:
                    shutil.rmtree(stale, ignore_errors=True)
                    print(f"    🧹 stale temizlendi: {os.path.basename(stale)}")
        except Exception:
            pass

# ── v2.1 A modülü (29 Ağu 2026): versiyon etiketi regex'i ──
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# Versiyon tar dosya adı — path güvenli (SECURITY — OceanAPI 2026-08-30)
_VERSION_TAR_RE = re.compile(r"^[A-Za-z0-9._-]+\.tar\.gz$")


def _now_iso_utc() -> str:
    """UTC ISO-8601, Z sonekli (çift offset hatası yok — OceanAPI #12)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_tar_member(name: str) -> bool:
    """Tar üyesi path güvenli mi? (mutlak yol / .. / sürücü harfi YASAK)."""
    if not name or name.startswith(("/", "\\")):
        return False
    if ":" in name:
        return False
    return ".." not in name.split("/") and ".." not in name.split("\\")

def cmd_versions(cfg, node=None, hub=None, tag=None, diff=None):
    """GDrive'da node'un versiyonlarını listele (+ --tag etiketle / --diff karşılaştır).

    --tag <etiket>: güncel en son versiyonu etiketler (tags/<tag>.txt içinde
    tam dosya adı + SHA256 + ts). Aynı tag varsa RED (üzerine yazmaz).
    --diff <v1> <v2>: iki versiyon tar.gz listesini karşılaştırır (içerik
    indirmeden tar üye listeleri — eklenen/silinen/değişen).
    """
    hub = _hub_base(hub)
    nodes = [node] if node else list(cfg["dirs"].keys())
    for n in nodes:
        r = subprocess.run(["rclone", "lsf", f"{hub}/{n}", "--files-only"],
                           capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            print(f"    ❌ {n}: versiyon listesi okunamadı: {r.stderr.strip()[:120]}")
            return 1
        vers = [f for f in r.stdout.splitlines() if f.endswith(".tar.gz")]
        if tag:
            rc = _tag_version(cfg, n, tag, vers, hub=hub)
            if rc != 0:
                return rc
            continue
        if diff:
            rc = _diff_versions(cfg, n, diff, hub=hub)
            if rc != 0:
                return rc
            continue
        print(f"\n  📦 {n}: {len(vers)} versiyon")
        for v in vers[-8:]:
            print(f"    {v}")
    return 0

def _tag_version(cfg, node, tag, vers, hub=None):
    """Versiyon etiketle — non-destructive (aynı tag RED).

    tags/<tag>.txt içeriği: {node, tag, version, sha256, ts}
    """
    if not vers:
        print(f"    ⚠ {node}: versiyon yok — etiketlenemedi")
        return 1
    if not _TAG_RE.match(tag):
        print(f"    ⛔ {node}: geçersiz etiket '{tag}' (^[a-z0-9][a-z0-9._-]{0,63}$)")
        return 1
    hub = _hub_base(hub)
    tags_dir = f"{hub}/{node}/tags"
    tag_file = f"{tags_dir}/{tag}.txt"
    # aynı tag var mı? (lsf hatası → RED, fail-open değil — OceanAPI #5)
    r = subprocess.run(["rclone", "lsf", tags_dir, "--files-only"],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        print(f"    ❌ {node}: tag listesi okunamadı: {r.stderr.strip()[:120]}")
        return 1
    if f"{tag}.txt" in (r.stdout or "").splitlines():
        print(f"    ⛔ {node}: tag '{tag}' zaten var (RED — üzerine yazılmaz)")
        return 1
    latest = vers[-1]
    # Uzak hash — GDrive için genelde MD5 olabilir; 'sha256' DEĞİL,
    # 'remote_hash' olarak etiketlenir (OceanAPI #10).
    rr = subprocess.run(["rclone", "lsjson", f"{hub}/{node}", "--hash",
                         "--files-only"],
                        capture_output=True, text=True, errors="replace")
    sha = ""
    if rr.returncode == 0:
        try:
            for f in json.loads(rr.stdout or "[]"):
                if f.get("Path") == latest:
                    sha = f.get("Hash", "")
                    break
        except Exception:
            sha = ""
    meta = json.dumps({"node": node, "tag": tag, "version": latest,
                       "remote_hash": sha,
                       "ts": _now_iso_utc()},
                      ensure_ascii=False, indent=1)
    tmp = tempfile.mkdtemp(prefix="synctag_")
    try:
        lp = os.path.join(tmp, f"{tag}.txt")
        with open(lp, "w") as f:
            f.write(meta + "\n")
        r2 = subprocess.run(["rclone", "copyto", lp, tag_file],
                            capture_output=True, text=True, errors="replace",
                            timeout=180)
        if r2.returncode != 0:
            print(f"    ❌ {node}: tag yazılamadı: {r2.stderr.strip()[:120]}")
            return 1
        print(f"    🏷  {node}: '{tag}' → {latest}"
              + (f" (remote hash {sha[:12]}…)" if sha else " (hash yok)"))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def _diff_versions(cfg, node, diff, hub=None):
    """İki versiyon arası dosya farkı — tar üye listelerini karşılaştır.

    diff formatı: 'v1.tar.gz,v2.tar.gz' (virgülle ayrılmış).
    İçerik indirilmez; rclone cat ile tar üye listesi okunur (stream).
    """
    hub = _hub_base(hub)
    parts = [p.strip() for p in diff.split(",")]
    if len(parts) != 2:
        print(f"    ⚠ {node}: --diff 'v1.tar.gz,v2.tar.gz' formatı beklenir")
        return 1
    v1, v2 = parts
    # shell enjeksiyon + path traversal koruması (OceanAPI #1)
    if not _VERSION_TAR_RE.match(v1) or not _VERSION_TAR_RE.match(v2):
        print(f"    ⛔ {node}: geçersiz versiyon adı "
              f"(yalnız [A-Za-z0-9._-]+.tar.gz kabul edilir)")
        return 1
    lists = []
    for v in (v1, v2):
        # rclone cat | tar tzf - — shlex.quote ile shell-escape (OceanAPI #1)
        r = subprocess.run(["bash", "-c",
                            f"rclone cat {shlex.quote(f'{hub}/{node}/{v}')} | tar tzf - 2>/dev/null"],
                           capture_output=True, text=True, errors="replace",
                           timeout=300)
        if r.returncode != 0:
            print(f"    ⚠ {node}: {v} okunamadı ({r.stderr.strip()[:100]})")
            return 1
        members = set(l for l in r.stdout.splitlines() if l.strip())
        lists.append(members)
    only1 = sorted(lists[0] - lists[1])
    only2 = sorted(lists[1] - lists[0])
    print(f"\n  🔀 {node}: {v1} ↔ {v2}")
    print(f"    ➕ sadece {v1}: {len(only1)} dosya")
    for f in only1[:10]:
        print(f"      + {f}")
    if len(only1) > 10:
        print(f"      … +{len(only1) - 10} daha")
    print(f"    ➖ sadece {v2}: {len(only2)} dosya")
    for f in only2[:10]:
        print(f"      - {f}")
    if len(only2) > 10:
        print(f"      … -{len(only2) - 10} daha")
    return 0

def cmd_rollback(cfg, node, version, hub=None, force=False, dry_run=False):
    """Belirli versiyonu non-destructive geri al (.conflict korur; --force ez).

    --dry-run: ön-inceleme — hangi dosyalar değişecek, kaç çakışma olacak;
    HİÇBİR ŞEY yazmaz (v2.1 A modülü).
    """
    if not node or not version:
        print("Kullanım: sync_motor.py rollback <node> --version <dosya.tar.gz> [--force] [--dry-run]")
        return 1
    # path traversal koruması (OceanAPI #3)
    if not _VERSION_TAR_RE.match(version) or os.path.basename(version) != version:
        print(f"    ⛔ geçersiz version '{version}' "
              f"(yalnız [A-Za-z0-9._-]+.tar.gz dosya adı kabul edilir)")
        return 1
    hub = _hub_base(hub)
    dst = cfg["dirs"][node]
    base = dst["path"] if isinstance(dst, dict) else dst
    tmp = tempfile.mkdtemp(prefix="syncrb_")
    try:
        tarp = os.path.join(tmp, version)
        r = subprocess.run(["rclone", "copyto", f"{hub}/{node}/{version}", tarp],
                           capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            print(f"    ❌ indirme hatası: {r.stderr.strip()[:120]}")
            return 1
        if not os.path.exists(tarp):
            print(f"    ❌ indirilen dosya bulunamadı (copyto rc=0 ama yok): {tarp}")
            return 1
        # önce tar üye listesi + değişecek dosyaları hesapla
        changed = 0
        conflicts = 0
        skipped = 0
        with tarfile.open(tarp, "r:gz") as tar:
            members = [m for m in tar.getmembers() if m.isfile()]
            for m in members:
                # tar üyesi path güvenliği (OceanAPI #4)
                if not _safe_tar_member(m.name):
                    skipped += 1
                    continue
                dstp = os.path.join(base, m.name)
                if os.path.exists(dstp):
                    srcp = os.path.join(tmp, m.name)
                    tar.extract(m, path=tmp, filter="data")
                    if not filecmp.cmp(dstp, srcp, shallow=False):
                        changed += 1
                        if not force:
                            conflicts += 1
                else:
                    changed += 1
        if skipped:
            print(f"    ⚠ {skipped} güvensiz tar üyesi atlandı")
        if dry_run:
            print(f"    🔍 [DRY-RUN] {node} ← {version}: {changed} dosya değişecek, "
                  f"{conflicts} çakışma korunacak (force={force})")
            print("    HİÇBİR ŞEY yazılmadı.")
            return 0
        with tarfile.open(tarp, "r:gz") as tar:
            for m in tar.getmembers():
                if not m.isfile() or not _safe_tar_member(m.name):
                    continue
                dstp = os.path.join(base, m.name)
                if os.path.exists(dstp) and not force:
                    # farklıysa .conflict koru, değilse atla
                    srcp = os.path.join(tmp, m.name)
                    tar.extract(m, path=tmp, filter="data")
                    if not filecmp.cmp(dstp, srcp, shallow=False):
                        c = f"{dstp}.conflict.{int(time.time())}"
                        shutil.copy(srcp, c)
                        print(f"    ! çakışma korundu: {os.path.basename(c)}")
                else:
                    tar.extract(m, path=base, filter="data")
        print(f"    ✅ {node} ← {version} geri alındı (force={force})")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════
# D MODÜLÜ — ORTAK HAFIZA (v2.1, 29 Ağu 2026)
# sync_memory.py (v0.1→v1.0 AKTİF): memory DIF JSONL → GDrive hub
# gdrive:hermes-sync/<user>/shared/memory/ push/pull + import +
# fact_store (memory_store.db facts) entegrasyonu.
# Tasarım ilkeleri (GPT-5.6): canlı DB senkronu YOK — mantıksal delta;
# çakışma preserve (.conflict~node~ts); secret allowlist; HLC saat.
# ═══════════════════════════════════════════════════════════════

MEMORY_HUB_SUBDIR = "shared/memory"          # cfg['gdrive']['user_root'] altında
DEFAULT_MEMORY_DIR = os.path.expanduser("~/.hermes/memory")


def _memory_hub(cfg):
    """GDrive hub yolu: gdrive:hermes-sync/<user>/shared/memory"""
    user_root = cfg.get("gdrive", {}).get("user_root",
                                          "gdrive:hermes-sync/cumulusnet")
    return f"{user_root}/{MEMORY_HUB_SUBDIR}"


def _memory_node_id(cfg):
    """Makine kimliği — memory DIF source.node_id."""
    mid = cfg.get("identity", {}).get("machine_id") or ""
    return mid or (os.uname().nodename if os.name != "nt"
                   else os.environ.get("COMPUTERNAME", "unknown"))


def memory_export(memory_dir, cfg, dry_run=False):
    """Yerel memory DIF'lerini JSONL delta'ya export et.

    sync_memory.export_memory_delta: namespace'leri (shared/private/
    quarantine) tarar, secret allowlist'ten geçirir, deltas/<ts>-<node>-
    <seq>.jsonl yazar. Secret hit → ValueError (RED, hiçbir şey gitmez).
    """
    node_id = _memory_node_id(cfg)
    agent_id = cfg.get("identity", {}).get("user_id", "cumulusnet")
    if dry_run:
        print(f"    [DRY] export memory_delta (node={node_id}, agent={agent_id})")
        return None
    try:
        exp = smem.export_memory_delta(memory_dir, node_id, agent_id, since_seq=0)
        print(f"    ✅ export: {exp['records']} kayıt → {os.path.basename(exp['delta'])}")
        return exp["delta"]
    except ValueError as e:
        print(f"    ⛔ SECRET RED: {e}")
        return None


def memory_push(cfg, delta_path, dry_run=False):
    """Delta JSONL'yi hub'a yükle (rclone copy — silmez, non-destructive)."""
    if not delta_path:
        return False
    hub = _memory_hub(cfg)
    if dry_run:
        print(f"    [DRY] rclone copy {os.path.basename(delta_path)} → {hub}")
        return True
    r = subprocess.run(["rclone", "copy", delta_path, hub],
                       capture_output=True, text=True, errors="replace",
                       timeout=180)
    if r.returncode != 0:
        print(f"    ❌ push hatası: {r.stderr.strip()[:150]}")
        return False
    print(f"    ✅ push: {os.path.basename(delta_path)} → {hub}")
    return True


def memory_pull_import(cfg, memory_dir, dry_run=False):
    """Hub'daki uzak deltaları çek + import (conflict_policy='preserve').

    Adımlar: 1) rclone lsf hub/*.jsonl → 2) her delta yerel incoming'e
    rclone copyto → 3) smem.import_memory_delta (aynı revision: eski atla,
    eşit+aynı hlc: atla, eşit+farklı hlc: .conflict koru, tombstone: kaldır).
    """
    hub = _memory_hub(cfg)
    node_id = _memory_node_id(cfg)
    incoming = os.path.join(memory_dir, "incoming")
    os.makedirs(incoming, exist_ok=True)
    if dry_run:
        print(f"    [DRY] pull+import deltas from {hub}")
        return 0
    r = subprocess.run(["rclone", "lsf", hub, "--files-only"],
                       capture_output=True, text=True, errors="replace",
                       timeout=90)
    if r.returncode != 0:
        # 29 Ağu FIX (OceanAPI #8): -1 = HARD hata — cmd_memory rc=1 döner,
        # cron görür; hub geçici kapalıysa retry şansı verir.
        print(f"    ❌ hub listelenemedi: {r.stderr.strip()[:120]}")
        return -1
    deltas = [f for f in (r.stdout or "").splitlines()
              if f.endswith(".jsonl")]
    total_applied = total_conflicts = total_tomb = 0
    for fn in sorted(deltas):
        dst = os.path.join(incoming, fn)
        rr = subprocess.run(["rclone", "copyto", f"{hub}/{fn}", dst],
                            capture_output=True, text=True, errors="replace",
                            timeout=180)
        if rr.returncode != 0:
            print(f"    ⚠ {fn} indirilemedi: {rr.stderr.strip()[:100]}")
            continue
        res = smem.import_memory_delta(memory_dir, dst, node_id,
                                       conflict_policy="preserve")
        total_applied += res.get("applied", 0)
        total_conflicts += res.get("conflicts", 0)
        total_tomb += res.get("tombstones", 0)
    if deltas:
        print(f"    ✅ import: {len(deltas)} delta, {total_applied} uygulandı, "
              f"{total_conflicts} çakışma korundu, {total_tomb} tombstone")
    else:
        print("    ℹ hub'da delta yok (ilk koşu olabilir)")
    return total_applied


def memory_to_fact_store(memory_dir, dry_run=False, db_path=None):
    """Import sonrası DIF kayıtlarını fact_store'a (memory_store.db) yaz.

    facts şeması: (content UNIQUE, category, tags, trust_score, ...).
    İçerik: subject — predicate — value metni; INSERT OR IGNORE (dedup).
    Bu, Hermes bellek füzyon hattıyla (memory_fusion.py facts_fts) birleşir.
    """
    db_path = db_path or os.path.expanduser("~/.hermes/memory_store.db")
    if not os.path.exists(db_path):
        print(f"    ℹ memory_store.db yok ({db_path}) — fact yazılmadı")
        return 0
    try:
        import sqlite3
    except ImportError:
        print("    ⚠ sqlite3 yok — fact yazılmadı")
        return 0
    added = 0
    try:
        conn = sqlite3.connect(db_path)
    except Exception as e:
        # 29 Ağu FIX (OceanAPI #8): -1 = HARD hata — cmd_memory rc=1 döner
        print(f"    ❌ fact_store bağlantı hatası: {e}")
        return -1
    try:
        for ns in smem.MEMORY_NAMESPACES:
            ns_dir = os.path.join(memory_dir, ns)
            if not os.path.isdir(ns_dir):
                continue
            for fn in sorted(os.listdir(ns_dir)):
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(ns_dir, fn)) as f:
                        rec = json.load(f)
                except Exception:
                    continue
                if rec.get("tombstone"):
                    continue
                subj = str(rec.get("subject", "")).strip()
                pred = str(rec.get("predicate", "")).strip()
                val = rec.get("value")
                if isinstance(val, (dict, list)):
                    val = json.dumps(val, ensure_ascii=False)
                content = " — ".join(x for x in (subj, pred, str(val)) if x)
                if not content:
                    continue
                if dry_run:
                    added += 1
                    continue
                cat = (pred or "sync-memory")[:63]
                tags = rec.get("namespace", "shared")
                cur = conn.execute(
                    "INSERT OR IGNORE INTO facts "
                    "(content, category, tags, trust_score) VALUES (?,?,?,?)",
                    (content, cat, tags, 0.5))
                added += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if added:
        if dry_run:
            print(f"    [DRY] fact_store: {added} kayıt adayı (yazılmaz)")
        else:
            print(f"    ✅ fact_store: +{added} kayıt ({db_path})")
    return added


def cmd_memory(cfg, dry_run=False, memory_dir=None, memory_db=None):
    """Ortak hafıza D modülü: export → push → pull/import → fact_store.

    Kullanım: sync_motor.py memory [--dry-run] [--memory-dir <dir>]
    Cron bağlantısı: node_agent.py once içinde 'memory' adımı (v2.1).
    memory_db: fact_store hedefi (varsayılan ~/.hermes/memory_store.db;
    testler geçici DB verir — gerçek DB'ye yazılmaz).
    """
    memory_dir = memory_dir or DEFAULT_MEMORY_DIR
    os.makedirs(memory_dir, exist_ok=True)
    print(f"\n  🧠 ORTAK HAFIZA (D) — {_memory_hub(cfg)}")
    print(f"    yerel: {memory_dir}")

    if dry_run:
        # export hiçbir şey üretmez (None); tüm adımlar simüle edilir
        memory_export(memory_dir, cfg, dry_run=True)
        memory_push(cfg, None, dry_run=True)
        memory_pull_import(cfg, memory_dir, dry_run=True)
        memory_to_fact_store(memory_dir, dry_run=True, db_path=memory_db)
        return 0

    delta = memory_export(memory_dir, cfg, dry_run=False)
    if delta is None:
        return 1  # secret RED veya export hatası
    if not memory_push(cfg, delta, dry_run=False):
        return 1
    pr = memory_pull_import(cfg, memory_dir, dry_run=False)
    if isinstance(pr, int) and pr < 0:
        return 1  # hub hard hata (OceanAPI #8) — pull/import başarısız
    fs = memory_to_fact_store(memory_dir, dry_run=False, db_path=memory_db)
    if isinstance(fs, int) and fs < 0:
        return 1  # fact_store hard hata — cron görsün, retry edebilsin
    return 0

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="sync_motor",
        description="Cumulus Sync Motoru — GitHub+GDrive+Tailscale "
                    "iki yönlü senkronizasyon")
    parser.add_argument("komut", nargs="?", default="status",
                        choices=["status", "push", "pull", "both",
                                 "conflicts", "init", "select", "nodes",
                                 "add-node", "share", "doctor", "version",
                                 "probe", "propose", "apply", "agent-status",
                                 "backup", "versions", "rollback", "memory", "mesh", "discover", "task"])
    parser.add_argument("hedef", nargs="?",
                        help="add-node: node adı | share: node adı")
    parser.add_argument("--config", default=None,
                        help="config.json yolu")
    parser.add_argument("--node", default=None,
                        help="sadece belirli node'u eşitle (kernel, patent, "
                             "scripts, pcb, hermes, openclaw... veya add-node "
                             "ile eklenen herhangi biri)")
    parser.add_argument("--path", default=None,
                        help="add-node: yeni node dizini")
    parser.add_argument("--include", default="*",
                        help="add-node: dahil edilecek pattern (örn: *.md,*.txt)")
    parser.add_argument("--max-kb", type=int, default=1024,
                        help="add-node: maksimum dosya boyutu KB")
    parser.add_argument("--to", default=None,
                        help="share: hedef kullanıcı")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug log")
    parser.add_argument("--dry-run", action="store_true",
                        help="push/both: ne yapılacağını göster, HİÇBİR ŞEY yazma (v1.4)")
    parser.add_argument("--tool", default=None,
                        help="apply: kurulacak araç adı (katalogdan)")
    parser.add_argument("--yes", action="store_true",
                        help="apply: onay sormadan kur (varsayılan: interaktif onay)")
    parser.add_argument("--no-color", action="store_true",
                        help="renksiz çıktı")
    parser.add_argument("--skip-unchanged", action="store_true",
                        help="push/both: içerik değişmediyse node'u atla (delta, v1.6.2)")
    parser.add_argument("--hub", default=None,
                        help="backup/versions/rollback: GDrive versiyon dizini (varsayılan gdrive:cumulusos-backups/versiyonlu)")
    parser.add_argument("--version", default=None,
                        help="rollback: geri alınacak versiyon dosyası (ör: kernel_20260815_120000.tar.gz)")
    parser.add_argument("--force", action="store_true",
                        help="rollback: çakışma dosyası oluşturmadan üzerine yaz")
    parser.add_argument("--memory-dir", default=None,
                        help="memory: yerel memory DIF dizini (varsayılan ~/.hermes/memory)")
    parser.add_argument("--tag", default=None,
                        help="versions: en son versiyonu etiketle (örn: kernel-v2.3-dgk)")
    parser.add_argument("--token", dest="token", default="")
    parser.add_argument("--task-id", dest="task_id", default="")
    parser.add_argument("--diff", default=None,
                        help="versions: iki versiyon arası dosya farkı (v1.tar.gz,v2.tar.gz)")
    args = parser.parse_args(argv)

    if args.komut == "version":
        print(f"sync_motor v{__version__}")
        return 0

    # Logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logfile = os.path.expanduser(DEFAULT_CONFIG["state"]["logfile"])
    setup_logging(level, logfile)

    cfg = load_config(args.config)

    # ── v1.6.4: TEK-INSTANCE KİLİT — aynı anda iki sync aynı hub'a yazmasın
    lock_fd = None
    if args.komut in MUTATING_CMDS and not args.dry_run:
        lock_fd = acquire_lock()
        if lock_fd is None:
            print("⛔ Başka bir sync işlemi çalışıyor — bu koşu ATLANDI "
                  "(kilit aktif, /tmp/cumulus_sync.lock)", file=sys.stderr)
            return 0   # cron no_agent: exit 0 = sessiz atla; sorun değil

    print(f"\n╔{'═'*58}╗")
    print(f"║  CUMULUS SYNC MOTOR v{__version__} — {cfg['machine']}"
          f"{' '*(34-len(cfg['machine']))}║")
    print(f"╚{'═'*58}╝")

    rc = 0
    if args.komut == "status":
        cmd_status(cfg)
    elif args.komut == "push":
        cmd_push(cfg, node=args.node, dry_run=args.dry_run)
    elif args.komut == "pull":
        cmd_pull(cfg)
    elif args.komut == "both":
        cmd_push(cfg, node=args.node, dry_run=args.dry_run)
        if not args.dry_run:
            cmd_pull(cfg)
    elif args.komut == "doctor":
        rc = cmd_doctor(cfg)
    elif args.komut == "probe":
        rc = cmd_probe(cfg)
    elif args.komut == "propose":
        rc = cmd_propose(cfg)
    elif args.komut == "apply":
        if not args.tool:
            print("Kullanım: sync_motor.py apply --tool <ad> [--yes]")
            rc = 1
        else:
            rc = cmd_apply(cfg, args.tool, yes=args.yes)
    elif args.komut == "conflicts":
        cmd_conflicts(cfg)
    elif args.komut == "agent-status":
        cmd_agent_status(cfg)
    elif args.komut == "discover":
        rc = cmd_discover(cfg, args.token)
    elif args.komut == "task":
        rc = cmd_task(cfg, args.hedef or "list", args.task_id or args.node or "", args.path or "", args.token)
    elif args.komut == "mesh":
        # Kullanım: mesh status | mesh send <host> "<görev>" (--node host, --path görev)
        rc = cmd_mesh(cfg, args.hedef or "status", args.node or "", args.path or "", args.token)
    elif args.komut == "backup":
        # v2.0 (28 Ağu): restic entegre — backup komutu artık incremental restic
        # yedekler (CDC dedup + snapshot + restore). Eski tar tabanlı davranış
        # --legacy-tar ile korunur (henüz eklenmedi; eski snapshot'lar duruyor).
        if os.environ.get("SYNC_BACKUP_ENGINE", "restic") == "restic":
            rc = cmd_restic_backup(cfg, node=args.node, dry_run=args.dry_run)
        else:
            rc = cmd_backup(cfg, node=args.node, hub=args.hub, dry_run=args.dry_run)
    elif args.komut == "versions":
        rc = cmd_versions(cfg, node=args.node, hub=args.hub,
                          tag=args.tag, diff=args.diff)
    elif args.komut == "rollback":
        rc = cmd_rollback(cfg, args.node, args.version, hub=args.hub,
                          force=args.force, dry_run=args.dry_run)
    elif args.komut == "memory":
        rc = cmd_memory(cfg, dry_run=args.dry_run, memory_dir=args.memory_dir)
    elif args.komut == "init":
        cmd_init(cfg)
    elif args.komut == "nodes":
        cmd_nodes(cfg)
    elif args.komut == "select":
        cmd_select(cfg)
    elif args.komut == "add-node":
        node_name = args.hedef or args.node
        if not node_name or not args.path:
            print("Kullanım: sync_motor.py add-node <ad> --path <dizin> "
                  "[--include '*.md,*.txt'] [--max-kb 1024]")
            rc = 1
        else:
            cmd_add_node(cfg, node_name, args.path, args.include, args.max_kb)
    elif args.komut == "share":
        node_name = args.hedef or args.node
        if not node_name or not args.to:
            print("Kullanım: sync_motor.py share <node> --to <kullanıcı>")
            rc = 1
        else:
            cmd_share(cfg, node_name, args.to)

    # ── v1.6.4: son-koşu kaydı (mutating koşular + agent-status okuyucuları)
    if args.komut in MUTATING_CMDS and not args.dry_run:
        record_run(cfg, args.komut, rc, node=args.node)
        if lock_fd is not None:
            try:
                lock_fd.close()
            except Exception:
                pass

    return rc


if __name__ == "__main__":
    sys.exit(main())
