"""Met à jour assistant_core.py depuis un manifeste distant (JSON), si configuré.

En build "source" (python tray_app.py), une mise à jour réécrit directement
assistant_core.py sur disque. En build compilé (.exe PyInstaller), ce fichier
n'existe plus séparément — il est intégré dans l'exécutable — donc on se
contente de signaler qu'une nouvelle version existe, sans tenter de patcher
le binaire en place."""

import json
import os
import sys

import requests

IS_FROZEN = getattr(sys, "frozen", False)

CONFIG_FILE = "update_config.json"
VERSION_FILE = "version.txt"
CORE_FILE = "assistant_core.py"


def _read_local_version(base_dir):
    path = os.path.join(base_dir, VERSION_FILE)
    if not os.path.exists(path):
        return "0.0.0"
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def _read_manifest_url(base_dir):
    path = os.path.join(base_dir, CONFIG_FILE)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("manifest_url") or None


def check_and_update(base_dir):
    """Vérifie le manifeste distant (voir update_config.json).

    Le manifeste attendu est un JSON de la forme :
    {"version": "1.1.0", "script_url": "https://.../assistant_core.py"}

    Retourne :
    - False : rien à faire (pas d'URL configurée, déjà à jour, ou erreur réseau)
    - True : mise à jour appliquée en place (build source uniquement)
    - remote_version (str) : nouvelle version détectée mais non appliquée
      automatiquement (build .exe compilé — l'utilisateur doit réinstaller)
    """
    manifest_url = _read_manifest_url(base_dir)
    if not manifest_url:
        return False

    try:
        resp = requests.get(manifest_url, timeout=10)
        resp.raise_for_status()
        manifest = resp.json()
    except (requests.exceptions.RequestException, ValueError):
        return False

    remote_version = manifest.get("version", "0.0.0")
    script_url = manifest.get("script_url")
    local_version = _read_local_version(base_dir)

    if not script_url or remote_version == local_version:
        return False

    if IS_FROZEN:
        return remote_version

    try:
        script_resp = requests.get(script_url, timeout=15)
        script_resp.raise_for_status()
    except requests.exceptions.RequestException:
        return False

    core_path = os.path.join(base_dir, CORE_FILE)
    with open(core_path, "w", encoding="utf-8") as f:
        f.write(script_resp.text)

    with open(os.path.join(base_dir, VERSION_FILE), "w", encoding="utf-8") as f:
        f.write(remote_version)

    return True
