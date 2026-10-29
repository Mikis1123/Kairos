import os
import urllib.request
import cv2
import numpy as np

MODEL_DIR = os.path.join(os.getcwd(), "models")
os.makedirs(MODEL_DIR, exist_ok=True)

YUNET_PATH = os.path.join(MODEL_DIR, "face_detection_yunet_2023mar.onnx")
SFACE_PATH = os.path.join(MODEL_DIR, "face_recognition_sface_2021dec.onnx")
SFACE_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"

# 1. Stažení modelu pro biometrický otisk (SFace ~ 1.2 MB)
if not os.path.exists(SFACE_PATH):
    print("Stahuji model SFace pro biometrii (cca 1.2 MB)...")
    urllib.request.urlretrieve(SFACE_URL, SFACE_PATH)
    print("Model SFace stažen.")

# 2. Inicializace modelů
recognizer = cv2.FaceRecognizerSF.create(model=SFACE_PATH, config="")

# 3. Načtení fotky s tváří
img_path = os.path.join(os.getcwd(), ".cache", "thumbnails", "thumb_15.jpg")
img = cv2.imread(img_path)
h, w, _ = img.shape

detector = cv2.FaceDetectorYN.create(
    model=YUNET_PATH,
    config="",
    input_size=(w, h),
    score_threshold=0.55,
    nms_threshold=0.3
)

_, faces = detector.detect(img)

if faces is not None and len(faces) > 0:
    face = faces[0]
    
    # 4. Afinní zarovnání (Face Alignment podle očí a úst)
    aligned_face = recognizer.alignCrop(img, face)
    cv2.imwrite("test_face_aligned.jpg", aligned_face)
    print("-> Zarovnaná tvář podle očí uložena: 'test_face_aligned.jpg'")

    # 5. Extrakce biometrického vektoru (128 čísel)
    feature = recognizer.feature(aligned_face)
    feature_norm = feature / np.linalg.norm(feature)
    
    print(f"Biometrický vektor úspěšně vytvořen! Rozměr: {feature.shape}")
    print(f"Prvních 5 hodnot otisku: {feature_norm[0][:5].round(4)}")

    # 6. Test kosínové podobnosti (tvář sama se sebou musí mít shodu 1.00)
    score = recognizer.match(aligned_face, aligned_face, cv2.FaceRecognizerSF_FR_COSINE)
    print(f"Ověření matematické shody: {score:.4f} (1.00 = identický člověk)")