"""Met à jour assistant_core.py depuis un manifeste distant (JSON), si configuré."""

import json
import os

import requests

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
    """Vérifie le manifeste distant (voir update_config.json) et remplace
    assistant_core.py si une version plus récente est disponible.

    Le manifeste attendu est un JSON de la forme :
    {"version": "1.1.0", "script_url": "https://.../assistant_core.py"}

    Retourne True si une mise à jour a été appliquée, False sinon (pas
    d'URL configurée, déjà à jour, ou erreur réseau).
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
