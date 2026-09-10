#!/usr/bin/env python3
"""Icône barre des tâches pour Alfred : démarrer/arrêter, vérifier les mises
à jour, quitter — sans fenêtre console visible."""

import importlib
import os
import sys
import tempfile
import threading
import traceback


def _data_dir():
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    chemin = os.path.join(base, "Alfred")
    os.makedirs(chemin, exist_ok=True)
    return chemin


LOG_PATH = os.path.join(_data_dir(), "alfred.log")
MAX_LOG_BYTES = 1_000_000


def _redirect_streams():
    """En build --windowed, sys.stdout et sys.stderr valent None. Sans cette
    redirection, tout diagnostic est perdu, et surtout la barre de
    progression tqdm du téléchargement de Whisper lève AttributeError
    (None.write) — ce qui tuait l'assistant au premier lancement sur une
    machine où le modèle n'était pas encore en cache."""
    if sys.stdout is not None and sys.stderr is not None:
        return

    mode = "a"
    try:
        if os.path.getsize(LOG_PATH) > MAX_LOG_BYTES:
            mode = "w"
    except OSError:
        pass

    flux = open(LOG_PATH, mode, encoding="utf-8", errors="replace", buffering=1)
    if sys.stdout is None:
        sys.stdout = flux
    if sys.stderr is None:
        sys.stderr = flux


_redirect_streams()

import pystray  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import updater  # noqa: E402

MUTEX_NAME = "AlfredVoiceAssistant-SingleInstance"
STOP_TIMEOUT = 60  # listen() puis Ollama peuvent retenir le thread ~40 s

stop_event = threading.Event()
_quit_event = threading.Event()
assistant_thread = None
_wanted_active = False
_needs_reload = False
_last_error = None
_mutex_handle = None


def _make_dot(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=color)
    return img


ICON_ON = _make_dot((40, 180, 99, 255))
ICON_OFF = _make_dot((150, 150, 150, 255))


def _set_icon(icon, actif):
    icon.icon = ICON_ON if actif else ICON_OFF
    icon.title = "Alfred - Actif" if actif else "Alfred - Arrêté"


def _notify(icon, message):
    print(message)
    try:
        icon.notify(message, "Alfred")
    except Exception:
        traceback.print_exc()


def _acquire_single_instance():
    """Empêche deux Alfred simultanés : deux instances enregistreraient le
    micro en parallèle et exécuteraient chaque commande deux fois."""
    global _mutex_handle
    try:
        import win32api
        import win32event
        import winerror
    except ImportError:
        return True

    _mutex_handle = win32event.CreateMutex(None, False, MUTEX_NAME)
    return win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS


def _assistant_target():
    """Exécute la boucle de l'assistant en mémorisant la cause d'un arrêt
    imprévu, pour que le chien de garde puisse la signaler."""
    global _needs_reload, _last_error
    try:
        import assistant_core
        if _needs_reload:
            # Uniquement après une mise à jour appliquée : un reload
            # systématique remettrait _whisper_model à None et rechargerait
            # 145 Mo de modèle à chaque bascule.
            importlib.reload(assistant_core)
            _needs_reload = False
        assistant_core.run(stop_event)
    except BaseException as e:
        _last_error = f"{type(e).__name__}: {e}"
        traceback.print_exc()


def start_assistant(icon, item=None):
    global assistant_thread, _wanted_active, _last_error
    if assistant_thread and assistant_thread.is_alive():
        return
    _last_error = None
    stop_event.clear()
    assistant_thread = threading.Thread(target=_assistant_target, daemon=True)
    assistant_thread.start()
    _wanted_active = True
    _set_icon(icon, True)


def stop_assistant(icon, item=None):
    global _wanted_active
    _wanted_active = False
    stop_event.set()
    _set_icon(icon, False)


def _stop_and_wait():
    """Arrête l'assistant et attend la fin réelle du thread. Sans ce join, un
    start_assistant() enchaîné repartait alors que l'ancien thread tournait
    encore : il sortait en early-return et laissait stop_event armé, donc
    l'assistant restait éteint."""
    global assistant_thread, _wanted_active
    _wanted_active = False
    stop_event.set()
    thread = assistant_thread
    if thread and thread.is_alive():
        thread.join(timeout=STOP_TIMEOUT)
    assistant_thread = None


def toggle_assistant(icon, item):
    if _wanted_active or (assistant_thread and assistant_thread.is_alive()):
        stop_assistant(icon)
    else:
        start_assistant(icon)


def is_active(item):
    return bool(assistant_thread and assistant_thread.is_alive())


def _check_updates_target(icon):
    global _needs_reload
    try:
        result = updater.check_and_update(BASE_DIR)
    except Exception as e:
        traceback.print_exc()
        _notify(icon, f"Vérification des mises à jour impossible : {e}")
        return

    if result is True:
        _needs_reload = True
        _notify(icon, "Mise à jour installée, redémarrage de l'assistant...")
        etait_actif = _wanted_active or bool(assistant_thread and assistant_thread.is_alive())
        _stop_and_wait()
        _set_icon(icon, False)
        if etait_actif:
            start_assistant(icon)
    elif isinstance(result, str):
        _notify(icon, f"Version {result} disponible. Téléchargez le nouvel installeur sur GitHub.")
    else:
        _notify(icon, "Déjà à jour.")


def check_updates(icon, item=None):
    # En tâche de fond : la requête réseau et l'attente du thread bloqueraient
    # sinon le menu de l'icône pendant plusieurs dizaines de secondes.
    threading.Thread(target=_check_updates_target, args=(icon,), daemon=True).start()


def _startup_update_check():
    global _needs_reload
    try:
        if updater.check_and_update(BASE_DIR) is True:
            _needs_reload = True
    except Exception:
        traceback.print_exc()


def _watchdog(icon):
    """Signale la mort du thread assistant. Sans cela, une exception dans
    run() (modèle Whisper indisponible, micro absent, TTS cassé) laissait
    l'icône au vert et l'utilisateur sans aucune indication."""
    global _wanted_active
    while not _quit_event.wait(2):
        if _wanted_active and not (assistant_thread and assistant_thread.is_alive()):
            _wanted_active = False
            _set_icon(icon, False)
            if _last_error:
                _notify(icon, f"Alfred s'est arrêté ({_last_error}). Détails : {LOG_PATH}")
            else:
                _notify(icon, "Alfred s'est arrêté.")


def quit_app(icon, item=None):
    global _wanted_active
    _wanted_active = False
    _quit_event.set()
    stop_event.set()
    icon.stop()


def main():
    if not _acquire_single_instance():
        print("Une autre instance d'Alfred est déjà en cours d'exécution, arrêt.")
        return

    menu = pystray.Menu(
        pystray.MenuItem("Assistant actif", toggle_assistant, checked=is_active),
        pystray.MenuItem("Vérifier les mises à jour", check_updates),
        pystray.MenuItem("Quitter", quit_app),
    )
    icon = pystray.Icon("alfred", ICON_OFF, "Alfred", menu)

    def setup(icon):
        icon.visible = True
        threading.Thread(target=_watchdog, args=(icon,), daemon=True).start()
        _startup_update_check()
        start_assistant(icon)

    icon.run(setup)


if __name__ == "__main__":
    main()
