import os
import speech_recognition as sr
from pynput import keyboard
import time

listening = True
transcription = []

def listen(recognizer, audio):
    global transcription
    try:
        transcription.append(recognizer.recognize_google(audio))
        print("appending")
    except sr.UnknownValueError:
        print("Could not understand audio")
    except sr.RequestError as e:
        print(f"API error: {e}")

def endListening():
    global listening
    listening = False

def on_press(key):
    global listening
    if key == keyboard.Key.space:
        listening = False
        return False

def main():
    global listening
    global transcription
    rec = sr.Recognizer()
    mic = sr.Microphone()
    print(sr.Microphone.list_microphone_names())
    with mic as source:
        print("Please be silent for a second to collect ambience noise")
        rec.adjust_for_ambient_noise(source)

    backgroundRecorder = rec.listen_in_background(mic, listen)
    keyboardListener = keyboard.Listener(on_press=on_press)
    keyboardListener.start()
    while listening:
        time.sleep(0.2)
    backgroundRecorder(wait_for_stop = False)
    keyboardListener.stop()

    if transcription:
        fullScript = " ".join(transcription)
        print(fullScript)


if __name__ == "__main__":
    main() 