# Speech Recognition v1.1
import os
import threading

import speech_recognition as sr
from pynput import keyboard
import time


class SpeechRecognition:
    def __init__(self):
        # VARIABLES
        self.listening = True
        self.transcription = []
        self.running = True

        self.rec = sr.Recognizer()
        self.mic = sr.Microphone()

        # Setup and control info
        print("Please be silent for a second to collect ambience noise")
        time.sleep(0.5)
        with self.mic as source:
            self.rec.adjust_for_ambient_noise(source)
        print("CONTROLS")
        print("Space to toggle listening")
        print("Left Shift to print transcript")
        print("ESC to quit")
        print("LISTENING...")

    # Actively records the input audio, does not interpret
    def recordingAudio(self):
        with self.mic as source:
            while self.running:
                if self.listening:
                    try:
                        audio = self.rec.listen(source, timeout = 0.5)
                        self.parseAudio(audio)
                    except sr.WaitTimeoutError:
                        pass
                    except sr.UnknownValueError:
                        pass
                    except sr.RequestError as e:
                        print(f"API error: {e}")
                else:
                    # Not listening, do not record audio
                    time.sleep(0.1)

    # Turns speech to text using google
    def parseAudio(self, audio):
        try:
            # Getting text from google, adding to transcript
            text = self.rec.recognize_google(audio)
            self.transcription.append(text)
            print(f"live output: {text}")
        except sr.UnknownValueError:
            pass
        except sr.RequestError as e:
            print(f"API error: {e}")

    # Registers and acts from keystrokes
    def on_press(self,key):
        if(key == keyboard.Key.space): # Toggles listening mode
            self.listening = not self.listening
            if(self.listening): print("LISTENING...")
            else: print("PAUSED")

        elif(key == keyboard.Key.shift_l): # Prints the current transcript
            print(self.getTranscript())

        elif(key == keyboard.Key.esc): # Quits the program
            self.running = False
            return False

    # Joins and prints the current transcript
    def getTranscript(self):
        transcript = ""
        if(self.transcription): transcript = " ".join(self.transcription)
        return transcript

    # Central program to run
    def run(self):
        # Background recording task
        recordingThread = threading.Thread(target=self.recordingAudio)
        recordingThread.start()

        # Register keyboard inputs and do nothing
        with keyboard.Listener(on_press=self.on_press) as listener:
            while(self.running):
                time.sleep(0.1)

        # Quit
        recordingThread.join()
        self.printTranscript()

# Run program
if __name__ == "__main__":
    app = SpeechRecognition()
    app.run()