# Synclave — Windows Kurulumu (H2 / Windows 10-11)

Bu belgedeki komut yüzeyi kaynak koddan doğrulanmıştır
(`synclave/cli.py`, `synclave/sync_motor.py` argparse `choices`, `synclave/node_agent.py`).
Kod tarafı Windows davranışı ortam-mock'lu testlerle kapsanır
(`tests/test_windows_uyum.py`: kilit yolu, path kurulumu, `a2a_cli`).
Windows makinede ilk kurulumda çıkan gerçek çıktıyı bu belgeye işleyin —
`(yakında)` yerine kanıt yazılır.

## 0) Ön koşullar

- Windows 10/11 (x64), PowerShell 5+ veya Windows Terminal
- Python 3.10+ — python.org kurulumunda **"Add python.exe to PATH"** işaretle
- git
- Tailscale (mesh düğümleri arası özel ağ)

```powershell
python --version; git --version; tailscale status
```

## 1) Synclave (pip)

```powershell
pip install --upgrade synclave
synclave version
```

A2A sunucusu için (opsiyonel — kurulmazsa sunucu net hata verip çıkar, traceback basmaz):

```powershell
pip install uvicorn fastapi
```

## 2) rclone (GDrive)

rclone tek dosyalık Go programıdır; **pip bu aracın resmi dağıtım yolu değildir** — binary kur:

```powershell
winget search rclone          # çıkan ID ile: winget install <ID>
# alternatif: choco install rclone | scoop install rclone
# alternatif: https://rclone.org/downloads/ (zip → PATH'e ekle)
rclone version
```

GDrive remote (tek seferlik OAuth):

```powershell
rclone config
#  n → name: gdrive → storage: drive
#  client_id / client_secret: boş geçilebilir; "shared client_id" uyarısı çıkarsa
#  Google Cloud OAuth ile kendi client_id'ni üret (shared client_id emekliye ayrılıyor)
#  scope: drive ; root_folder_id: boş ; auto config: y
rclone lsd gdrive:            # erişim doğrulaması
```

## 3) restic (yedek motoru)

restic de Go programıdır; binary kur:

```powershell
winget search restic          # çıkan ID ile: winget install <ID>
# alternatif: choco install restic | scoop install restic
# alternatif: https://restic.net/ → Releases → windows_amd64.zip → PATH
restic version
```

GDrive object store'u yerel uç nokta olarak sun (arka plan):

```powershell
rclone serve restic gdrive:restic-backup --addr 127.0.0.1:8443
```

Oturum açılışında başlatmak için Task Scheduler (`schtasks /create ...`) veya NSSM.

## 4) Syncthing (P2P dosya kanalı — opsiyonel, önerilir)

```powershell
winget install syncthing.syncthing
# GUI: http://127.0.0.1:8384 → H1/H3 cihaz ID'lerini eşleştir (22000/tcp)
```

## 5) A2A token (mesh kimliği)

```powershell
setx A2A_TOKEN "<ortak-token>"
# yeni terminal aç → $env:A2A_TOKEN
```

`cmd_mesh`, token'ı ortamdan bulamazsa `~/.hermes/.env` içindeki `A2A_TOKEN=` satırına bakar.

## 6) İlk çalıştırma

```powershell
synclave init          # manifest + GDrive snapshot (argümansız)
synclave doctor        # rclone/gh/restic erişim tanısı
synclave both          # push + pull
synclave mesh status   # A2A düğüm durumu (A2A 8643)
```

## 7) Otomatik çalıştırma (90 dk)

```powershell
schtasks /create /tn "Synclave" /sc minute /mo 90 ^
  /tr "python -m synclave.node_agent once"
```

Hazır uzaktan kurulum betiği: `remote_hermes_setup.ps1` (SSH/Tailscale üzerinden).

## Windows notları (kod ile doğrulanmış)

- İki kilit dosyası vardır (v2.6.0): `%TEMP%\cumulus_sync.lock` (motor,
  `msvcrt.locking`) ve `%TEMP%\cumulus_node_agent.lock` (otonom ajan). POSIX'te
  aynı adlar `/tmp` altındadır (`fcntl.flock`). Ajan kilidi doluysa hub raporu
  adımı ATLANIR (koşu düşmez, bekleme yok); motor kilidi ayrı dosyadır —
  ajan kilit tutarken `sync_motor` alt-süreci kendi kilidini alabilir.
- Kilit kapsamı yalnızca aynı makinedir; farklı makineler makineye özel
  `status.json` yazar (paylaşımlı değiştirilebilir dosya yok).
- Yollar `os.path.join` ile kurulur; geçici dizinler `_platform_temp_dir()`
  ile platformdan türetilir (sabit `/` ayracı ve `/tmp` varsayımı yoktur).
- `a2a_cli.py` yalnızca `urllib` kullanır → ek bağımlılık gerekmez.
- A2A sunucusu `uvicorn` bulamazsa net hata mesajı basar ve çıkar.
- `tests/manual/` elle koşulan betiklerdir; `pytest tests/` bunları toplamaz
  (`norecursedirs`), aksi halde `SystemExit` tüm paketi düşürür.

## Doğrulama

```powershell
python -m pytest tests/ -q     # kaynak kurulumda: tüm testler PASS (mevcut: 286)
synclave doctor                # rclone remote + gh + restic kontrolü
```

Bilinen sınır: uzak GDrive erişimi için `rclone config` + Tailscale gerekir;
GDrive'a yazma bu belgede test edilmez (üretim verisine dokunulmaz).
