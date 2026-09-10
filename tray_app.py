#!/usr/bin/env python3
"""Icône barre des tâches pour Alfred : état, choix de la voix, réglages,
mises à jour — sans fenêtre console visible."""

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

# Voix masculines capables de lire du français (edge-tts).
VOIX = [
    ("Remy — France", "fr-FR-RemyMultilingualNeural"),
    ("Henri — France", "fr-FR-HenriNeural"),
    ("Gérard — Belgique", "fr-BE-GerardNeural"),
    ("Fabrice — Suisse", "fr-CH-FabriceNeural"),
    ("Thierry — Québec", "fr-CA-ThierryNeural"),
    ("Jean — Québec", "fr-CA-JeanNeural"),
    ("Antoine — Québec", "fr-CA-AntoineNeural"),
    ("Andrew — accent anglais", "en-US-AndrewMultilingualNeural"),
    ("William — accent australien", "en-AU-WilliamMultilingualNeural"),
]

# Le ton n'est volontairement pas proposé : tout décalage de pitch par
# edge-tts s'entend comme un traitement artificiel. La gravité se règle au
# débit.
DEBITS = [
    ("Normal", "+0%"),
    ("Posé", "-5%"),
    ("Lent", "-10%"),
    ("Très lent", "-15%"),
]

stop_event = threading.Event()
_quit_event = threading.Event()
assistant_thread = None
_wanted_active = False
_needs_reload = False
_last_error = None
_mutex_handle = None


# ============ ICÔNE ============
def _asset_path(nom):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", nom)


def _monogramme():
    try:
        return Image.open(_asset_path("alfred.png")).convert("RGBA")
    except Exception:
        return None


_BASE_ICON = _monogramme()


def _icone(actif):
    """Monogramme d'Alfred avec une pastille de statut incrustée : vert quand
    il écoute, gris quand il est à l'arrêt."""
    couleur = (40, 180, 99, 255) if actif else (150, 150, 150, 255)

    if _BASE_ICON is None:  # repli si l'asset n'a pas été empaqueté
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((8, 8, 56, 56), fill=couleur)
        return img

    img = _BASE_ICON.copy().resize((64, 64), Image.LANCZOS)
    dessin = ImageDraw.Draw(img)
    dessin.ellipse((38, 38, 62, 62), fill=couleur, outline=(20, 20, 24, 255), width=3)
    return img


ICON_ON = _icone(True)
ICON_OFF = _icone(False)


def _set_icon(icon, actif):
    icon.icon = ICON_ON if actif else ICON_OFF
    icon.title = f"Alfred — {'à l’écoute' if actif else 'arrêté'}"


def _notify(icon, message):
    print(message)
    try:
        icon.notify(message, "Alfred")
    except Exception:
        traceback.print_exc()


# ============ ÉTAT ============
def _est_vivant():
    return bool(assistant_thread and assistant_thread.is_alive())


def _texte_statut(item=None):
    if _est_vivant():
        return "Alfred — à l’écoute"
    if _last_error:
        return "Alfred — arrêté sur erreur"
    return "Alfred — arrêté"


def _version():
    return updater._read_local_version(BASE_DIR)


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


# ============ CYCLE DE VIE DE L'ASSISTANT ============
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
    if _est_vivant():
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
    if _wanted_active or _est_vivant():
        stop_assistant(icon)
    else:
        start_assistant(icon)


def is_active(item):
    return _est_vivant()


# ============ VOIX ET DÉBIT ============
def _config():
    import assistant_core
    return assistant_core.CONFIG


def _voix_courante():
    try:
        return _config()["edge_voice"]
    except Exception:
        return None


def _debit_courant():
    try:
        return _config()["edge_rate"]
    except Exception:
        return None


def _choisir_voix(identifiant, libelle):
    def action(icon, item):
        threading.Thread(
            target=_appliquer_voix, args=(icon, identifiant, libelle), daemon=True
        ).start()
    return action


def _appliquer_voix(icon, identifiant, libelle):
    try:
        import assistant_core
        assistant_core.save_settings({"edge_voice": identifiant})
        # Un échantillon immédiat, pour juger la voix sans relancer.
        assistant_core.speak("Très bien, monsieur. Voici ma nouvelle voix.")
    except Exception as e:
        traceback.print_exc()
        _notify(icon, f"Changement de voix impossible : {e}")


def _choisir_debit(valeur, libelle):
    def action(icon, item):
        threading.Thread(
            target=_appliquer_debit, args=(icon, valeur, libelle), daemon=True
        ).start()
    return action


def _appliquer_debit(icon, valeur, libelle):
    try:
        import assistant_core
        assistant_core.save_settings({"edge_rate": valeur})
        assistant_core.speak(f"Débit réglé sur {libelle.lower()}, monsieur.")
    except Exception as e:
        traceback.print_exc()
        _notify(icon, f"Changement de débit impossible : {e}")


def _menu_voix():
    return pystray.Menu(*[
        pystray.MenuItem(
            libelle,
            _choisir_voix(identifiant, libelle),
            radio=True,
            checked=lambda item, ident=identifiant: _voix_courante() == ident,
        )
        for libelle, identifiant in VOIX
    ])


def _menu_debit():
    return pystray.Menu(*[
        pystray.MenuItem(
            libelle,
            _choisir_debit(valeur, libelle),
            radio=True,
            checked=lambda item, v=valeur: _debit_courant() == v,
        )
        for libelle, valeur in DEBITS
    ])


# ============ DÉMARRAGE AVEC WINDOWS ============
def _startup_shortcut():
    return os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "Alfred.lnk",
    )


def _demarrage_actif(item=None):
    return os.path.exists(_startup_shortcut())


def toggle_demarrage(icon, item):
    try:
        import install_autostart
        if _demarrage_actif():
            install_autostart.uninstall()
            _notify(icon, "Alfred ne démarrera plus avec Windows.")
        else:
            install_autostart.install()
            _notify(icon, "Alfred démarrera avec Windows.")
    except Exception as e:
        traceback.print_exc()
        _notify(icon, f"Modification du démarrage impossible : {e}")


# ============ FICHIERS ============
def _ouvrir(icon, chemin, libelle):
    try:
        if not os.path.exists(chemin):
            _notify(icon, f"{libelle} introuvable : {chemin}")
            return
        os.startfile(chemin)  # noqa: S606 - ouverture avec l'app par défaut
    except Exception as e:
        traceback.print_exc()
        _notify(icon, f"Ouverture impossible : {e}")


def ouvrir_reglages(icon, item=None):
    _ouvrir(icon, os.path.join(BASE_DIR, "alfred_settings.json"), "Fichier de réglages")


def ouvrir_journal(icon, item=None):
    _ouvrir(icon, LOG_PATH, "Journal")


# ============ MISES À JOUR ============
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
        etait_actif = _wanted_active or _est_vivant()
        _stop_and_wait()
        _set_icon(icon, False)
        if etait_actif:
            start_assistant(icon)
    elif isinstance(result, str):
        _notify(icon, f"Version {result} disponible. Téléchargez le nouvel installeur sur GitHub.")
    else:
        _notify(icon, "Alfred est à jour.")


def check_updates(icon, item=None):
    # En tâche de fond : la requête réseau et l'attente du thread bloqueraient
    # sinon le menu de l'icône pendant plusieurs dizaines de secondes.
    threading.Thread(target=_check_updates_target, args=(icon,), daemon=True).start()


# ============ CHIEN DE GARDE ============
def _watchdog(icon):
    """Signale la mort du thread assistant. Sans cela, une exception dans
    run() (modèle Whisper indisponible, micro absent, TTS cassé) laissait
    l'icône au vert et l'utilisateur sans aucune indication."""
    global _wanted_active
    while not _quit_event.wait(2):
        if _wanted_active and not _est_vivant():
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


def _startup_update_check():
    global _needs_reload
    try:
        if updater.check_and_update(BASE_DIR) is True:
            _needs_reload = True
    except Exception:
        traceback.print_exc()


def main():
    if not _acquire_single_instance():
        print("Une autre instance d'Alfred est déjà en cours d'exécution, arrêt.")
        return

    menu = pystray.Menu(
        pystray.MenuItem(_texte_statut, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Assistant actif", toggle_assistant, checked=is_active),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Voix", _menu_voix()),
        pystray.MenuItem("Débit", _menu_debit()),
        pystray.MenuItem("Ouvrir les réglages…", ouvrir_reglages),
        pystray.MenuItem("Ouvrir le journal…", ouvrir_journal),
        pystray.MenuItem("Démarrer avec Windows", toggle_demarrage, checked=_demarrage_actif),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda item: f"Version {_version()}", None, enabled=False),
        pystray.MenuItem("Vérifier les mises à jour…", check_updates),
        pystray.Menu.SEPARATOR,
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
