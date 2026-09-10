#!/usr/bin/env python3
"""
Assistant Vocal Local - moteur (utilisable en standalone ou depuis tray_app.py)
Whisper (STT) + Ollama/llama3.1 (LLM) + edge-tts/pyttsx3 (TTS) + PyAutoGUI (Actions)
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from datetime import datetime

import edge_tts
import pyautogui
import pygame
import pyttsx3
import requests
import whisper
from ddgs import DDGS

# ============ CONFIG ============
CONFIG = {
    "assistant_name": "Alfred",
    "ollama_host": "http://localhost:11434",
    "ollama_model": "llama3.1",
    "whisper_model": "base",
    "language": "fr",
    "edge_voice": "fr-FR-RemyMultilingualNeural",  # voix masculine française (nécessite internet)
    "edge_rate": "-5%",
    # Volontairement neutre : tout décalage de ton par edge-tts s'entend comme
    # un traitement artificiel. Pour une voix plus posée, ralentir edge_rate.
    "edge_pitch": "+0Hz",
}

SETTINGS_FILE = "alfred_settings.json"

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_settings():
    """Applique les réglages trouvés à côté de l'exécutable, pour pouvoir
    changer de voix ou de débit sans reconstruire l'application."""
    chemin = os.path.join(BASE_DIR, SETTINGS_FILE)
    if not os.path.exists(chemin):
        return

    try:
        # utf-8-sig : le Bloc-notes de Windows ajoute un BOM en enregistrant,
        # ce qui ferait échouer la lecture et ignorer silencieusement les
        # réglages que l'utilisateur vient de modifier.
        with open(chemin, encoding="utf-8-sig") as f:
            reglages = json.load(f)
    except (OSError, ValueError) as e:
        print(f"⚠️  Réglages ignorés, {SETTINGS_FILE} illisible : {e}")
        return

    if not isinstance(reglages, dict):
        print(f"⚠️  Réglages ignorés, {SETTINGS_FILE} n'est pas un objet JSON")
        return

    for cle, valeur in reglages.items():
        if cle.startswith("_"):
            continue  # champs d'aide, non appliqués
        if cle in CONFIG:
            CONFIG[cle] = valeur
            print(f"Réglage appliqué : {cle} = {valeur!r}")
        else:
            print(f"⚠️  Réglage inconnu ignoré : {cle}")


_load_settings()


def save_settings(modifications):
    """Applique des réglages et les écrit dans alfred_settings.json, en
    conservant les champs d'aide existants. Permet de changer de voix depuis
    le menu sans perdre le choix au redémarrage."""
    chemin = os.path.join(BASE_DIR, SETTINGS_FILE)

    contenu = {}
    if os.path.exists(chemin):
        try:
            with open(chemin, encoding="utf-8-sig") as f:
                charge = json.load(f)
            if isinstance(charge, dict):
                contenu = charge
        except (OSError, ValueError) as e:
            print(f"⚠️  {SETTINGS_FILE} illisible, il sera réécrit : {e}")

    for cle, valeur in modifications.items():
        if cle in CONFIG:
            CONFIG[cle] = valeur
            contenu[cle] = valeur

    tmp = f"{chemin}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(contenu, f, ensure_ascii=False, indent=2)
    os.replace(tmp, chemin)
    print(f"Réglages enregistrés : {modifications}")

# Applications connues (nom prononcé -> argv). Des listes, jamais des chaînes
# passées à un shell : la cible vient du LLM et ne doit pas pouvoir être
# interprétée comme une commande.
APPS = {
    "bloc-notes": ["notepad.exe"],
    "notepad": ["notepad.exe"],
    "calculatrice": ["calc.exe"],
    "calculette": ["calc.exe"],
    "explorateur": ["explorer.exe"],
    "paint": ["mspaint.exe"],
    "chrome": ["cmd", "/c", "start", "", "chrome"],
    "navigateur": ["cmd", "/c", "start", "", "chrome"],
    "firefox": ["cmd", "/c", "start", "", "firefox"],
    "edge": ["cmd", "/c", "start", "", "msedge"],
}

# Actions irréversibles : jamais exécutées sur la seule foi du classement du
# LLM, qui se trompe régulièrement de catégorie.
DESTRUCTIVE_ACTIONS = {"shutdown", "restart"}
CONFIRM_WORDS = ("oui", "confirme", "confirmé", "vas-y", "allez-y", "je confirme")

# Phrases qui arrêtent l'assistant, comparées à l'identique après
# normalisation : une commande comme « arrête la musique » doit partir au LLM
# et non couper Alfred.
STOP_PHRASES = {
    "stop", "arrête", "arrete", "arrête-toi", "arrete-toi", "arrête toi",
    "arrete toi", "quitte", "quitter", "au revoir", "tais-toi", "tais toi",
    "arrête alfred", "arrete alfred", "alfred arrête", "alfred arrete",
    "c'est tout", "ce sera tout",
}

# Nom d'exécutable ou de processus plausible : garde-fou sur la sortie non
# déterministe du LLM avant de la passer à CreateProcess ou à taskkill.
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.\-]{0,63}")

# Nombre d'erreurs consécutives tolérées avant de renoncer, pour ne pas
# boucler indéfiniment quand la panne est permanente (micro absent, etc.).
MAX_ECHECS = 5

# Format attendu par Whisper : mono, 16 kHz, float32.
SAMPLE_RATE = 16000
LISTEN_SECONDS = 5

# En dessous de ce niveau crête, on considère qu'il n'y a pas de parole et on
# ne transcrit pas : sur du silence ou du bruit de fond, Whisper hallucine des
# phrases entières, que le LLM traduit ensuite en commandes exécutées pour de
# bon. Alfred tournant en permanence, ce garde-fou est indispensable.
SILENCE_THRESHOLD = 0.01

# ============ INIT (paresseux : chargé au premier appel) ============
_whisper_model = None
_tts_engine = None
_init_lock = threading.Lock()
# pygame.mixer.music est une ressource unique : sans ce verrou, un essai de
# voix lancé depuis le menu et une réponse de la boucle principale se
# couperaient mutuellement.
_speak_lock = threading.Lock()


def _select_voice(engine):
    """Choisit une voix française, en préférant une voix masculine si
    disponible. Retourne l'id de la voix choisie, ou None si aucune voix
    française n'est installée (garde la voix par défaut du système)."""
    voix_francaises = [v for v in engine.getProperty('voices') if 'fr' in (v.id + v.name).lower()]
    if not voix_francaises:
        return None

    for v in voix_francaises:
        if str(getattr(v, 'gender', '')).lower() == 'male':
            return v.id

    return voix_francaises[0].id


def _ensure_whisper():
    global _whisper_model
    with _init_lock:
        if _whisper_model is None:
            _whisper_model = whisper.load_model(CONFIG["whisper_model"])


def _ensure_tts():
    global _tts_engine
    with _init_lock:
        if _tts_engine is None:
            engine = pyttsx3.init()
            engine.setProperty('rate', 150)
            engine.setProperty('volume', 0.9)
            voice_id = _select_voice(engine)
            if voice_id:
                engine.setProperty('voice', voice_id)
            _tts_engine = engine


def _ensure_loaded():
    _ensure_whisper()
    _ensure_tts()


def _normalize(texte):
    """Minuscules, ponctuation finale et espaces superflus retirés."""
    t = str(texte).strip().lower()
    t = re.sub(r"[.!?…,;:]+$", "", t)
    return re.sub(r"\s+", " ", t).strip()


# ============ SPEECH TO TEXT ============
def listen():
    """Écoute le microphone et retourne le texte.

    L'audio est passé à Whisper sous forme de tableau float32 mono 16 kHz,
    c'est-à-dire exactement ce que sounddevice produit. Passer par un fichier
    obligerait Whisper à le décoder avec l'outil externe ffmpeg, absent de la
    plupart des machines (et de l'installeur) : la transcription échouait
    alors systématiquement en WinError 2."""
    import sounddevice as sd

    _ensure_whisper()

    print("🎤 Écoute...")
    audio = sd.rec(
        int(LISTEN_SECONDS * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype='float32',
    )
    sd.wait()
    audio = audio.reshape(-1)

    crete = float(abs(audio).max()) if audio.size else 0.0
    if crete < SILENCE_THRESHOLD:
        print(f"… silence (crête {crete:.4f}), rien à transcrire")
        return ""

    print("🔄 Transcription...")
    result = _whisper_model.transcribe(
        audio, language=CONFIG["language"], fp16=False
    )

    text = result["text"].strip()
    print(f"📝 Vous: {text}")
    return text


# ============ LLM ENGINE ============
def query_ollama(prompt):
    """Appelle Ollama pour traiter la commande.

    Retourne le texte du modèle, ou None si Ollama est injoignable — la cause
    est journalisée, sinon une panne d'Ollama se traduisait juste par un
    « Je n'ai pas compris » sans indice pour l'utilisateur."""
    try:
        response = requests.post(
            f"{CONFIG['ollama_host']}/api/generate",
            json={"model": CONFIG["ollama_model"], "prompt": prompt, "stream": False},
            timeout=30
        )
        if response.status_code != 200:
            print(f"❌ Ollama a répondu {response.status_code} : {response.text[:200]}")
            return None
        return response.json()["response"]
    except requests.exceptions.RequestException as e:
        print(f"❌ Ollama injoignable sur {CONFIG['ollama_host']} : {e}")
        return None


# ============ RECHERCHE WEB ============
def web_search(query, max_results=5):
    """Cherche sur DuckDuckGo et retourne une liste d'extraits (title/href/body).

    Ne lève jamais : sans internet, ou si DuckDuckGo limite les requêtes
    automatisées (ça arrive), retourne simplement une liste vide plutôt que
    de faire planter le thread principal."""
    try:
        return DDGS().text(query, max_results=max_results, region="fr-fr")
    except Exception as e:
        print(f"❌ Recherche web impossible : {e}")
        return []


def _synthesize_search_answer(query, resultats):
    """Rédige, à partir des extraits trouvés, une réponse orale dans la
    personnalité d'Alfred. Retourne None si rien à synthétiser."""
    if not resultats:
        return None

    extraits = "\n".join(
        f"- {r.get('title', '')} : {r.get('body', '')}" for r in resultats
    )
    prompt = f"""{PERSONA.format(name=CONFIG['assistant_name'])}

L'utilisateur a demandé : "{query}"

Voici des extraits de résultats de recherche web trouvés à ce sujet :
{extraits}

Rédige la réponse orale que tu vas prononcer, en te basant uniquement sur ces
extraits. Contrairement à tes autres réponses habituellement très brèves,
ici tu peux prendre 2 à 4 phrases si l'information le demande, toujours dans
ta personnalité de majordome. Ne mentionne ni le mot "extrait" ni le mot
"recherche" : parle comme si tu savais déjà la réponse. Réponds uniquement
avec le texte à prononcer, sans JSON ni balises."""

    return query_ollama(prompt)


def answer_from_search(query):
    """Cherche sur le web puis synthétise une réponse orale. Retourne None si
    la recherche n'a rien donné ou si Ollama est injoignable."""
    resultats = web_search(query)
    return _synthesize_search_answer(query, resultats)


PERSONA = """Tu es {name}, un majordome anglais d'une soixantaine d'années, au service de l'utilisateur \
depuis de nombreuses années. Tu es calme, courtois, un brin pince-sans-rire, et tu vouvoies toujours \
l'utilisateur. Tes réponses sont brèves (une phrase, deux maximum) mais jamais froides ni robotiques : \
elles ont la voix d'un homme posé qui a de l'expérience et un léger sens de l'humour discret. Tu ne \
dis jamais que tu es une intelligence artificielle. Écris toujours des phrases fluides et complètes : \
ne commence jamais par un mot isolé (comme "Bonsoir." seul) et ne termine jamais sur un simple point \
d'interrogation sec — préfère une formule qui s'étoffe, par exemple "Dites-moi ce qu'il vous faut" \
plutôt qu'un "?" abrupt."""


def interpret_command(user_text):
    """Interprète la commande avec le LLM"""
    prompt = f"""{PERSONA.format(name=CONFIG['assistant_name'])}

L'utilisateur dit : "{user_text}"

Réponds UNIQUEMENT en JSON avec ces champs, sans texte autour ni balises markdown:
{{"action": "type_action", "target": "cible", "response": "ta réponse vocale, dans ta personnalité"}}

Actions possibles: play_pause, next_track, prev_track, volume_up, volume_down,
open_app, close_app, shutdown, restart, time, web_search, help

Utilise web_search pour toute question dont tu ne peux pas connaître la
réponse avec certitude (météo, actualité, résultat sportif, information
récente ou précise sur une personne, un lieu, un événement...). Dans ce cas,
"target" doit être une requête de recherche courte et précise, pas la phrase
de l'utilisateur telle quelle.

Exemple:
- "pause la musique" → {{"action": "play_pause", "target": "", "response": "Musique en pause, comme vous le souhaitiez."}}
- "ouvre firefox" → {{"action": "open_app", "target": "firefox", "response": "Firefox arrive à l'instant, monsieur."}}
- "quelle heure" → {{"action": "time", "target": "", "response": "Il est 14h30"}}
- "quel temps fait-il à Lyon" → {{"action": "web_search", "target": "météo Lyon aujourd'hui", "response": "Je me renseigne, monsieur."}}

Réponds maintenant en JSON uniquement:"""

    response = query_ollama(prompt)
    if response is None:
        return {"action": "help", "target": "",
                "response": "Je n'arrive pas à joindre mon moteur de langage, monsieur."}

    # Le LLM entoure parfois le JSON de texte explicatif ou de balises ```json ... ```
    match = re.search(r"\{.*\}", response, re.DOTALL)
    if match:
        try:
            command = json.loads(match.group(0))
            if isinstance(command, dict):
                return command
        except json.JSONDecodeError:
            pass
    print(f"⚠️  Réponse du modèle non exploitable : {response[:300]!r}")
    return {"action": "help", "target": "", "response": "Je n'ai pas compris"}


# ============ TEXT TO SPEECH ============
def _speak_edge(text):
    """Synthétise avec la voix Edge (Remy, masculine, française, nécessite internet) et la joue."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        audio_path = tmp.name
    try:
        comm = edge_tts.Communicate(
            text,
            CONFIG["edge_voice"],
            rate=CONFIG["edge_rate"],
            pitch=CONFIG["edge_pitch"],
        )
        asyncio.run(comm.save(audio_path))

        if not pygame.mixer.get_init():
            pygame.mixer.init()
        try:
            pygame.mixer.music.load(audio_path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                pygame.time.wait(100)
        finally:
            # Sans unload(), pygame garde le fichier ouvert et la suppression
            # échoue sur Windows en masquant l'erreur d'origine.
            try:
                pygame.mixer.music.unload()
            except Exception:
                pass
    finally:
        try:
            os.remove(audio_path)
        except OSError:
            pass


def speak(text):
    """Parle le texte. Utilise la voix Remy (Edge, masculine, française) si
    internet est disponible, sinon retombe sur la voix locale (Hortense).

    Ne lève jamais : speak() est appelé depuis les gestionnaires d'erreur de
    la boucle principale, où une exception tuerait le thread."""
    print(f"🔊 {CONFIG['assistant_name']}: {text}")
    with _speak_lock:
        try:
            _speak_edge(text)
            return
        except Exception as e:
            print(f"⚠️  Voix Edge indisponible ({e}), repli sur la voix locale")

        try:
            _ensure_tts()
            _tts_engine.say(text)
            _tts_engine.runAndWait()
        except Exception as e:
            print(f"⚠️  Voix locale indisponible également : {e}")


# ============ ACTIONS SYSTEM ============
def _app_key(nom):
    """Clé de recherche tolérante : le LLM renvoie indifféremment
    « bloc-notes », « bloc_notes », « Bloc Notes »… pour la même application."""
    return re.sub(r"[\s_\-]+", "", _normalize(nom))


_APPS_INDEX = {_app_key(cle): argv for cle, argv in APPS.items()}


def _open_app(target):
    argv = _APPS_INDEX.get(_app_key(target))
    if argv:
        subprocess.Popen(argv)
        return True

    nom = str(target).strip()
    if not _SAFE_NAME.fullmatch(nom):
        print(f"❌ Nom d'application refusé : {nom!r}")
        return False
    subprocess.Popen([nom])
    return True


def _close_app(target):
    nom = str(target).strip()
    if nom.lower().endswith(".exe"):
        nom = nom[:-4]
    if not _SAFE_NAME.fullmatch(nom):
        print(f"❌ Nom de processus refusé : {nom!r}")
        return False
    resultat = subprocess.run(["taskkill", "/IM", f"{nom}.exe", "/F"], capture_output=True)
    return resultat.returncode == 0


def execute_action(action, target):
    """Exécute une action sur le PC.

    Retourne l'heure (str) pour `time`, True si l'action a abouti, et False
    si elle a échoué ou a été refusée — l'appelant doit en tenir compte pour
    ne pas confirmer à l'oral une action qui n'a rien fait."""
    try:
        if action == "play_pause":
            pyautogui.press('playpause')

        elif action == "next_track":
            pyautogui.press('nexttrack')

        elif action == "prev_track":
            pyautogui.press('prevtrack')

        elif action == "volume_up":
            for _ in range(3):
                pyautogui.press('volumeup')

        elif action == "volume_down":
            for _ in range(3):
                pyautogui.press('volumedown')

        elif action == "open_app":
            return _open_app(target)

        elif action == "close_app":
            return _close_app(target)

        elif action == "shutdown":
            subprocess.run(["shutdown", "/s", "/t", "30"], capture_output=True)

        elif action == "restart":
            subprocess.run(["shutdown", "/r", "/t", "30"], capture_output=True)

        elif action == "time":
            return datetime.now().strftime("%H:%M")

        elif action == "web_search":
            return answer_from_search(target) or False

        return True
    except Exception as e:
        print(f"❌ Erreur exécution: {e}")
        return False


def _confirm_destructive(action):
    """Demande une confirmation vocale explicite avant une action irréversible."""
    libelle = "éteindre l'ordinateur" if action == "shutdown" else "redémarrer l'ordinateur"
    speak(f"Confirmez-vous que je dois {libelle} ? Dites oui pour valider.")
    reponse = _normalize(listen())
    print(f"Confirmation entendue : {reponse!r}")
    return any(mot in reponse for mot in CONFIRM_WORDS)


# ============ MAIN LOOP ============
def run(stop_event=None):
    """Boucle principale de l'assistant. S'arrête dès que stop_event est levé
    (si fourni), sinon tourne jusqu'à un mot d'arrêt vocal ou Ctrl+C."""
    if stop_event is None:
        stop_event = threading.Event()

    print("=" * 50)
    print(f"🎙️  {CONFIG['assistant_name'].upper()} - ASSISTANT VOCAL LOCAL")
    print("=" * 50)

    _ensure_loaded()
    speak(f"Eh bien, {CONFIG['assistant_name']} à votre service. Dites-moi ce dont vous avez besoin.")

    echecs = 0
    while not stop_event.is_set():
        try:
            user_input = listen()

            if stop_event.is_set():
                break

            if not user_input:
                continue

            if _normalize(user_input) in STOP_PHRASES:
                speak("Très bien, je reste à votre entière disposition.")
                break

            command = interpret_command(user_input)
            action = str(command.get("action") or "help")
            target = str(command.get("target") or "")
            response = str(command.get("response") or "Commande non reconnue")

            if action in DESTRUCTIVE_ACTIONS and not _confirm_destructive(action):
                speak("Fort bien, je n'y touche pas.")
                continue

            resultat = execute_action(action, target)
            if action == "time" and resultat:
                response = f"Il est {resultat}, monsieur."
            elif action == "web_search" and resultat:
                response = resultat
            elif resultat is False:
                response = "Je n'ai pas pu m'en occuper, monsieur."
            speak(response)

            echecs = 0

        except KeyboardInterrupt:
            speak("Fort bien, je me retire pour cette fois.")
            break
        except Exception as e:
            echecs += 1
            print(f"Erreur (échec {echecs}/{MAX_ECHECS}) : {e}")
            if echecs == 1:
                speak("Toutes mes excuses, un contretemps est survenu.")
            if echecs >= MAX_ECHECS:
                speak("Je rencontre un problème persistant, je me mets en veille.")
                raise RuntimeError(f"{echecs} échecs consécutifs, dernier : {e}") from e
            # Attente croissante, interruptible par une demande d'arrêt.
            stop_event.wait(min(2 ** echecs, 30))


if __name__ == "__main__":
    print("\n⚙️  Prérequis:")
    print("1. Installer Ollama: https://ollama.ai")
    print(f"2. Télécharger le modèle: ollama pull {CONFIG['ollama_model']}")
    print("3. Lancer Ollama: ollama serve\n")

    input("Appuyez sur Entrée quand Ollama est lancé...")

    run()
