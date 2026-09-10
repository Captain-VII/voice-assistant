"""Met à jour assistant_core.py depuis un manifeste distant (JSON), si configuré.

En build "source" (python tray_app.py), une mise à jour réécrit directement
assistant_core.py sur disque. En build compilé (.exe PyInstaller), ce fichier
n'existe plus séparément — il est intégré dans l'exécutable — donc on se
contente de signaler qu'une nouvelle version existe, sans tenter de patcher
le binaire en place.

Le code téléchargé est exécuté au démarrage suivant : il est donc exigé en
HTTPS et vérifié contre l'empreinte sha256 annoncée par le manifeste."""

import hashlib
import json
import os
import re
import sys
from urllib.parse import urlparse

import requests

IS_FROZEN = getattr(sys, "frozen", False)

CONFIG_FILE = "update_config.json"
VERSION_FILE = "version.txt"
CORE_FILE = "assistant_core.py"


def _parse_version(version):
    """« 1.5.1 » -> (1, 5, 1), pour comparer par ordre et non par inégalité."""
    parties = [int(m) if m.isdigit() else 0 for m in re.split(r"[.\-+_]", str(version).strip())]
    return tuple(parties) or (0,)


def _is_https(url):
    return urlparse(str(url)).scheme == "https"


def _read_local_version(base_dir):
    path = os.path.join(base_dir, VERSION_FILE)
    if not os.path.exists(path):
        return "0.0.0"
    # utf-8-sig : tolère le BOM ajouté par les éditeurs Windows.
    with open(path, encoding="utf-8-sig") as f:
        return f.read().strip()


def _read_manifest_url(base_dir):
    path = os.path.join(base_dir, CONFIG_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"{CONFIG_FILE} illisible, vérification des mises à jour ignorée : {e}")
        return None
    return data.get("manifest_url") or None


def _write_atomic(path, data):
    """Écrit via un fichier temporaire puis os.replace : une interruption ne
    laisse jamais un assistant_core.py tronqué, donc inimportable."""
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def check_and_update(base_dir):
    """Vérifie le manifeste distant (voir update_config.json).

    Le manifeste attendu est un JSON de la forme :
    {"version": "1.6.0",
     "script_url": "https://.../assistant_core.py",
     "sha256": "<empreinte du fichier servi>"}

    Retourne :
    - False : rien à faire (pas d'URL configurée, déjà à jour, version
      distante plus ancienne, empreinte invalide, ou erreur réseau)
    - True : mise à jour appliquée en place (build source uniquement)
    - remote_version (str) : nouvelle version détectée mais non appliquée
      automatiquement (build .exe compilé — l'utilisateur doit réinstaller)
    """
    manifest_url = _read_manifest_url(base_dir)
    if not manifest_url:
        return False
    if not _is_https(manifest_url):
        print(f"URL de manifeste refusée, HTTPS requis : {manifest_url}")
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

    if not script_url:
        return False
    if _parse_version(remote_version) <= _parse_version(local_version):
        return False

    if IS_FROZEN:
        return remote_version

    if not _is_https(script_url):
        print(f"URL de script refusée, HTTPS requis : {script_url}")
        return False

    empreinte_attendue = str(manifest.get("sha256") or "").strip().lower()
    if not empreinte_attendue:
        print("Manifeste sans empreinte sha256 : mise à jour refusée.")
        return False

    try:
        script_resp = requests.get(script_url, timeout=15)
        script_resp.raise_for_status()
    except requests.exceptions.RequestException:
        return False

    contenu = script_resp.content
    empreinte = hashlib.sha256(contenu).hexdigest()
    if empreinte != empreinte_attendue:
        print(f"Empreinte sha256 invalide (attendu {empreinte_attendue}, obtenu {empreinte}) : "
              "mise à jour refusée.")
        return False

    _write_atomic(os.path.join(base_dir, CORE_FILE), contenu)
    _write_atomic(os.path.join(base_dir, VERSION_FILE), str(remote_version).encode("utf-8"))
    return True
