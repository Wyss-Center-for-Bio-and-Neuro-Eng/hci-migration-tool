# HCI Migration Tool

Outil de migration de VMs de Nutanix AHV vers Harvester HCI.

## Fonctionnalités

- **Export** : Extraction des disques VM depuis Nutanix (via acli)
- **Conversion** : RAW → QCOW2 avec compression
- **Import** : Upload vers Harvester (HTTP ou virtctl)
- **Création VM** : Configuration automatique depuis les specs Nutanix
- **Dissociation** : Clonage des volumes pour supprimer la dépendance aux images
- **Windows Tools** : Pre-check, collecte config réseau, post-migration
- **Vault** : Stockage sécurisé des credentials avec `pass`

## Prérequis

### Système (Debian/Ubuntu)

```bash
# Packages système
sudo apt install -y \
    python3 python3-pip \
    qemu-utils \
    sshpass \
    pass gnupg2 \
    krb5-user libkrb5-dev \
    realmd sssd sssd-tools adcli

# Packages Python
pip install pyyaml requests pywinrm[kerberos] --break-system-packages
```

### Jonction au domaine AD (pour Kerberos)

```bash
# Joindre le domaine
sudo realm join -U administrator AD.WYSSCENTER.CH

# Vérifier
realm list
id votre_user@ad.wysscenter.ch
```

### Configuration du Vault (pass)

```bash
# Générer une clé GPG
gpg --batch --gen-key <<EOF
Key-Type: RSA
Key-Length: 4096
Name-Real: HCI Migration Tool
Name-Email: migration@ad.wysscenter.ch
Expire-Date: 0
%no-protection
%commit
EOF

# Initialiser pass
pass init "migration@ad.wysscenter.ch"

# Ajouter les credentials Windows (pour machines hors domaine)
pass insert migration/windows/local-admin
```

### Authentification Kerberos

```bash
# Obtenir un ticket (valide 10h)
kinit votre_admin@AD.WYSSCENTER.CH

# Vérifier le ticket
klist

# Renouveler si expiré
kinit -R
```

## Installation

```bash
# Cloner le repo
git clone https://github.com/Wyss-Center-for-Bio-and-Neuro-Eng/hci-migration-tool.git
cd hci-migration-tool

# Copier et éditer la config
cp config.yaml.example config.yaml
nano config.yaml
```

## Configuration

### config.yaml

```yaml
nutanix:
  prism_ip: "10.16.22.46"
  username: "admin"
  password: "votre_mot_de_passe"

harvester:
  api_url: "https://10.16.16.130:6443"
  token: "votre_token_bearer"
  namespace: "harvester-public"
  verify_ssl: false

transfer:
  staging_mount: "/mnt/data"
  convert_to_qcow2: true
  compress: true

windows:
  domain: "AD.WYSSCENTER.CH"
  use_kerberos: true
  vault_backend: "pass"
  vault_path: "migration/windows"
  winrm_port: 5985
  winrm_transport: "kerberos"
```

### Obtenir le token Harvester

```bash
# Via kubectl sur le cluster Harvester
kubectl -n cattle-system get secret \
  $(kubectl -n cattle-system get sa rancher -o jsonpath='{.secrets[0].name}') \
  -o jsonpath='{.data.token}' | base64 -d
```

## Utilisation

```bash
python3 migrate.py
```

### Menu Principal

```
╔══════════════════════════════════════════════════════════════╗
║              NUTANIX → HARVESTER MIGRATION TOOL              ║
╠══════════════════════════════════════════════════════════════╣
║                         MAIN MENU                            ║
╠══════════════════════════════════════════════════════════════╣
║  1. Nutanix                                                  ║
║  2. Harvester                                                ║
║  3. Migration                                                ║
║  4. Windows Tools                                            ║
║  5. Configuration                                            ║
║  q. Quit                                                     ║
╚══════════════════════════════════════════════════════════════╝
```

## Workflow de Migration

### Migration Standard

1. **Sélectionner la VM source** (Menu 1 → Option 3)
2. **Éteindre la VM source** (Menu 1 → Option 5)
3. **Créer image Nutanix** (via Prism ou acli)
4. **Exporter le disque** (Menu 3 → Option 4)
5. **Convertir RAW → QCOW2** (Menu 3 → Option 5)
6. **Importer dans Harvester** (Menu 3 → Option 6)
7. **Créer la VM** (Menu 3 → Option 7)
8. **Démarrer et tester** (Menu 2 → Option 2)
9. **Dissocier de l'image** (Menu 2 → Option 5) - Optionnel
10. **Cleanup** : Supprimer images et fichiers staging

### Migration Windows (avec reconfiguration réseau)

#### Pré-migration

1. **Télécharger les outils** (Menu 4 → Option 4)
   - virtio-win.iso
   - virtio-win-gt-x64.msi
   - qemu-ga-x86_64.msi

2. **Installer sur la VM source** (manuellement ou via GPO) :
   - VirtIO drivers Fedora/Red Hat
   - QEMU Guest Agent

3. **Collecter la configuration** (Menu 4 → Option 2)
   - Connexion WinRM via Kerberos
   - Collecte : IP, DNS, Gateway, Hostname, Agents
   - Sauvegarde dans `/mnt/data/migrations/<hostname>/vm-config.json`

4. **Vérifier la préparation** (Menu 4 → Option 3)
   - Affiche le JSON de configuration
   - Indique les prérequis manquants

#### Post-migration

1. **Démarrer la VM migrée** sur Harvester
2. **Générer le script de reconfiguration** (Menu 4 → Option 5)
3. **Appliquer la configuration** :
   - Via console VNC : exécuter le script PowerShell généré
   - Ou via WinRM si accessible

## Menus Détaillés

### Menu Nutanix (1)

| Option | Description |
|--------|-------------|
| 1 | Lister les VMs |
| 2 | Détails d'une VM |
| 3 | Sélectionner une VM |
| 4 | Démarrer une VM |
| 5 | Éteindre une VM |
| 6 | Lister les images |
| 7 | Supprimer une image |

### Menu Harvester (2)

| Option | Description |
|--------|-------------|
| 1 | Lister les VMs |
| 2 | Démarrer une VM |
| 3 | Éteindre une VM |
| 4 | Supprimer une VM |
| 5 | **Dissocier VM de l'image** |
| 6 | Lister les images |
| 7 | Supprimer une image |
| 8 | Lister les volumes |
| 9 | Supprimer un volume |
| 10 | Lister les réseaux |
| 11 | Lister les storage classes |

### Menu Migration (3)

| Option | Description |
|--------|-------------|
| 1 | Vérifier le staging |
| 2 | Lister les disques staging |
| 3 | Détails d'une image disque |
| 4 | Exporter VM (Nutanix → Staging) |
| 5 | Convertir RAW → QCOW2 |
| 6 | **Importer image dans Harvester** (HTTP ou Upload) |
| 7 | Créer VM dans Harvester |
| 8 | Supprimer fichier staging |
| 9 | Migration complète |

### Menu Windows Tools (4)

| Option | Description |
|--------|-------------|
| 1 | Vérifier WinRM/Prérequis |
| 2 | Pre-migration check (collecter config) |
| 3 | Voir la configuration VM |
| 4 | Télécharger virtio/qemu-ga |
| 5 | Générer script post-migration |
| 6 | Gestion du Vault |

## Dissociation des Images

### Problème

Harvester utilise des "backing images" pour le thin provisioning. Les volumes créés depuis une image restent liés à celle-ci, empêchant sa suppression.

### Solution

L'option "Dissocier VM de l'image" (Menu 2 → Option 5) :

1. Clone le(s) volume(s) de la VM via CSI
2. Met à jour la VM pour utiliser les clones
3. Supprime les anciens volumes
4. L'image peut maintenant être supprimée

```
Avant:  VM → Volume → Backing Image (lié)
Après:  VM → Volume Clone (indépendant)
```

## Structure du Staging

```
/mnt/data/
├── tools/                          # Outils à déployer
│   ├── virtio-win.iso
│   ├── virtio-win-gt-x64.msi
│   └── qemu-ga-x86_64.msi
│
├── migrations/                     # Configs par VM
│   └── <hostname>/
│       ├── vm-config.json          # Config collectée
│       └── reconfig-network.ps1    # Script post-migration
│
├── <vm>-disk0.raw                  # Disques exportés
└── <vm>-disk0.qcow2                # Disques convertis
```

## Format vm-config.json

```json
{
  "collected_at": "2025-12-08T10:30:00Z",
  "source_platform": "nutanix",
  "system": {
    "hostname": "SRV-APP01",
    "os_name": "Microsoft Windows Server 2019 Standard",
    "os_version": "10.0.17763",
    "architecture": "64-bit",
    "domain": "AD.WYSSCENTER.CH",
    "domain_joined": true
  },
  "network": {
    "interfaces": [
      {
        "name": "Ethernet0",
        "mac": "50:6b:8d:aa:bb:cc",
        "dhcp": false,
        "ip": "10.16.20.50",
        "prefix": 24,
        "gateway": "10.16.20.1",
        "dns": ["10.16.20.10", "10.16.20.11"]
      }
    ]
  },
  "agents": {
    "ngt_installed": true,
    "virtio_fedora": false,
    "qemu_guest_agent": false
  },
  "migration_ready": false,
  "missing_prerequisites": ["virtio_fedora", "qemu_guest_agent"]
}
```

## Dépannage

### Erreur WinRM "Access Denied"

```bash
# Vérifier le ticket Kerberos
klist

# Renouveler si expiré
kinit votre_admin@AD.WYSSCENTER.CH
```

### Erreur "422 Unprocessable Entity" sur Harvester

Les opérations start/stop utilisent l'API subresources de KubeVirt :
```
PUT /apis/subresources.kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}/start
PUT /apis/subresources.kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}/stop
```

### Image Harvester ne peut pas être supprimée

L'image est utilisée par un volume (backing image). Solutions :
1. Utiliser "Dissocier VM de l'image" (Menu 2 → Option 5)
2. Ou supprimer manuellement : VM → Volume → Image

### Vault "Connection timed out"

Vérifier que `pass` est initialisé avec la bonne clé GPG :
```bash
pass init "migration@ad.wysscenter.ch"
```

## Sécurité

- Les credentials Nutanix sont stockés en clair dans `config.yaml`
- Les credentials Windows sont stockés chiffrés dans le vault `pass`
- Les tickets Kerberos expirent après 10h (configurable)
- Utilisez des comptes de service avec privilèges minimaux

## Licence

MIT License - Wyss Center for Bio and Neuro Engineering

## Contributeurs

- Infrastructure Team @ Wyss Center
