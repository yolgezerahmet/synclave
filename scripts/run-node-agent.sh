#!/bin/bash
# run-node-agent.sh — node_agent otonom koşusu (no_agent cron, sessiz-OK)
#
# DIŞ KAPI: Hermes cron bu script'i 3600s'te ÖLDÜRÜR. Bu yüzden
#   (1) `python3 -u` — stdout satır tamponlu: öldürülse bile log'da son adım kalır
#       (öncesi: dosyaya yönlendirilen blok tamponlu çıktı → BOŞ log + teşhis yok;
#        11 Eyl 2026'da 14 ardışık başarısız koşu bu yüzden teşhis edilemedi),
#   (2) node_agent adım bütçeleri (BUTCE_*, toplam 3240s) bu kapının ALTINDA —
#       aşan adım rc=-1 ile RAPOR EDİLİR, koşu rapor adımına devam eder.
cd /root/cumulus-sync-motor || exit 1
if python3 -u node_agent.py once > /tmp/node_agent.log 2>&1; then
    exit 0   # sessiz
else
    echo "NODE AGENT HATA — log: /tmp/node_agent.log"
    tail -8 /tmp/node_agent.log
fi
