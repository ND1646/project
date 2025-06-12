import os
import json
import pytesseract
from PIL import Image
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify, session, flash
from werkzeug.utils import secure_filename
from PyPDF2 import PdfReader
from docx import Document
from flask_login import LoginManager, login_user, logout_user, login_required, UserMixin, current_user
from googletrans import Translator
import google.generativeai as genai

# Gemini API setup
genai.configure(api_key=os.getenv("GEMINI_API_KEY") or "YOUR_GEMINI_API_KEY")

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'docx', 'txt'}
USERS_FILE = 'users.json'

app = Flask(__name__)
app.secret_key = 'your_secret_key'
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

# --- User Authentication ---
class User(UserMixin):
    def __init__(self, id, username, password):
        self.id = id
        self.username = username
        self.password = password

def load_users():
    if not os.path.exists(USERS_FILE):
        with open(USERS_FILE, "w") as f:
            json.dump([{"id": 1, "username": "ND", "password": "test"}], f)
    with open(USERS_FILE, "r") as f:
        users = json.load(f)
    return [User(**u) for u in users]

def get_user_by_username(username):
    users = load_users()
    for user in users:
        if user.username == username:
            return user
    return None

@login_manager.user_loader
def load_user(user_id):
    users = load_users()
    for user in users:
        if str(user.id) == str(user_id):
            return user
    return None

# --- File utilities ---
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def user_folder():
    if not current_user.is_authenticated:
        return app.config['UPLOAD_FOLDER']
    folder = os.path.join(app.config['UPLOAD_FOLDER'], current_user.username)
    if not os.path.exists(folder):
        os.makedirs(folder)
    return folder

def extract_text_pdf(filepath):
    try:
        with open(filepath, 'rb') as f:
            reader = PdfReader(f)
            text = ""
            for page in reader.pages:
                text += page.extract_text() or ""
            return text.strip()
    except Exception as e:
        return f"[Error extracting PDF text: {e}]"

def extract_text_docx(filepath):
    try:
        doc = Document(filepath)
        text = "\n".join(para.text for para in doc.paragraphs)
        return text.strip()
    except Exception as e:
        return f"[Error extracting DOCX text: {e}]"

def extract_text_image(filepath):
    try:
        text = pytesseract.image_to_string(Image.open(filepath))
        return text.strip()
    except Exception as e:
        return f"[Error extracting image text: {e}]"

# --- Gemini AI Chat Logic ---
def gemini_chat_response(history, user_message, lang='en'):
    """
    history: list of {'role': 'user'|'assistant', 'content': str}
    user_message: the latest user message (already in English)
    lang: language code for response (default 'en')
    """
    try:
        model = genai.GenerativeModel("gemini-1.5-pro")  # Use your preferred Gemini model
        # Gemini expects list of dicts like [{'role': 'user', 'parts': ['message']}]
        gemini_history = []
        for msg in history:
            if msg["role"] in ["user", "assistant"]:
                gemini_history.append({"role": msg["role"], "parts": [msg["content"]]})
        gemini_history.append({"role": "user", "parts": [user_message]})
        convo = model.start_chat(history=gemini_history[:-1])  # start chat with prior history
        gemini_response = convo.send_message(user_message)
        return gemini_response.text
    except Exception as e:
        return f"[Gemini AI Error: {e}]"

# --- Routes ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = get_user_by_username(username)
        if user and user.password == password:
            login_user(user)
            session["chat_history"] = []
            return redirect(url_for('index'))
        flash("Invalid credentials. Try ND / test for demo.")
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("Logged out.")
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('chat.html', username=current_user.username)

@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        folder = user_folder()
        filepath = os.path.join(folder, filename)
        file.save(filepath)
        ext = filename.rsplit('.', 1)[1].lower()
        file_url = url_for('uploaded_file', username=current_user.username, filename=filename)
        last_file = {"filename": filename, "filepath": filepath, "url": file_url, "type": ext}
        session['last_file'] = last_file

        if ext in ['png', 'jpg', 'jpeg', 'gif']:
            ocr_text = extract_text_image(filepath)
            session['last_file_text'] = ocr_text
            return jsonify({'filename': filename, 'url': file_url, 'type': 'image', 'ocr': ocr_text[:300]}), 200
        elif ext == 'pdf':
            text = extract_text_pdf(filepath)
            session['last_file_text'] = text
            summary = text[:300] + ("..." if len(text) > 300 else "")
            return jsonify({'filename': filename, 'url': file_url, 'type': 'pdf', 'summary': summary}), 200
        elif ext == 'docx':
            text = extract_text_docx(filepath)
            session['last_file_text'] = text
            summary = text[:300] + ("..." if len(text) > 300 else "")
            return jsonify({'filename': filename, 'url': file_url, 'type': 'docx', 'summary': summary}), 200
        elif ext == 'txt':
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            session['last_file_text'] = text
            summary = text[:300] + ("..." if len(text) > 300 else "")
            return jsonify({'filename': filename, 'url': file_url, 'type': 'txt', 'summary': summary}), 200
        else:
            return jsonify({'filename': filename, 'url': file_url, 'type': 'document'}), 200
    return jsonify({'error': 'File type not allowed'}), 400

@app.route('/uploads/<username>/<filename>')
@login_required
def uploaded_file(username, filename):
    folder = os.path.join(app.config['UPLOAD_FOLDER'], username)
    return send_from_directory(folder, filename)

@app.route('/view_text/<filename>')
@login_required
def view_text(filename):
    folder = user_folder()
    filepath = os.path.join(folder, filename)
    ext = filename.rsplit('.', 1)[1].lower()
    text = ""
    if ext == 'pdf':
        text = extract_text_pdf(filepath)
    elif ext == 'docx':
        text = extract_text_docx(filepath)
    elif ext == 'txt':
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    elif ext in ['png', 'jpg', 'jpeg', 'gif']:
        text = extract_text_image(filepath)
    else:
        text = "[Cannot display file content]"
    return render_template('view_text.html', filename=filename, text=text)

@app.route('/files')
@login_required
def files():
    folder = user_folder()
    files = []
    for fname in os.listdir(folder):
        fpath = os.path.join(folder, fname)
        ext = fname.rsplit('.', 1)[1].lower()
        files.append({
            "name": fname,
            "type": ext,
            "url": url_for('uploaded_file', username=current_user.username, filename=fname),
            "view_text_url": url_for('view_text', filename=fname) if ext in ['pdf','docx','txt','png','jpg','jpeg','gif'] else None
        })
    return render_template('files.html', files=files)

@app.route('/chat', methods=['POST'])
@login_required
def chat():
    user_message = request.form.get('message', '').strip()
    lang = request.form.get('lang', 'en')
    translator = Translator()

    if not user_message:
        return jsonify({'response': "Please enter a message."})

    # Chat history for AI context
    chat_history = session.get("chat_history", [])
    history = chat_history[-8:]  # keep last 8 messages for context

    # Multilingual support
    query_for_ai = user_message
    if lang != 'en':
        query_for_ai = translator.translate(user_message, dest='en').text

    # Gemini AI Chatbot reply
    bot_reply = gemini_chat_response(history, query_for_ai, lang='en')
    if lang != 'en':
        bot_reply = translator.translate(bot_reply, dest=lang).text

    # Update chat history (user then bot)
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": bot_reply})
    session["chat_history"] = history

    return jsonify({'response': bot_reply})

if __name__ == '__main__':
    app.run(debug=True)