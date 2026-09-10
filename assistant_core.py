#!/usr/bin/env python3
"""
Assistant Vocal Local - moteur (utilisable en standalone ou depuis tray_app.py)
Whisper (STT) + Ollama/llama3.1 (LLM) + edge-tts/pyttsx3 (TTS) + PyAutoGUI (Actions)
"""

import asyncio
import json
import os
import queue
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime

import edge_tts
import numpy as np
import pyautogui
import pygame
import pyttsx3
import requests
import whisper
from ddgs import DDGS

try:
    import winsound  # bibliothèque standard, Windows uniquement
except ImportError:  # pragma: no cover - autre plateforme
    winsound = None

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
    # Mot de réveil : Alfred n'interprète que les phrases qui le contiennent.
    # Chaîne vide = désactivé (il réagit alors à tout ce qu'il entend).
    "wake_word": "alfred",
    # Orthographes que Whisper produit parfois pour ce prénom : à compléter
    # depuis le journal si une prononciation revient sans être reconnue.
    "wake_word_variants": [
        "alfrède", "alfrèd", "alfrede", "alfredo", "alfret", "alfredd",
        "alfreed", "halfred", "alfrid", "alfride", "l'fride", "alfredo",
    ],
    # Niveau MOYEN (RMS) d'un bloc de 100 ms à partir duquel on considère
    # qu'on parle. La crête était trop fragile : un clic de clavier la
    # dépassait et Whisper hallucinait ensuite des phrases entières. Le
    # niveau moyen d'une pièce calme mesuré ici est 0.0001-0.0004 : 0.005
    # laisse dix fois de marge sans risquer de manquer une voix normale.
    "silence_threshold": 0.005,
    # Silence qui clôt un énoncé, durée maximale d'un énoncé, et âge au-delà
    # duquel un énoncé capté n'est plus exécuté (une commande périmée
    # exécutée longtemps après vaut moins que pas de commande).
    "silence_duration": 0.8,
    "max_utterance_seconds": 15,
    "stale_command_seconds": 10,
    # Bip bref dès que le prénom est reconnu, avant même de réfléchir.
    "beep_on_wake": True,
}

SETTINGS_FILE = "alfred_settings.json"

# Réglages exposés à l'utilisateur dans alfred_settings.json, et textes d'aide
# associés. Source de vérité du modèle de fichier : l'installeur conserve le
# fichier existant pour ne pas écraser les choix de l'utilisateur, si bien
# qu'une option ajoutée dans une nouvelle version n'y apparaîtrait jamais.
SETTINGS_EXPOSED = (
    "edge_voice", "edge_rate", "edge_pitch",
    "wake_word", "wake_word_variants", "silence_threshold",
    "silence_duration", "max_utterance_seconds", "stale_command_seconds",
    "beep_on_wake",
    "ollama_model", "whisper_model",
)

SETTINGS_HELP = {
    "_aide": "Modifiez ces valeurs puis relancez Alfred. Aucune reconstruction nécessaire.",
    "_voix_masculines": (
        "fr-FR-RemyMultilingualNeural, fr-FR-HenriNeural, fr-BE-GerardNeural, "
        "fr-CH-FabriceNeural, fr-CA-ThierryNeural, fr-CA-JeanNeural, "
        "fr-CA-AntoineNeural, en-US-AndrewMultilingualNeural, "
        "en-AU-WilliamMultilingualNeural"
    ),
    "_ton": (
        "Laisser edge_pitch à +0Hz : tout décalage de ton par edge-tts s'entend "
        "comme un traitement artificiel. Pour une voix plus posée, ralentir "
        "edge_rate (par exemple -8%)."
    ),
    "_mot_de_reveil": (
        "Alfred n'interprète que les phrases contenant wake_word. Mettre une "
        "chaîne vide pour qu'il réagisse à tout ce qu'il entend. Si une de vos "
        "prononciations n'est pas reconnue, regardez la ligne « ignoré, mot de "
        "réveil absent » du journal et ajoutez l'orthographe entendue dans "
        "wake_word_variants."
    ),
    "_sensibilite": (
        "silence_threshold est le niveau sonore MOYEN (et non plus la crête) "
        "à partir duquel Alfred considère qu'on parle. Si une télévision ou "
        "des conversations le déclenchent, montez-le (0.02, 0.03...) ; s'il "
        "n'entend pas une voix douce ou éloignée, baissez-le (0.005). Pour "
        "choisir une valeur sur des chiffres réels, utilisez « Tester le "
        "micro… » dans le menu de l'icône : le journal affiche le niveau "
        "mesuré pendant dix secondes."
    ),
    "_ecoute": (
        "Alfred écoute en continu. Un énoncé se termine après silence_duration "
        "secondes de silence, ne dépasse jamais max_utterance_seconds, et n'est "
        "plus exécuté s'il attend depuis plus de stale_command_seconds (par "
        "exemple parce qu'Alfred parlait). beep_on_wake : bip bref dès que le "
        "prénom est reconnu."
    ),
}

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


def _sync_settings_file():
    """Complète alfred_settings.json avec les options et aides absentes, sans
    toucher aux valeurs déjà choisies par l'utilisateur. Sans cela, une option
    ajoutée dans une nouvelle version resterait invisible et donc inutilisable
    pour qui met à jour une installation existante."""
    chemin = os.path.join(BASE_DIR, SETTINGS_FILE)

    contenu = {}
    if os.path.exists(chemin):
        try:
            with open(chemin, encoding="utf-8-sig") as f:
                charge = json.load(f)
            if isinstance(charge, dict):
                contenu = charge
        except (OSError, ValueError):
            return  # fichier illisible : _load_settings l'a déjà signalé

    attendu = {**SETTINGS_HELP, **{cle: CONFIG[cle] for cle in SETTINGS_EXPOSED}}
    manquants = {cle: val for cle, val in attendu.items() if cle not in contenu}

    # Fichier d'avant l'écoute continue : silence_threshold y désignait une
    # crête, il désigne maintenant un niveau moyen, nettement plus bas. On ne
    # migre que l'ancienne valeur par défaut, jamais un réglage personnalisé.
    if "_ecoute" not in contenu and contenu.get("silence_threshold") == 0.01:
        manquants["silence_threshold"] = CONFIG["silence_threshold"] = 0.005
        print("Réglage migré : silence_threshold 0.01 (crête) -> 0.005 (niveau moyen)")

    if not manquants:
        return

    contenu.update(manquants)
    try:
        tmp = f"{chemin}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(contenu, f, ensure_ascii=False, indent=2)
        os.replace(tmp, chemin)
        print(f"Réglages complétés avec les nouvelles options : {sorted(manquants)}")
    except OSError as e:
        print(f"⚠️  Impossible de compléter {SETTINGS_FILE} : {e}")


_load_settings()
_sync_settings_file()


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
# Blocs de 100 ms remis par le flux de capture continue.
BLOCK_SECONDS = 0.1
BLOCK_SIZE = int(SAMPLE_RATE * BLOCK_SECONDS)
# Parole minimale pour qu'un énoncé soit transcrit : en dessous, c'est un
# clic ou une porte qui claque.
MIN_SPEECH_SECONDS = 0.4
# Blocs conservés avant le premier bloc de parole : l'attaque d'un mot est
# plus douce que son milieu, et Whisper la veut pour bien démarrer.
PRE_ROLL_BLOCKS = 2
# Énoncés en attente de transcription : au-delà, le plus ancien est jeté.
MAX_PENDING = 3
# Segments Whisper au-delà de cette probabilité de « pas de parole » : rejetés.
NO_SPEECH_MAX = 0.6

# ============ INIT (paresseux : chargé au premier appel) ============
_whisper_model = None
_tts_engine = None
_init_lock = threading.Lock()
# pygame.mixer.music est une ressource unique : sans ce verrou, un essai de
# voix lancé depuis le menu et une réponse de la boucle principale se
# couperaient mutuellement.
_speak_lock = threading.Lock()
# stop_event de la boucle en cours, pour que listen() y reste réactif même
# quand il est appelé depuis _confirm_destructive().
_stop_event = threading.Event()


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


def _wake_regex():
    """Motif du mot de réveil et de ses variantes. Reconstruit à chaque appel
    pour tenir compte d'un réglage modifié à chaud. None si désactivé."""
    mot = str(CONFIG.get("wake_word") or "").strip()
    if not mot:
        return None

    variantes = [mot] + [str(v) for v in (CONFIG.get("wake_word_variants") or [])]
    # Les plus longues d'abord : dans une alternance, « alfred » masquerait
    # « alfredo » et laisserait un « o » orphelin dans la commande.
    motifs = sorted(
        {re.escape(v.strip()) for v in variantes if v.strip()},
        key=len,
        reverse=True,
    )
    return re.compile(r"\b(?:" + "|".join(motifs) + r")\b", re.IGNORECASE)


def _wake_and_command(texte):
    """Sépare le mot de réveil du reste de la phrase.

    Retourne (réveillé, commande). Alfred écoutant en continu, sans ce filtre
    il interprète et exécute pour de bon les conversations et la télévision
    qui passent à portée de micro.

    La commande est ce qui suit le prénom : « Quoi ? Alfred, mets play » donne
    « mets play », le « Quoi ? » n'étant qu'un préambule. Si rien d'utile ne
    suit, on garde ce qui précède, pour que « arrête, Alfred » fonctionne
    aussi. Une commande vide signifie que seul le prénom a été prononcé (ou
    suivi de mots creux) : l'appelant enchaîne alors sur une seconde écoute."""
    motif = _wake_regex()
    if motif is None:
        return True, texte

    premier = motif.search(texte)
    if not premier:
        return False, ""

    avant = _nettoyer_commande(texte[:premier.start()])
    apres = _nettoyer_commande(motif.sub(" ", texte[premier.end():]))
    if not _est_appel_seul(apres):
        return True, apres
    if not _est_appel_seul(avant):
        return True, avant
    return True, ""


def _nettoyer_commande(texte):
    """Espaces et ponctuation orpheline laissés par le retrait du prénom :
    « Dis-moi , quelle heure » -> « Dis-moi, quelle heure »."""
    reste = re.sub(r"\s+", " ", texte)
    reste = re.sub(r"\s+([,.;:!?])", r"\1", reste)
    return reste.strip(" ,.;:!?-—…'\"")


# Ce qui reste d'un simple appel une fois le prénom retiré : « Alfred ! Allo ! »
# n'est pas une commande, c'est quelqu'un qui vérifie qu'on l'écoute.
_MOTS_CREUX = {
    "allo", "allô", "oui", "hé", "eh", "hey", "ho", "coucou", "hello", "salut",
    "tu es là", "t'es là", "es-tu là", "vous êtes là", "êtes-vous là",
    "dis", "dis-moi", "dites", "dites-moi", "s'il te plaît", "s'il vous plaît",
}


def _est_appel_seul(reste):
    """Vrai si l'énoncé, prénom retiré, ne contient aucune commande."""
    n = re.sub(r"[^\w\s'-]", " ", _normalize(reste))
    n = re.sub(r"\s+", " ", n).strip(" '-")
    return not n or len(n) < 3 or n in _MOTS_CREUX


# ============ SPEECH TO TEXT ============
# Capture continue : le micro reste ouvert en permanence et découpe lui-même
# les énoncés. L'ancienne fenêtre fixe de 5 s fermait le micro pendant la
# transcription, le classement et toute la lecture à voix haute : Alfred
# était sourd la moitié du temps (80 % pendant une réponse de recherche), et
# la fenêtre s'ouvrait au rythme de la boucle, jamais quand on lui parlait.
_audio_q = queue.Queue()      # (instant de clôture, tableau float32)
_capture_stream = None
_capture_lock = threading.Lock()
_mute_until = 0.0             # Alfred parle : ce qui entre est ignoré
_monitor = None               # liste (rms, crête) remplie par mesure_micro
_capture_state = {
    "blocs": [], "pre": deque(maxlen=PRE_ROLL_BLOCKS),
    "actif": False, "parole": 0.0, "silence": 0.0, "erreur_signalee": False,
}


def _reset_capture(etat):
    etat["blocs"] = []
    etat["actif"] = False
    etat["parole"] = 0.0
    etat["silence"] = 0.0


def _enqueue_utterance(audio):
    """File bornée : on jette le plus ancien, jamais le plus récent."""
    while _audio_q.qsize() >= MAX_PENDING:
        try:
            _audio_q.get_nowait()
            print("… énoncé en attente écarté, trop de retard")
        except queue.Empty:
            break
    _audio_q.put((time.monotonic(), audio))


def _close_utterance(etat):
    if etat["parole"] >= MIN_SPEECH_SECONDS and etat["blocs"]:
        _enqueue_utterance(np.concatenate(etat["blocs"]))
    _reset_capture(etat)


def _on_audio(indata, frames, temps, status):
    """Callback PortAudio, un bloc de 100 ms à la fois. Reste minimal : tout
    travail lourd ici ferait perdre de l'audio."""
    etat = _capture_state
    try:
        bloc = np.asarray(indata, dtype="float32").reshape(-1).copy()
        rms = float(np.sqrt(np.mean(bloc * bloc))) if bloc.size else 0.0
        if _monitor is not None:
            _monitor.append((rms, float(np.abs(bloc).max()) if bloc.size else 0.0))

        if time.monotonic() < _mute_until:
            _reset_capture(etat)
            etat["pre"].clear()
            return

        seuil = float(CONFIG.get("silence_threshold") or 0.01)
        duree_bloc = bloc.size / SAMPLE_RATE

        if rms >= seuil:
            if not etat["actif"]:
                etat["actif"] = True
                etat["blocs"].extend(etat["pre"])
            etat["blocs"].append(bloc)
            etat["parole"] += duree_bloc
            etat["silence"] = 0.0
        elif etat["actif"]:
            # On garde le souffle de fin de phrase, Whisper coupe mieux ainsi.
            etat["blocs"].append(bloc)
            etat["silence"] += duree_bloc
            if etat["silence"] >= float(CONFIG.get("silence_duration") or 0.8):
                _close_utterance(etat)
        else:
            etat["pre"].append(bloc)

        if etat["actif"]:
            total = sum(b.size for b in etat["blocs"]) / SAMPLE_RATE
            if total >= float(CONFIG.get("max_utterance_seconds") or 15):
                _close_utterance(etat)
    except Exception as e:  # une exception ici arrêterait le flux en silence
        if not etat["erreur_signalee"]:
            etat["erreur_signalee"] = True
            print(f"❌ Erreur dans la capture audio : {e}")


def _start_capture():
    """Ouvre le flux micro une seule fois ; les appels suivants ne font rien.
    Retourne True si le flux vient d'être ouvert par cet appel."""
    global _capture_stream
    import sounddevice as sd

    with _capture_lock:
        if _capture_stream is not None:
            return False
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            blocksize=BLOCK_SIZE, callback=_on_audio,
        )
        stream.start()
        _capture_stream = stream
        print("🎤 Écoute continue ouverte")
        return True


def _stop_capture():
    global _capture_stream
    with _capture_lock:
        stream, _capture_stream = _capture_stream, None
    if stream is None:
        return
    try:
        stream.stop()
        stream.close()
    except Exception as e:
        print(f"⚠️  Fermeture du micro : {e}")
    _reset_capture(_capture_state)
    _drain_queue()
    print("🎤 Écoute continue fermée")


def _drain_queue():
    while True:
        try:
            _audio_q.get_nowait()
        except queue.Empty:
            return


def _transcribe(audio):
    result = _whisper_model.transcribe(
        audio, language=CONFIG["language"], fp16=False,
        condition_on_previous_text=False, temperature=0.0,
    )
    # Sur du bruit, Whisper produit des segments qu'il juge lui-même sans
    # parole : on les écarte au lieu de les envoyer au LLM.
    segments = list(result.get("segments") or [])
    gardes = [s for s in segments if float(s.get("no_speech_prob", 0.0)) <= NO_SPEECH_MAX]
    if segments and not gardes:
        return ""
    if gardes:
        return " ".join(str(s.get("text", "")).strip() for s in gardes).strip()
    return str(result.get("text", "")).strip()


def listen(timeout=None):
    """Attend le prochain énoncé capté et retourne sa transcription.

    Retourne "" si stop_event est levé, si `timeout` secondes s'écoulent sans
    rien entendre, ou si Whisper ne trouve pas de parole. Les énoncés plus
    vieux que stale_command_seconds sont jetés : une commande exécutée bien
    après avoir été prononcée est précisément le symptôme qu'on corrige.

    L'audio est passé à Whisper sous forme de tableau float32 mono 16 kHz.
    Passer par un fichier l'obligerait à décoder avec ffmpeg, absent de la
    plupart des machines : la transcription échouait alors en WinError 2."""
    _ensure_whisper()
    _start_capture()

    limite = None if timeout is None else time.monotonic() + timeout
    while True:
        if _stop_event.is_set():
            return ""
        if limite is not None and time.monotonic() >= limite:
            return ""
        try:
            capte, audio = _audio_q.get(timeout=0.2)
        except queue.Empty:
            continue
        age = time.monotonic() - capte
        peremption = float(CONFIG.get("stale_command_seconds") or 10)
        if age > peremption:
            print(f"… énoncé périmé ({age:.1f} s > {peremption:g} s), ignoré")
            continue
        break

    print(f"🔄 Transcription ({audio.size / SAMPLE_RATE:.1f} s)...")
    text = _transcribe(audio)
    print(f"📝 Vous: {text}")
    return text


def mesure_micro(secondes=10):
    """Affiche le niveau moyen par bloc de 100 ms pendant `secondes`, puis un
    résumé, pour régler silence_threshold sur des chiffres réels."""
    global _monitor
    ouvert_ici = _start_capture()
    _monitor = []
    try:
        print(f"🎚️  Mesure du micro pendant {secondes} s (parlez normalement)…")
        fin = time.monotonic() + secondes
        while time.monotonic() < fin:
            time.sleep(0.1)
        mesures = list(_monitor)
    finally:
        _monitor = None
        if ouvert_ici:
            _stop_capture()

    seuil = float(CONFIG.get("silence_threshold") or 0.01)
    for i in range(0, len(mesures), 10):
        tranche = mesures[i:i + 10]
        print(f"  {i / 10:4.1f} s  moyen " + " ".join(f"{r:.4f}" for r, _ in tranche)
              + "  crête " + f"{max(c for _, c in tranche):.3f}")
    if mesures:
        rms = sorted(r for r, _ in mesures)
        fond = rms[len(rms) // 10]
        pic = rms[-1]
        print(f"🎚️  Fond sonore ≈ {fond:.4f}, parole max ≈ {pic:.4f}, "
              f"seuil actuel {seuil:g} (blocs au-dessus : "
              f"{sum(1 for r in rms if r >= seuil)}/{len(rms)})")
        if pic >= 0.005 and pic > fond * 3:
            print(f"🎚️  Suggestion de seuil : {(fond * 2 + pic * 0.15):.4f}")
        else:
            print("🎚️  Aucune parole nette détectée pendant la mesure : parlez au micro pour obtenir une suggestion.")


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
    global _mute_until
    print(f"🔊 {CONFIG['assistant_name']}: {text}")
    with _speak_lock:
        # Sourdine le temps de parler : Alfred ne doit ni s'entendre, ni
        # traiter ce qui a été capté pendant qu'il parlait. Placé ici et non
        # dans _speak_edge pour couvrir aussi le repli pyttsx3.
        _mute_until = float("inf")
        try:
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
        finally:
            _mute_until = time.monotonic() + 0.4
            _drain_queue()


def _bip():
    """Accusé de réception instantané dès que le prénom est reconnu, avant
    même de réfléchir. Un bip et non une phrase : une parole d'Alfred
    couperait celle de l'utilisateur."""
    global _mute_until
    if not CONFIG.get("beep_on_wake", True) or winsound is None:
        return
    _mute_until = time.monotonic() + 0.3
    try:
        winsound.Beep(880, 120)
    except Exception as e:
        print(f"⚠️  Bip impossible : {e}")


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


# Commandes courantes reconnues sans passer par le modèle : llama3.1 classait
# « mets play sur la vidéo » en ouverture d'application, et chaque passage
# par lui coûte plus d'une seconde. Tout ce qui ne correspond pas continue
# vers interpret_command() sans changement.
_ROUTES_DIRECTES = (
    (re.compile(r"\b(monte|augmente|hausse|plus fort)\b"), "volume_up"),
    (re.compile(r"\b(baisse|diminue|réduis|moins fort)\b"), "volume_down"),
    (re.compile(r"\b(suivante?|prochaine?|d'après|skip|passe)\b"), "next_track"),
    (re.compile(r"\b(précédente?|d'avant|reviens en arrière|recule)\b"), "prev_track"),
    (re.compile(r"\b(play|pause|lecture|reprends?|relance|continue|"
                r"(re)?mets?( la| le)?( musique| vidéo| son| film)|"
                r"(arr[êe]te|stoppe?|coupe)( la| le)( musique| vidéo| son| film))\b"),
     "play_pause"),
    (re.compile(r"\b(quelle heure|l'heure|il est quelle heure|heure est-il)\b"), "time"),
)
_MOTIF_OUVRIR = re.compile(
    r"\b(?:ouvre|ouvrir|lance|lancer|démarre|démarrer|affiche)\s+"
    r"(?:le |la |l'|les |mon |ma |mes |un |une )?(.+)$"
)

_REPONSES_DIRECTES = {
    "play_pause": ("C'est fait, monsieur.", "Voilà qui est fait.",
                   "À votre convenance, monsieur."),
    "next_track": ("Piste suivante, monsieur.", "Passons à la suivante."),
    "prev_track": ("Piste précédente, monsieur.", "Revenons en arrière."),
    "volume_up": ("Un peu plus fort, monsieur.", "Je monte le son."),
    "volume_down": ("Un peu moins fort, monsieur.", "Je baisse le son."),
    "time": ("",),
    "open_app": ("{app} arrive à l'instant, monsieur.", "J'ouvre {app} pour vous."),
}


def _route_directe(texte):
    """(action, cible, réponse) pour une commande courante, ou None si elle
    doit passer par le modèle."""
    n = _normalize(texte)
    if not n:
        return None

    for motif, action in _ROUTES_DIRECTES:
        if motif.search(n):
            return action, "", random.choice(_REPONSES_DIRECTES[action])

    m = _MOTIF_OUVRIR.search(n)
    if m:
        cible = m.group(1).strip(" ,.;:!?'\"")
        if _app_key(cible) in _APPS_INDEX:
            return ("open_app", cible,
                    random.choice(_REPONSES_DIRECTES["open_app"]).format(app=cible.capitalize()))
    return None


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
    reponse = _normalize(listen(timeout=8))
    print(f"Confirmation entendue : {reponse!r}")
    return any(mot in reponse for mot in CONFIRM_WORDS)


# ============ MAIN LOOP ============
def run(stop_event=None):
    """Boucle principale de l'assistant. S'arrête dès que stop_event est levé
    (si fourni), sinon tourne jusqu'à un mot d'arrêt vocal ou Ctrl+C."""
    global _stop_event
    if stop_event is None:
        stop_event = threading.Event()
    _stop_event = stop_event

    print("=" * 50)
    print(f"🎙️  {CONFIG['assistant_name'].upper()} - ASSISTANT VOCAL LOCAL")
    print("=" * 50)

    _ensure_loaded()
    if str(CONFIG.get("wake_word") or "").strip():
        speak(f"Eh bien, {CONFIG['assistant_name']} à votre service. "
              f"Appelez-moi par mon nom quand vous aurez besoin de moi.")
    else:
        speak(f"Eh bien, {CONFIG['assistant_name']} à votre service. "
              f"Dites-moi ce dont vous avez besoin.")

    echecs = 0
    try:
        while not stop_event.is_set():
            echecs = _tour_de_boucle(stop_event, echecs)
            if echecs is None:
                break
    finally:
        _stop_capture()


def _tour_de_boucle(stop_event, echecs):
    """Un énoncé traité. Retourne le nouveau compteur d'échecs, ou None pour
    arrêter la boucle."""
    try:
        user_input = listen()

        if stop_event.is_set():
            return None

        if not user_input:
            return echecs

        reveille, commande_texte = _wake_and_command(user_input)
        if not reveille:
            print(f"… ignoré, mot de réveil absent : {user_input!r}")
            return echecs

        _bip()

        if not commande_texte:
            # Seul le prénom a été prononcé : on acquitte et on attend la
            # commande, un énoncé entier cette fois.
            speak("Oui monsieur, je vous écoute.")
            commande_texte = listen(timeout=8)
            if stop_event.is_set():
                return None
            if not commande_texte or _est_appel_seul(commande_texte):
                return echecs

        if _normalize(commande_texte) in STOP_PHRASES:
            speak("Très bien, je reste à votre entière disposition.")
            return None

        directe = _route_directe(commande_texte)
        if directe:
            action, target, response = directe
            print(f"⚡ Commande directe : {action} {target!r}")
        else:
            command = interpret_command(commande_texte)
            action = str(command.get("action") or "help")
            target = str(command.get("target") or "")
            response = str(command.get("response") or "Commande non reconnue")

        if action in DESTRUCTIVE_ACTIONS and not _confirm_destructive(action):
            speak("Fort bien, je n'y touche pas.")
            return echecs

        resultat = execute_action(action, target)
        if action == "time" and resultat:
            response = f"Il est {resultat}, monsieur."
        elif action == "web_search" and resultat:
            response = resultat
        elif resultat is False:
            response = "Je n'ai pas pu m'en occuper, monsieur."
        speak(response)
        return 0

    except KeyboardInterrupt:
        speak("Fort bien, je me retire pour cette fois.")
        return None
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
        return echecs



if __name__ == "__main__":
    print("\n⚙️  Prérequis:")
    print("1. Installer Ollama: https://ollama.ai")
    print(f"2. Télécharger le modèle: ollama pull {CONFIG['ollama_model']}")
    print("3. Lancer Ollama: ollama serve\n")

    input("Appuyez sur Entrée quand Ollama est lancé...")

    run()
