# Synclave — Encrypted Multi-Node Agent Mesh

Hermes/OpenClaw ağları için **şifreli çok nokta yedekleme + senkronizasyon + ajan mesh** motoru.
Non-destructive, versiyonlu, sınırsız node. **MIT** lisansı.

> Eski ad: `hermes-sync` (v2.3.1) → rebrand: **Synclave** (v1.0.0).
> Özel CumulusNET kopyası: `cumulus-sync-motor` (private). Bu repo (public) evrensel
> Hermes/OpenClaw kullanımı içindir.

## Kurulum (PyPI)

```bash
pip install synclave                       # PyPI — v2.7.9
synclave status                            # JSON durum (synclave status --json)
synclave init                              # ilk config
synclave both --skip-unchanged             # push + pull
synclave mesh status                       # makinelerin A2A durumu
synclave backup                            # restic incremental yedek
```

Kaynaktan: `git clone <repo> && pip install -e .`. CLI'lar: `synclave`,
`synclave-a2a` (A2A istemcisi), `synclave-worker` (görev işleyici).
Windows adımları aşağıda ([Windows Kurulum](#windows-kurulum-h2--windows-1011));
adım adım tam prosedür: [`docs/windows.md`](docs/windows.md).

## v2.7 — 14-17 Eyl 2026: rclone güvenilirliği (retry + üç durumlu tanı)

**Ölçülmüş kusur → düzeltme:** geçici bir rclone hatası (yük altında fork/exec
gecikmesi, AV taraması, anlık rc≠0) "rclone yok" sanılıp GDrive kanalı
**sessizce** kapanıyordu; ayrıca pipeline (`rclone lsd … | wc -l`) rc'yi
`wc`/`tail`'den aldığı için gerçek rc kayboluyor ve retry hiç tetiklenmiyordu.

- **Retry politikası:** yalnız **idempotent OKUMA** alt-komutları (`cat`, `lsf`,
  `lsjson`, `lsd`, `status`, `ping`, `listremotes`, `direxists`, `about`,
  `version`) geçici hatada **1 kez** yeniden denenir (3s bekleme). **Yazma
  komutlarına (copy/copyto/moveto/move/sync/delete/serve/mount/…) ASLA retry
  YOK** — çift yazma ve kısmi durum riski fail-closed korunur. Yazma veto'su
  okuma sınıflandırmasından ÖNCE gelir; bu yüzden konumsal argümanı okuma
  sözcüğü olan bir mutasyon da retry almaz (`rclone moveto cat <dest>` RED).
  İki modülün (`sync_motor`, `sync_common_knowledge`) okuma kümesi birebir aynı
  olmak zorundadır (drift kapısı).
- **Zaman aşımı:** veri okumaları 180s; erişilebilirlik yoklaması 60s
  (retry ile en kötü durum ~123s). Yoklamayı kısaltmak yanlış "rclone yok"
  negatifini geri getireceği için bilinçli olarak kısaltılmadı.
- **Üç durumlu tanı:** `rclone_durum()` → `ok` | `yok` (kalıcı: binary yok,
  rc=127, "no such file") | `belirsiz` (geçici: retry tükendi, ağ işareti).
  Karar değişmezi aynı: `ok` değilse GDrive işlemi **atlanır, yazma yapılmaz**
  (fail-closed); ayrım yalnız tanı içindir — `doctor` artık "GDrive remote YOK"
  demek yerine "sorgulanamadı" diyebilir.
- **Log biçimi:** `sync hata: <komut> rc=<rc> <süre>s retry=<n>` — komut, rc,
  süre ve retry sayısı tek satırda.
- **Kapılar:** `tests/test_retry.py` (okuma/yazma sınıflandırma ayrımı, konumsal
  okuma sözcüğü yazmayı retry ettirmez, iki sınıflandırıcı birebir eşit).

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

# 2) Python paketi + CLI kur (synclave / synclave-a2a / synclave-worker)
pip install synclave
pip install uvicorn fastapi       # A2A server için (opsiyonel)

# 2b) rclone + restic — ikisi de Go BINARY'sidir, pip ile KURULMAZ
#     PyPI'daki "rclone" bir Python wrapper'dır (yine rclone CLI'sini ister);
#     PyPI'daki "restic" ise tamamen ALAKASIZ bir REST istemcisidir — yedek
#     aracı DEĞİLDİR. Yanlış kurulum sessizce çalışmayan bir motor bırakır.
winget install Rclone.Rclone      # veya https://rclone.org/downloads/ → PATH
winget install restic.restic      # veya https://restic.net/ → windows_amd64.zip → PATH
rclone version                    # doğrulama (rc=0 beklenir)
restic version

# 3) rclone — GDrive remote (tek seferlik OAuth)
rclone config
#    remote adı: gdrive
#    (client_id paylaşılmışsa "shared client_id" uyarısı — kendi client_id'niz
#     Google Cloud OAuth'da daha hızlı ve 2026 sonrası zorunlu)

# 4) restic — GDrive object store'u mount et (arka plan servisi)
rclone serve restic gdrive:restic-backup --addr 127.0.0.1:8443
#    Motor varsayılan olarak `rest:http://127.0.0.1:8443/` adresine bağlanır
#    (RESTIC_REPOSITORY). Başka addr/port veya uzak yol için üzerine yaz:
#      setx RESTIC_REPO_URL "rest:http://127.0.0.1:9000/my-repo/"
#    Windows: `schtasks /create` veya NSSM ile oturum açılışında başlat

# 5) Syncthing — P2P dosya kanalı (opsiyonel ama önerilir)
winget install Syncthing.Syncthing    # kanonik kimlik (winget-pkgs: s/Syncthing/Syncthing)
#    GUI 127.0.0.1:8384 → H1/H3 cihazları eşleştir (device ID'ler)

# 6) A2A token — H1/H3 ile aynı ortak token'ı .env/ortam değişkenine yaz
setx A2A_TOKEN "test-a2a-mesh-2026"     # kendi ortak değerinizle değiştirin
#    `setx` yalnızca YENİ terminallere işler; görev SYSTEM/başka kullanıcı
#    bağlamında çalışıyorsa token'ı görev tanımında da verin (yoksa görünmez)

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
- `rclone` ve `restic` **Go binary**leridir; kurulum **pip değil**. PyPI'daki
  `rclone` bir wrapper'dır (yine CLI ister), PyPI'daki `restic` ise alakasız bir
  REST istemcisidir — `pip install restic` yedek motorunu KURMAZ. Doğru yol:
  `winget install Rclone.Rclone` / `winget install restic.restic` veya resmi
  binary → PATH (`rclone version`, `restic version` ile doğrula).
  Kapı: `tests/test_readme_kurulum.py`.
- Uzaktan kurulum için hazır betikler: `remote_hermes_setup.ps1` (SSH/Tailscale).
- Adım adım tam prosedür (rclone OAuth, restic, Syncthing, A2A token, Task Scheduler):
  [`docs/windows.md`](docs/windows.md).

### Güvenlik

- `.env`, `*.key`, `*.pem`, token içeren dosyalar ASLA kapsama alınmaz (secret filtre)
- A2A: Bearer token + Tailscale-only (dışa kapalı)
- Görev işleyici: allowlist komutlar (status/uptime/test:<modul>/shell:ls)
- Non-destructive: çakışma `.conflict.TS` korunur, üzerine yazma yok

## Geçmiş

- v2.7 (14-17 Eyl 2026): rclone retry + üç durumlu tanı, retention kapıları,
  build kilidi, audit log yolu tek kaynak, Windows kurulumu düzeltmesi
- v2.6 (12-13 Eyl 2026): Windows ajan kilidi + platform geçici dizinleri,
  retry sınıflandırıcısı ve veto kümesi (ölçülmüş fail-open kapatıldı)
- v2.5 (11-12 Eyl 2026): kopya paritesi (tek kanonik motor + sapma kapısı),
  kilit bütünlüğü, retry politikası iki sınıflandırıcıda hizalandı
- v2.3.2 (10 Eyl 2026): rclone OKUMA retry kapsamı tamamlandı
- v2.3 (30 Ağu 2026): kriptografik kimlik + şifreli A2A mesh + sohbet köprüsü
- v2.1 (29 Ağu 2026): ajan mesh (A2A) + restic + akıllı kurulum
- v1.6 (12 Ağu): akıllı kurulum (probe/propose/apply)
- v1.3 (3 Ağu): evrensel — kullanıcı/makine kimliği, sınırsız node, share
- v1.0 (3 Ağu): GitHub manifest + GDrive versiyonlu + OpenClaw skill

Düzeltme gerekçeleri ve ölçümleri (2.5.0 → 2.7.9): [`CHANGELOG.md`](CHANGELOG.md).
