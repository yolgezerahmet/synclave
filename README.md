# Synclave — Encrypted Multi-Node Agent Mesh

Hermes/OpenClaw ağları için **şifreli çok nokta yedekleme + senkronizasyon + ajan mesh** motoru.
Non-destructive, versiyonlu, sınırsız node. **MIT** lisansı.

> Eski ad: `hermes-sync` (v2.3.1) → rebrand: **Synclave** (v1.0.0).
> Özel CumulusNET kopyası: `cumulus-sync-motor` (private). Bu repo (public) evrensel
> Hermes/OpenClaw kullanımı içindir.

## v2.3 — 30 Ağu 2026: Kriptografik Kimlik + Şifreli Mesh + Sohbet Köprüsü

**Yeni yetenekler (bu sürümde):**
- **Kopyalanamaz-kanıtlı ajan kimliği** (`agent_identity.py`): her çalışma
  zamanı (Hermes/OpenClaw) kendi Ed25519 kimliğine sahiptir; agent_id = açık
  anahtar özeti (`hx-` / `oc-`), donanım parmak izine bağlıdır, klon şüphesinde
  mesh 403 ile reddeder (fail-closed). Meşru taşıma için `rekey --confirm`.
- **Uçtan uca şifreli A2A**: X25519 ECDH + AES-GCM (her mesajda ephemeral →
  Perfect Forward Secrecy) + Ed25519 imza + ts/nonce replay koruması.
  Aradaki dinleyici içeriği çözemez; kurcalama/replay reddedilir.
- **Sohbet köprüsü** (`conversation_bridge.py`): Hermes state.db'deki tüm
  kullanıcı sohbetleri (telegram/cli/whatsapp) kalıcı sohbet defterine akar;
  içerik saklanmaz (sadece sha256). Sohbet ID: `u.<agent>.<kanal>.<peer>.<ulid>`
  (kullanıcı) / `a.<agent>~<peer>.<kanal>.<ulid>` (ajan-ajan).
- **GPU analiz kanalı** (`gpu_agent.py` + `gpu_task.py`): GPU'lu node (örn.
  Windows + RTX) analiz görevlerini yerel kartta işler; sonuçlar mesh ile
  yayılır.
- **Rate limiting**: 429 Too Many Requests (IP+agent, 120 req/60s, env ile
  ölçeklenir) — brute-force koruması.
- **Ölçek**: her node kendi anahtarına sahip → 3/20 node aynı model
  (mac/windows/linux); ortak token tek başına yetmez, kimlik anahtar tabanlı.

```bash
# kimlik + şifreli sohbet
python3 agent_identity.py show                 # kimlik göster/üret
python3 agent_identity.py verify-self          # imza + ID doğrula
python3 a2a_cli.py send <host> "görev" --token <A2A_TOKEN>   # otomatik şifreli

# sohbet köprüsü (her 15 dk / cron)
python3 conversation_bridge.py --full          # ilk kurulum (tüm geçmiş)
python3 conversation_bridge.py                 # artımlı (watermark)

# GPU analiz (H2 RTX örnek)
python3 gpu_task.py status                     # H2 GPU durumu
python3 gpu_task.py task "PCB BGA fanout analizi"   # GPU'da işle
```

### Güvenlik özeti (v2.3)
- Kimlik: agent_id = pubkey özeti; donanım bağı + klon fail-closed
- Bütünlük: Ed25519 imza (gövde + ts + nonce)
- Gizlilik: X25519 ECDH + AES-GCM (PFS)
- Replay: ts (±120s) + nonce tekrarı reddi
- Brute-force: rate limit (429)
- Eski sunucularla geriye uyum: imzalı ama düz gövde (otomatik seçim)

## v2.1 — 29 Ağu 2026: Ajan Mesh + Restic

```
┌────────────┐   A2A (JSON-RPC, Tailscale)   ┌────────────┐
│  H1 Hermes │ ◄──────────────────────────► │  H3 Hermes │
│  (VPS)     │                               │  (Proxmox) │
└──┬─────┬───┘                               └──┬─────┬───┘
   │     │ Syncthing (P2P dosya)               │     │
   │     └─────────────────────────────────────┘     │
   │              ┌────────────┐                     │
   └─────────────►│   GDrive   │◄────────────────────┘
                  │  restic    │  (ortak yedek repo)
                  │  state.json│  (ortak durum)
                  └────────────┘
   ┌────────────┐
   │  H2 Hermes │  (A2A + restic canlı; Syncthing/worker talimatlı)
   │  (Windows) │
   └────────────┘
```

### Bileşenler

| Bileşen | Dosya | Açıklama |
|---|---|---|
| Senkron motoru | `sync_motor.py` | push/pull/both/backup/versions/rollback + `mesh` komutu (kanal seçici) |
| Node ajanı | `node_agent.py` | otonom eşitleme + yedek + hub raporu + ortak akıl |
| A2A mesh server | `agent_mesh_a2a.py` | Ajanlar arası konuşma (JSON-RPC, port 8643) |
| A2A client | `a2a_cli.py` | send/get/stream — görev gönder, sonuç al, canlı akış dinle |
| Görev işleyici | `inbox_worker.py` | A2A inbox görevlerini çalıştırır (allowlist) |
| Ortak akıl | `sync_common_knowledge.py` | GDrive hub'da dağıtık ortak durum + görev kuyruğu (HLC) |
| Ortak hafıza | `sync_memory.py` | Memory DIF'leri JSONL + audit hash-chain |
| Retention | `sync_retention.py` | Snapshot yaşam döngüsü |
| Akıllı kurulum | `probe/propose/apply` | Kaynak farkındalıklı kurulum önerisi |
| Ajan kimliği | `agent_identity.py` | Ed25519 + X25519 kimlik, klon tespiti, sohbet defteri |
| Şifreli mesh | `agent_mesh_a2a.py` | A2A + X-Agent-Enc şifreli gövde + rate limit |
| Sohbet köprüsü | `conversation_bridge.py` | state.db → sohbet defteri (watermark) |
| GPU analiz | `gpu_agent.py` / `gpu_task.py` | GPU'lu node'da analiz görevi |

### 3 Katmanlı Akıllı Kanal Mimarisi

```
Görev/cevap  → A2A      (Tailscale HTTP, anlık — saniyeler)
Dosya değişimi → Syncthing (P2P, GDrive'suz — fsWatcher)
Arşiv/yedek  → GDrive  (restic incremental + versiyonlu snapshot)
```

### A2A — 3 İletişim Modu

```bash
# SENKRON: anında sonuç
python3 a2a_cli.py send-status 100.103.44.107 --token <TOKEN>

# ASENKRON: görev gönder → task_id → sonra sonucu al (kalıcı task store)
python3 a2a_cli.py send <host> "uptime" --mode async --token <TOKEN>
python3 a2a_cli.py get <host> --task-id <TASK_ID> --token <TOKEN>

# CANLI: SSE akışı (2s'de bir veri)
python3 a2a_cli.py stream <host> "selam" --seconds 10 --token <TOKEN>
```

### Restic Incremental Yedek

```bash
# rclone serve restic gdrive:restic-backup --addr 127.0.0.1:8443
python3 sync_motor.py backup            # CDC dedup + snapshot + restore
python3 sync_motor.py versions          # snapshot listesi
python3 sync_motor.py rollback <node> --version <snapshot> --dry-run
```

Retention: forget keep-daily 7 / weekly 4 / monthly 6 — prune yalnız birincil makinede (04:00).

### Kurulum (yeni node)

```bash
python3 sync_motor.py init              # config üret (gdrive:synclave/<user>/)
python3 sync_motor.py add-node <ad> --path <dizin> [--include '*.md'] [--max-kb 1024]
python3 sync_motor.py both              # push + pull
python3 sync_motor.py mesh status       # tüm node'ların A2A durumu
```

### Gereksinimler

- Python 3.10+, rclone (GDrive remote), restic 0.19+ (yedek), fastapi+uvicorn (A2A server)
- Tailscale veya doğrudan erişim (A2A 8643, Syncthing 22000/8384)

### Windows Kurulum (H2 + Windows 10/11)

```powershell
# 1) Python 3.10+ (python.org — PATH'e ekle) + git
python --version

# 2) Paket + CLI kur
pip install synclave
pip install rclone                # veya winget install Rclone.Rclone
pip install restic                # veya restic.net binary → PATH'e ekle
pip install uvicorn fastapi       # A2A server için (opsiyonel)

# 3) rclone — GDrive remote (tek seferlik OAuth)
rclone config
#    remote adı: gdrive
#    (client_id paylaşılmışsa "shared client_id" uyarısı — kendi client_id'niz
#     Google Cloud OAuth'da daha hızlı ve 2026 sonrası zorunlu)

# 4) restic — GDrive object store'u mount et (arka plan servisi)
rclone serve restic gdrive:restic-backup --addr 127.0.0.1:8443
#    Windows: `schtasks /create` veya NSSM ile oturum açılışında başlat

# 5) Syncthing — P2P dosya kanalı (opsiyonel ama önerilir)
winget install syncthing.syncthing
#    GUI 127.0.0.1:8384 → H1/H3 cihazları eşleştir (device ID'ler)

# 6) A2A token — H1/H3 ile aynı ortak token'ı .env/ortam değişkenine yaz
setx A2A_TOKEN "test-a2a-mesh-2026"     # kendi ortak değerinizle değiştirin

# 7) İlk senkron
python -m synclave.sync_motor init    # config üret
python -m synclave.sync_motor both    # push + pull
python -m synclave.sync_motor mesh status
```

Windows notları:
- Kilit dosyaları `%TEMP%\cumulus_sync.lock` (motor) ve
  `%TEMP%\cumulus_node_agent.lock` (ajan) — `/tmp` kullanılmaz (`msvcrt.locking`;
  POSIX'te `fcntl.flock`). Ajan kilidi doluysa hub raporu adımı ATLANIR, koşu
  düşmez; motor kilidi ayrı dosyadır (döngüsel kilitlenme olmaz).
- Kapsam: dosya kilidi yalnızca aynı makinedeki ajanları koordine eder; farklı
  makineler makineye özel `status.json` yazar.
- `sync_motor.py` / `sync_common_knowledge.py` path'leri `os.path.join` ile kurar;
  geçici dizinler `_platform_temp_dir()` ile platformdan türetilir (sabit `/` yok).
- A2A istemcisi (`a2a_cli.py`) yalnızca `urllib` kullanır — ek bağımlılık gerekmez.
- A2A server `uvicorn` bulunamazsa net hata mesajı basar ve çıkar (traceback değil).
- Uzaktan kurulum için hazır betikler: `remote_hermes_setup.ps1` (SSH/Tailscale).
- Adım adım tam prosedür (rclone OAuth, restic, Syncthing, A2A token, Task Scheduler):
  [`docs/windows.md`](docs/windows.md).

### Güvenlik

- `.env`, `*.key`, `*.pem`, token içeren dosyalar ASLA kapsama alınmaz (secret filtre)
- A2A: Bearer token + Tailscale-only (dışa kapalı)
- Görev işleyici: allowlist komutlar (status/uptime/test:<modul>/shell:ls)
- Non-destructive: çakışma `.conflict.TS` korunur, üzerine yazma yok

## Geçmiş

- v1.6 (12 Ağu): akıllı kurulum (probe/propose/apply)
- v1.3 (3 Ağu): evrensel — kullanıcı/makine kimliği, sınırsız node, share
- v1.0 (3 Ağu): GitHub manifest + GDrive versiyonlu + OpenClaw skill
