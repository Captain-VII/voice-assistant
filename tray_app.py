#!/usr/bin/env python3
"""Icône barre des tâches pour l'assistant vocal : démarrer/arrêter,
vérifier les mises à jour, quitter — sans fenêtre console visible."""

import importlib
import os
import sys
import threading

import pystray
from PIL import Image, ImageDraw

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import updater  # noqa: E402

stop_event = threading.Event()
assistant_thread = None


def _make_dot(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=color)
    return img


ICON_ON = _make_dot((40, 180, 99, 255))
ICON_OFF = _make_dot((150, 150, 150, 255))


def start_assistant(icon, item=None):
    global assistant_thread
    if assistant_thread and assistant_thread.is_alive():
        return
    stop_event.clear()
    import assistant_core
    importlib.reload(assistant_core)
    assistant_thread = threading.Thread(target=assistant_core.run, args=(stop_event,), daemon=True)
    assistant_thread.start()
    icon.icon = ICON_ON
    icon.title = "Alfred - Actif"


def stop_assistant(icon, item=None):
    stop_event.set()
    icon.icon = ICON_OFF
    icon.title = "Alfred - Arrêté"


def toggle_assistant(icon, item):
    if assistant_thread and assistant_thread.is_alive():
        stop_assistant(icon)
    else:
        start_assistant(icon)


def is_active(item):
    return bool(assistant_thread and assistant_thread.is_alive())


def check_updates(icon, item=None):
    result = updater.check_and_update(BASE_DIR)
    if result is True:
        icon.notify("Mise à jour installée, redémarrage de l'assistant...", "Alfred")
        was_active = bool(assistant_thread and assistant_thread.is_alive())
        stop_assistant(icon)
        if was_active:
            start_assistant(icon)
    elif isinstance(result, str):
        icon.notify(
            f"Version {result} disponible. Téléchargez le nouvel installeur sur GitHub pour mettre à jour.",
            "Alfred",
        )
    else:
        icon.notify("Déjà à jour.", "Alfred")


def quit_app(icon, item=None):
    stop_event.set()
    icon.stop()


def main():
    menu = pystray.Menu(
        pystray.MenuItem("Assistant actif", toggle_assistant, checked=is_active),
        pystray.MenuItem("Vérifier les mises à jour", check_updates),
        pystray.MenuItem("Quitter", quit_app),
    )
    icon = pystray.Icon("assistant_vocal", ICON_OFF, "Alfred", menu)

    def setup(icon):
        icon.visible = True
        updater.check_and_update(BASE_DIR)
        start_assistant(icon)

    icon.run(setup)


if __name__ == "__main__":
    main()
