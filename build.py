"""Construit l'exécutable Alfred puis l'installeur, de façon reproductible.

    python build.py

Sans ce script, la sortie de PyInstaller et les fichiers attendus par
l'installeur devaient être assemblés à la main, et un simple oubli produisait
un installeur sans version.txt ni update_config.json.
"""

import os
import shutil
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ISCC = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"
)


def _stop_running_app():
    """Un Alfred.exe en cours verrouille dist\\Alfred et fait échouer le build."""
    subprocess.run(["taskkill", "/IM", "Alfred.exe", "/F"], capture_output=True)


def _clean():
    for dossier in ("build", "dist"):
        shutil.rmtree(os.path.join(BASE_DIR, dossier), ignore_errors=True)


def build_exe():
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            "--onedir", "--windowed",
            "--icon", os.path.join("assets", "alfred.ico"),
            "--name", "Alfred",
            "--collect-data", "whisper",
            # Le monogramme sert d'icône dans la barre des tâches : il doit
            # être embarqué, sinon on retombe sur une pastille unie.
            "--add-data", f"{os.path.join('assets', 'alfred.png')}{os.pathsep}assets",
            "--noconfirm",
            "tray_app.py",
        ],
        cwd=BASE_DIR,
        check=True,
    )


def build_installer():
    if not os.path.exists(ISCC):
        print(f"ISCC.exe introuvable ({ISCC}) : installeur non compilé.")
        print("Installer Inno Setup : winget install --id JRSoftware.InnoSetup -e")
        return False
    subprocess.run([ISCC, "installer.iss"], cwd=BASE_DIR, check=True)
    return True


if __name__ == "__main__":
    _stop_running_app()
    _clean()
    build_exe()
    print("\nExécutable : " + os.path.join(BASE_DIR, "dist", "Alfred", "Alfred.exe"))
    if build_installer():
        print("Installeur : " + os.path.join(BASE_DIR, "installer_output", "Alfred_Setup.exe"))
