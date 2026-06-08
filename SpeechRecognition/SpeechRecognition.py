# Speech Recognition v1.1
import os
import speech_recognition as sr
from pynput import keyboard
import time
class SpeechRecognition:
    def __init__(self):
        self.listening = True
        self.transcription = []
        self.running = True

        self.rec = sr.Recognizer()
        self.mic = sr.Microphone()

        print("Please be silent for a second to collect ambience noise")
        time.sleep(0.5)
        with self.mic as source:
            self.rec.adjust_for_ambient_noise(source)
        print("CONTROLS")
        print("Space to toggle listening")
        print("Left Shift to print transcript")
        print("ESC to quit")
        print("LISTENING...")
    def audioCallback(self, recognizer, audio):
        if(not self.listening):
            return
        else:
            try:
                text = recognizer.recognize_google(audio)
                self.transcription.append(text)
                print(f"live output: {text}")
            except sr.UnknownValueError:
                pass
            except sr.RequestError as e:
                print(f"API error: {e}")

    def on_press(self,key):
        if(key == keyboard.Key.space):
            self.listening = not self.listening
            if(self.listening): print("LISTENING...")
            else: print("PAUSED")
        elif(key == keyboard.Key.shift_l):
            self.printTranscript()
        elif(key == keyboard.Key.esc):
            running = False
            return False
    def printTranscript(self):
        if(self.transcription): print(" ".join(self.transcription))
        else: print("No transcript")
    def run(self):
        background_listener = self.rec.listen_in_background(self.mic, self.audioCallback)
        with keyboard.Listener(on_press=self.on_press) as listener:
            while(self.running):
                time.sleep(0.1)
        background_listener(wait_for_stop=False)
        self.printTranscript()


if __name__ == "__main__":
    app = SpeechRecognition()
    app.run()