import cv2
import numpy as np
import serial
import time
import requests
import smtplib
import os
import glob
from datetime import datetime
from email.message import EmailMessage
from flask import Flask, Response
import threading
from pyngrok import ngrok

# ================= CONFIG =================
SERIAL_PORT = None
BAUD_RATE = 9600

TELEGRAM_TOKEN = "8490765768:AAFU-Vpi0HAiS5_2V2mcboWYeiG8W4neiVE"
CHAT_ID = "7175315173"

CONFIDENCE_THRESHOLD = 70
FACE_TIMEOUT = 3
TELEGRAM_COOLDOWN = 30

EMAIL_ADDRESS = "growpfiveim312@gmail.com"
EMAIL_APP_PASSWORD = "qerlwnbhfcaprcll"
RECEIVER_EMAIL = "ocmaikreedvejee6@gmail.com"

NGROK_AUTH_TOKEN = "3CNooZSFRM64UqMFHQhvjL167bU_4RZuEZf7oztKsnwVyVcHJ"

PERSON_DETECT_INTERVAL = 3
VIDEO_TIMEOUT = 3

# ================= GLOBALS =================
last_face_time = 0
last_telegram_time = 0
system_on = False

frame_global = None
lock = threading.Lock()

frame_count = 0

recording = False
video_writer = None
last_intruder_time = 0

cap = None
arduino = None
arduino_port = None

STREAM_URL = None

# ================= NGROK =================
ngrok.set_auth_token(NGROK_AUTH_TOKEN)

# ================= MODELS =================
recognizer = cv2.face.LBPHFaceRecognizer_create()
recognizer.read("trainer1.yml")
label_map = np.load("labels1.npy", allow_pickle=True).item()

face_cascade = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
)

hog = cv2.HOGDescriptor()
hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

# ================= ARDUINO HANDLING =================
def find_arduino():
    ports = glob.glob('/dev/ttyACM*') + glob.glob('/dev/ttyUSB*')
    return ports[0] if ports else None


def connect_arduino():
    global arduino, arduino_port

    port = find_arduino()

    if not port:
        arduino = None
        arduino_port = None
        return

    if port != arduino_port:
        try:
            arduino = serial.Serial(port, BAUD_RATE, timeout=1)
            time.sleep(2)
            arduino_port = port
            print(f"✅ Arduino connected: {port}")
        except Exception as e:
            print("❌ Arduino connect failed:", e)
            arduino = None


def safe_arduino_write(cmd):
    global arduino
    try:
        if arduino and arduino.is_open:
            arduino.write(cmd)
    except:
        arduino = None

# ================= CAMERA =================
def connect_camera():
    global cap
    while True:
        cap = cv2.VideoCapture(0)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 360)
            print("✅ Camera connected")
            return
        time.sleep(2)

# ================= FLASK =================
app = Flask(__name__)

def generate_frames():
    global frame_global
    while True:
        with lock:
            if frame_global is None:
                continue
            frame = frame_global.copy()

        _, buffer = cv2.imencode('.jpg', frame)

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')


@app.route('/')
def video_feed():
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


def run_flask():
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

# ================= ALERTS =================
def send_telegram(image_path):
    global last_telegram_time, STREAM_URL

    if time.time() - last_telegram_time < TELEGRAM_COOLDOWN:
        return

    try:
        with open(image_path, "rb") as photo:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                files={"photo": photo},
                data={"chat_id": CHAT_ID}
            )
        last_telegram_time = time.time()
    except:
        pass


def send_email(image_path):
    try:
        msg = EmailMessage()
        msg['Subject'] = "Intruder Alert"
        msg['From'] = EMAIL_ADDRESS
        msg['To'] = RECEIVER_EMAIL

        with open(image_path, 'rb') as f:
            msg.add_attachment(f.read(), maintype='image', subtype='jpeg')

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            smtp.send_message(msg)
    except:
        pass

# ================= RECORDING =================
def start_recording(frame):
    global recording, video_writer

    os.makedirs("videos", exist_ok=True)

    path = f"videos/intruder_{datetime.now().strftime('%Y%m%d_%H%M%S')}.avi"

    fourcc = cv2.VideoWriter_fourcc(*'XVID')
    h, w, _ = frame.shape

    video_writer = cv2.VideoWriter(path, fourcc, 20, (w, h))
    recording = True

    snap = path.replace(".avi", ".jpg")
    cv2.imwrite(snap, frame)

    threading.Thread(target=send_telegram, args=(snap,)).start()
    threading.Thread(target=send_email, args=(snap,)).start()


def stop_recording():
    global recording, video_writer

    if video_writer:
        video_writer.release()

    recording = False

# ================= MAIN =================
def main():
    global frame_global, frame_count
    global recording, last_intruder_time
    global system_on, last_face_time

    print("🚀 System Starting...")

    connect_camera()

    tunnel = ngrok.connect(5000, "http")
    global STREAM_URL
    STREAM_URL = tunnel.public_url
    print("🌍 Stream:", STREAM_URL)

    threading.Thread(target=run_flask, daemon=True).start()

    while True:
        connect_arduino()

        ret, frame = cap.read()
        if not ret:
            cap.release()
            time.sleep(2)
            connect_camera()
            continue

        frame = cv2.resize(frame, (640, 360))

        with lock:
            frame_global = frame.copy()

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        faces = face_cascade.detectMultiScale(gray, 1.2, 5)
        face_detected = len(faces) > 0

        frame_count += 1
        person_detected = False

        if frame_count % PERSON_DETECT_INTERVAL == 0:
            boxes, _ = hog.detectMultiScale(frame, winStride=(8, 8))
            person_detected = len(boxes) > 0

        intruder = False

        # FACE CHECK
        for (x, y, w, h) in faces:
            face = gray[y:y+h, x:x+w]

            try:
                label, conf = recognizer.predict(face)
                name = label_map.get(label, "Unknown") if conf < CONFIDENCE_THRESHOLD else "Unknown"
            except:
                name = "Unknown"

            if name == "Unknown":
                intruder = True

            cv2.rectangle(frame, (x, y), (x+w, y+h), (255, 0, 0), 2)

        if person_detected and not face_detected:
            intruder = True

        now = time.time()

        # RELAY CONTROL
        if person_detected or face_detected:
            last_face_time = now
            if not system_on:
                safe_arduino_write(b'ON\n')
                system_on = True
        else:
            if system_on and (now - last_face_time > FACE_TIMEOUT):
                safe_arduino_write(b'OFF\n')
                system_on = False

        # RECORDING
        if intruder:
            last_intruder_time = now
            if not recording:
                start_recording(frame)

        if recording and video_writer:
            video_writer.write(frame)

        if recording and (now - last_intruder_time > VIDEO_TIMEOUT):
            stop_recording()

        time.sleep(0.03)


# ================= START =================
if __name__ == "__main__":
    main()
