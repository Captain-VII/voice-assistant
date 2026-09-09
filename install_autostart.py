"""Ajoute/retire l'assistant vocal du démarrage automatique de Windows,
via un raccourci dans le dossier "Démarrage" de l'utilisateur (aucune
modification du Registre ni des paramètres système)."""

import os
import sys

from win32com.client import Dispatch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SHORTCUT_NAME = "AssistantVocal.lnk"


def _startup_dir():
    return os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _shortcut_path():
    return os.path.join(_startup_dir(), SHORTCUT_NAME)


def install():
    tray_script = os.path.join(BASE_DIR, "tray_app.py")
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable  # fallback si pythonw introuvable

    shell = Dispatch("WScript.Shell")
    shortcut = shell.CreateShortCut(_shortcut_path())
    shortcut.TargetPath = pythonw
    shortcut.Arguments = f'"{tray_script}"'
    shortcut.WorkingDirectory = BASE_DIR
    shortcut.IconLocation = pythonw
    shortcut.Save()
    print(f"Démarrage automatique activé : {_shortcut_path()}")


def uninstall():
    path = _shortcut_path()
    if os.path.exists(path):
        os.remove(path)
        print("Démarrage automatique désactivé.")
    else:
        print("Aucun raccourci de démarrage trouvé.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "uninstall":
        uninstall()
    else:
        install()
