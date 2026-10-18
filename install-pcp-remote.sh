# ==============================================================================
# INSTALLATION DU PONT BLE HID (piCorePlayer)
# ==============================================================================

# --- 1a. Dépendances --------------------------------------------------------
tce-load -wi python3.11-bleak.tcz
# Note : uinput est déjà chargé automatiquement par l'extension pcp-bt
# Vérification possible : lsmod | grep uinput


# --- 1b. Télécharger le pont ------------------------------------------------
cd /home/tc
wget -O /home/tc/hid_ble_bridge.py https://github.com/melomane63/LyBriCa/blob/main/hid_ble_bridge.py
#wget -O /home/tc/hid_ble_bridge.py https://github.com/paul-1/pcp-ble-gatt/raw/refs/heads/main/hid_ble_bridge.py
wget -O /home/tc/le_auto_pair.py https://github.com/paul-1/pcp-ble-gatt/raw/refs/heads/main/le_auto_pair.py
chmod 755 /home/tc/hid_ble_bridge.py /home/tc/le_auto_pair.py


# --- 1c. Appairer la télécommande (SSH uniquement, PAS via interface web) ---
/home/tc/le_auto_pair.py --device-name "HID Remote01"
bluetoothctl info FF:23:10:10:21:25 | grep -E "Paired|Bonded"
# Attendu : Paired: yes, Bonded: yes


# --- 1d. Test manuel --------------------------------------------------------
sudo -E python3 /home/tc/hid_ble_bridge.py --device-mac FF:23:10:10:21:25 -vv
# Dans une autre session :
sudo python3 -m evdev.evtest
# Appui long Volume+ : value 1 → value 2 (500ms) → value 2 (toutes les 40ms) → value 0


# ==============================================================================
# MODIFICATION DU PONT (nouveau fichier /home/tc/hid_ble_bridge.py)
# ==============================================================================
# Éditer les constantes en haut du fichier :
#   REPEAT_DELAY = 0.5      # secondes avant le 1er repeat
#   REPEAT_PERIOD = 0.04    # secondes entre chaque repeat (40 ms)
#
# Après modification :
#python3 -m py_compile /home/tc/hid_ble_bridge.py && echo "Syntaxe OK"
pcp bu
#
# Restaurer la version d'origine si besoin :
# wget -O /home/tc/hid_ble_bridge.py https://github.com/paul-1/pcp-ble-gatt/raw/refs/heads/main/hid_ble_bridge.py


# ==============================================================================
# DÉMARRAGE AUTOMATIQUE
# ==============================================================================

# --- 3a. Créer /home/tc/start_remote.sh -------------------------------------
cat > /home/tc/start_remote.sh << 'EOF'
#!/bin/sh
LOG=/home/tc/pcp_hidbridge.log

# 1. Arrêter le service officiel (avec vérification)
echo "$(date): Arrêt du service hidbridge officiel..." >> $LOG
if ps | grep -v grep | grep -q "hid_ble_bridge.py"; then
    echo "$(date): Service actif, arrêt en cours..." >> $LOG
    /usr/local/etc/init.d/hidbridge stop FF:23:10:10:21:25 2>/dev/null
    sudo pkill -f "hid_ble_bridge.py --device-mac" 2>/dev/null
else
    echo "$(date): Aucun service hidbridge actif" >> $LOG
fi

# 1b. Attente active de l'arrêt (max 5s)
for i in $(seq 1 10); do
    if ! ps | grep -v grep | grep -q "hid_ble_bridge.py"; then
        echo "$(date): Processus arrêtés après $((i/2))s" >> $LOG
        break
    fi
    sleep 0.5
done

# 2. Attendre que le contrôleur Bluetooth soit prêt (max 60s)
echo "$(date): Attente du contrôleur Bluetooth..." >> $LOG
for i in $(seq 1 30); do
    if bluetoothctl show 2>/dev/null | grep -q "Powered: yes"; then
        echo "$(date): Contrôleur prêt après $((i*2))s" >> $LOG
        break
    fi
    sleep 2
done

# 3. Attendre que la télécommande soit connectée (max 3 min)
#    Essai immédiat (i=1), puis toutes les 6s (i%3==1)
echo "$(date): Attente de la télécommande..." >> $LOG
CONNECTED=0
for i in $(seq 1 90); do
    if bluetoothctl info FF:23:10:10:21:25 2>/dev/null | grep -q "Connected: yes"; then
        echo "$(date): Télécommande connectée après $((i*2))s" >> $LOG
        CONNECTED=1
        break
    fi
    if [ $((i % 3)) -eq 1 ]; then
        echo "$(date): Tentative de connexion (essai $((i/3+1)))..." >> $LOG
        bluetoothctl connect FF:23:10:10:21:25 >> $LOG 2>&1
    fi
    sleep 2
done
if [ $CONNECTED -eq 0 ]; then
    echo "$(date): ÉCHEC - télécommande non connectée après 3 min" >> $LOG
fi
sleep 1

# 4. Lancer le pont en arrière-plan
echo "$(date): Lancement du pont BLE patché" >> $LOG
nohup sudo -E python3 /home/tc/hid_ble_bridge.py --device-mac FF:23:10:10:21:25 -vv > /home/tc/bridge_stdout.log 2>&1 &
EOF
chmod +x /home/tc/start_remote.sh
sh -n /home/tc/start_remote.sh && echo "Syntaxe OK"


# --- 3b. Tester le script manuellement --------------------------------------
sudo pkill -f hid_ble_bridge
/home/tc/start_remote.sh
sleep 10
cat /home/tc/pcp_hidbridge.log | tail -10
ps | grep -v grep | grep hid_ble_bridge


# --- 3c. Configurer User Commands (interface web) ---------------------------
# Main Page > Tweaks > User Commands
# Saisir :   /home/tc/start_remote.sh
# Cliquer Save
#

# --- 3d. Persister et rebooter ----------------------------------------------
pcp bu
sudo reboot


# --- 3e. Vérifier après reboot (attendre 2-3 min) ---------------------------
cat /home/tc/pcp_hidbridge.log | tail -15
ps | grep -v grep | grep hid_ble_bridge
bluetoothctl info FF:23:10:10:21:25 | grep Connected
#
# Attendu :
#   ...: Service actif, arrêt en cours...   (ou "Aucun service actif")
#   ...: Processus arrêtés après Xs
#   ...: Contrôleur prêt après 2s
#   ...: Tentative de connexion (essai 1)...
#   Connection successful
#   ...: Télécommande connectée après 4s
#   ...: Lancement du pont BLE patché


# ==============================================================================
# RÉCAPITULATIF DES FICHIERS
# ==============================================================================
# /home/tc/hid_ble_bridge.py   → Pont BLE (auto-repeat intégré)      [wget]
# /home/tc/le_auto_pair.py     → Appairage BLE                       [wget]
# /home/tc/start_remote.sh     → Attente BT + lancement pont         [cat > EOF]
# /home/tc/pcp_hidbridge.log   → Log du script                       [généré]
# /home/tc/bridge_stdout.log   → Log du pont                         [généré]
#
# CONFIGURATION SYSTÈME :
#   User Command    : /home/tc/start_remote.sh
#   Interface web   : Bluetooth On (contrôleur activé)
#   Interface web   : NE PAS appairer la télécommande via le web
#   pcp bu          : après chaque modification dans /home/tc/
# ==============================================================================
