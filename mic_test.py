#!/usr/bin/env python3
import RPi.GPIO as GPIO
import subprocess
import numpy as np
import wave
import time

BUTTON_PIN = 17
RECORD_SECONDS = 3
WAV_FILE = "/home/pranav/recording.wav"

GPIO.setmode(GPIO.BCM)
GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

def record_audio():
    print("Recording...")
    subprocess.run([
        "arecord", "-D", "plughw:0,0", "-f", "S16_LE", "-r", "44100",
        "-c", "2", "-d", str(RECORD_SECONDS), WAV_FILE
    ])
    print("Recording done.")

def analyze_audio():
    with wave.open(WAV_FILE, 'rb') as wf:
        n_channels = wf.getnchannels()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        raw_data = wf.readframes(n_frames)

    data = np.frombuffer(raw_data, dtype=np.int16)
    if n_channels == 2:
        data = data[::2]

    rms = np.sqrt(np.mean(data.astype(np.float64) ** 2))
    db = 20 * np.log10(rms) if rms > 0 else -float("inf")

    fft_result = np.fft.rfft(data)
    fft_freqs = np.fft.rfftfreq(len(data), d=1.0 / framerate)
    dominant_freq = fft_freqs[np.argmax(np.abs(fft_result))]

    print(f"Decibel level (relative): {db:.2f} dB")
    print(f"Dominant frequency: {dominant_freq:.2f} Hz")

def main():
    print("Waiting for button press... (Ctrl+C to exit)")
    try:
        while True:
            if GPIO.input(BUTTON_PIN) == GPIO.LOW:
                record_audio()
                analyze_audio()
                time.sleep(1)
            time.sleep(0.05)
    except KeyboardInterrupt:
        GPIO.cleanup()

if __name__ == "__main__":
    main()
