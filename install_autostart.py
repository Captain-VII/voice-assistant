"""Ajoute/retire Alfred du démarrage automatique de Windows, via un raccourci
dans le dossier "Démarrage" de l'utilisateur (aucune modification du Registre
ni des paramètres système).

L'installeur crée le même raccourci quand on coche l'option correspondante :
le nom de fichier est volontairement identique pour qu'on ne puisse pas
cumuler deux lancements au démarrage."""

import os
import sys

from win32com.client import Dispatch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SHORTCUT_NAME = "Alfred.lnk"


def _startup_dir():
    return os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _shortcut_path():
    return os.path.join(_startup_dir(), SHORTCUT_NAME)


def _launch_target():
    """(cible, arguments, dossier de travail) selon qu'on tourne depuis
    l'exécutable compilé ou depuis les sources."""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return exe, "", os.path.dirname(exe)

    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable  # fallback si pythonw introuvable
    return pythonw, f'"{os.path.join(BASE_DIR, "tray_app.py")}"', BASE_DIR


def install():
    cible, arguments, dossier = _launch_target()

    shell = Dispatch("WScript.Shell")
    shortcut = shell.CreateShortCut(_shortcut_path())
    shortcut.TargetPath = cible
    shortcut.Arguments = arguments
    shortcut.WorkingDirectory = dossier
    shortcut.IconLocation = cible
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
