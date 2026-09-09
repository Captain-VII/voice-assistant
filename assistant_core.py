#!/usr/bin/env python3
"""
Assistant Vocal Local - moteur (utilisable en standalone ou depuis tray_app.py)
Whisper (STT) + Ollama/Llama2 (LLM) + pyttsx3 (TTS) + PyAutoGUI (Actions)
"""

import asyncio
import whisper
import pyttsx3
import pyautogui
import subprocess
import json
import os
import re
import tempfile
import threading
from datetime import datetime

import edge_tts
import pygame
import requests

# ============ CONFIG ============
CONFIG = {
    "assistant_name": "Alfred",
    "ollama_host": "http://localhost:11434",
    "ollama_model": "llama3.1",
    "whisper_model": "base",
    "language": "fr",
    "edge_voice": "fr-FR-RemyMultilingualNeural",  # voix masculine française (nécessite internet)
    "edge_rate": "-8%",
    "edge_pitch": "-3Hz",
}

# Applications connues (nom prononcé -> commande réelle)
APPS = {
    "bloc-notes": "notepad.exe",
    "notepad": "notepad.exe",
    "calculatrice": "calc.exe",
    "calculette": "calc.exe",
    "explorateur": "explorer.exe",
    "paint": "mspaint.exe",
    "chrome": "start chrome",
    "navigateur": "start chrome",
    "firefox": "start firefox",
    "edge": "start msedge",
}

# ============ INIT (paresseux : chargé au premier appel) ============
_whisper_model = None
_tts_engine = None
_init_lock = threading.Lock()


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


def _ensure_loaded():
    global _whisper_model, _tts_engine
    with _init_lock:
        if _whisper_model is None:
            _whisper_model = whisper.load_model(CONFIG["whisper_model"])
        if _tts_engine is None:
            _tts_engine = pyttsx3.init()
            _tts_engine.setProperty('rate', 150)
            _tts_engine.setProperty('volume', 0.9)
            voice_id = _select_voice(_tts_engine)
            if voice_id:
                _tts_engine.setProperty('voice', voice_id)


# ============ SPEECH TO TEXT ============
def listen():
    """Écoute le microphone et retourne le texte"""
    import sounddevice as sd
    import soundfile as sf

    print("🎤 Écoute...")
    duration = 5
    sample_rate = 16000
    audio = sd.rec(int(duration * sample_rate), samplerate=sample_rate, channels=1, dtype='float32')
    sd.wait()

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        audio_path = tmp.name
    try:
        sf.write(audio_path, audio, sample_rate)
        print("🔄 Transcription...")
        result = _whisper_model.transcribe(audio_path, language=CONFIG["language"], fp16=False)
    finally:
        os.remove(audio_path)

    text = result["text"].strip()
    print(f"📝 Vous: {text}")
    return text


# ============ LLM ENGINE ============
def query_ollama(prompt):
    """Appelle Ollama pour traiter la commande"""
    try:
        response = requests.post(
            f"{CONFIG['ollama_host']}/api/generate",
            json={"model": CONFIG["ollama_model"], "prompt": prompt, "stream": False},
            timeout=30
        )
        if response.status_code == 200:
            return response.json()["response"]
        return "Erreur de connexion Ollama"
    except requests.exceptions.RequestException as e:
        return f"Erreur: {str(e)}"


PERSONA = f"""Tu es {{name}}, un majordome anglais d'une soixantaine d'années, au service de l'utilisateur \
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
open_app, close_app, shutdown, restart, time, help

Exemple:
- "pause la musique" → {{"action": "play_pause", "target": "", "response": "Musique en pause, comme vous le souhaitiez."}}
- "ouvre firefox" → {{"action": "open_app", "target": "firefox", "response": "Firefox arrive à l'instant, monsieur."}}
- "quelle heure" → {{"action": "time", "target": "", "response": "Il est 14h30"}}

Réponds maintenant en JSON uniquement:"""

    response = query_ollama(prompt)
    # Le LLM entoure parfois le JSON de texte explicatif ou de balises ```json ... ```
    match = re.search(r"\{.*\}", response, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
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
        pygame.mixer.music.load(audio_path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.wait(100)
        pygame.mixer.music.unload()
    finally:
        os.remove(audio_path)


def speak(text):
    """Parle le texte. Utilise la voix Remy (Edge, masculine, française) si
    internet est disponible, sinon retombe sur la voix locale (Hortense)."""
    print(f"🔊 {CONFIG['assistant_name']}: {text}")
    try:
        _speak_edge(text)
    except Exception as e:
        print(f"⚠️  Voix Edge indisponible ({e}), repli sur la voix locale")
        _tts_engine.say(text)
        _tts_engine.runAndWait()


# ============ ACTIONS SYSTEM ============
def execute_action(action, target):
    """Exécute une action sur le PC. Retourne True si l'action a réussi."""
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
            commande = APPS.get(target.lower().strip(), target)
            if commande.startswith("start "):
                os.system(commande)
            else:
                subprocess.Popen(commande)

        elif action == "close_app":
            nom = target.strip()
            if nom.lower().endswith(".exe"):
                nom = nom[:-4]
            subprocess.run(f"taskkill /IM {nom}.exe /F", shell=True, capture_output=True)

        elif action == "shutdown":
            subprocess.run("shutdown /s /t 30", shell=True)

        elif action == "restart":
            subprocess.run("shutdown /r /t 30", shell=True)

        elif action == "time":
            return datetime.now().strftime("%H:%M")

        return True
    except Exception as e:
        print(f"❌ Erreur exécution: {e}")
        return False


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

    while not stop_event.is_set():
        try:
            user_input = listen()

            if stop_event.is_set():
                break

            if not user_input:
                continue

            texte_min = user_input.lower()
            if any(mot in texte_min for mot in ["arrête", "arrete", "stop", "quitte"]):
                speak("Très bien, je reste à votre entière disposition.")
                break

            command = interpret_command(user_input)
            action = command.get("action", "help")
            target = command.get("target", "")
            response = command.get("response", "Commande non reconnue")

            resultat = execute_action(action, target)
            if action == "time" and resultat:
                response = f"Il est {resultat}, monsieur."
            speak(response)

        except KeyboardInterrupt:
            speak("Fort bien, je me retire pour cette fois.")
            break
        except Exception as e:
            print(f"Erreur: {e}")
            speak("Toutes mes excuses, un contretemps est survenu.")


if __name__ == "__main__":
    print("\n⚙️  Prérequis:")
    print("1. Installer Ollama: https://ollama.ai")
    print("2. Télécharger Llama2: ollama pull llama2")
    print("3. Lancer Ollama: ollama serve\n")

    input("Appuyez sur Entrée quand Ollama est lancé...")

    run()
