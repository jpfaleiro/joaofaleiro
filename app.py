from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, flash, session
)
from functools import wraps
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

import os, re, json

# -----------------------------------------------------------------------------
# Config básica
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = "dev-secret"  # troque em produção
CORS(app)

# .env (precisa vir ANTES de ler variáveis)
load_dotenv()
ADMIN_EMAILS = set([
    e.strip().lower()
    for e in os.getenv("ADMIN_EMAILS", "").split(",")
    if e.strip()
])

# -----------------------------------------------------------------------------
# Chatbot (Gemini opcional)
# -----------------------------------------------------------------------------
try:
    import google.generativeai as genai
except Exception:
    genai = None

API_KEY = os.getenv("API_KEY")
_model = None
if API_KEY and genai is not None:
    try:
        genai.configure(api_key=API_KEY)
        _model = genai.GenerativeModel("gemini-1.5-flash")
        print("[chat] Gemini habilitado.")
    except Exception as e:
        print("[chat] Gemini desabilitado:", e)


def _denumerate(text: str) -> str:
    """Remove numeração/marcadores no início das linhas e limpa quebras."""
    lines = (text or "").splitlines()
    cleaned = [re.sub(r'^\s*(?:\d+|[•\-])[\.)\-:]\s*', '', ln) for ln in lines]
    out = "\n".join(cleaned).strip()
    return re.sub(r'\n{3,}', '\n\n', out)


def _clean(text: str, max_words: int = 120) -> str:
    t = _denumerate(text)
    words = t.split()
    return (" ".join(words[:max_words]).rstrip() + "...") if len(words) > max_words else t


# Fallback “clean” (sem numeração)
_FALLBACK = {
    ("currículo", "curriculo", "cv", "resumo"):
        "Use uma página com conquistas mensuráveis, destaque 3–5 projetos relevantes, organize tecnologias por nível e inclua links de GitHub e LinkedIn.",
    ("entrevista", "recrutador", "processo"):
        "Pesquise a empresa, explique projetos com desafios e resultados, mostre aprendizados e leve perguntas objetivas para o final.",
    ("estágio", "estagio", "junior", "primeiro emprego"):
        "Monte um portfólio simples, contribua em projetos abertos, participe de eventos, mantenha o LinkedIn ativo e envie candidaturas com regularidade.",
    ("conectati", "site", "cadastro", "contato", "o que é", "o que e"):
        "Veja ‘O que é’ para entender a proposta, cadastre-se no topo, fale conosco pelo formulário e confira vagas na Home usando o e-mail do UniCEUB."
}
_DEFAULT_FALLBACK = (
    "Explique seu objetivo e contexto, liste habilidades atuais e projetos, peça feedback específico e compartilhe links de GitHub e LinkedIn."
)


def _fallback_reply(text: str) -> str:
    low = (text or "").lower()
    for keys, rep in _FALLBACK.items():
        if any(k in low for k in keys):
            return rep
    return _DEFAULT_FALLBACK


# -----------------------------------------------------------------------------
# Decorators de proteção
# -----------------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_email"):
            flash("Faça login para acessar esta página.")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_email"):
            flash("Faça login para acessar.", "err")
            return redirect(url_for("login", next=request.path))
        if not session.get("is_admin"):
            return jsonify({"error": "Acesso restrito ao admin."}), 403
        return view(*args, **kwargs)
    return wrapped


# -----------------------------------------------------------------------------
# Persistência simples (arquivos JSON em ./instance)
# -----------------------------------------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(__file__), "instance")
os.makedirs(DATA_DIR, exist_ok=True)

USERS_JSON = os.path.join(DATA_DIR, "users.json")
PERFIS_JSON = os.path.join(DATA_DIR, "perfis.json")
VAGAS_JSON  = os.path.join(DATA_DIR, "vagas.json")
HOME_JSON   = os.path.join(DATA_DIR, "home.json")

def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _load_users():     return _load_json(USERS_JSON, {})
def _save_users(d):    _save_json(USERS_JSON, d)

def _load_profiles():  return _load_json(PERFIS_JSON, {})
def _save_profiles(d): _save_json(PERFIS_JSON, d)

def _load_vagas():     return _load_json(VAGAS_JSON, [])
def _save_vagas(lst):  _save_json(VAGAS_JSON, lst)

def _next_id(vagas):   return (max([v["id"] for v in vagas], default=0) + 1)

# ---- Conteúdo da Home (3 cards) ----
DEFAULT_HOME = {
    "cards": {
        "vagas":   {"title": "Vagas & Estágios",
                    "text": "Oportunidades reais com empresas parceiras e startups da região."},
        "projetos":{"title": "Projetos & Desafios",
                    "text": "Prática guiada para fortalecer seu portfólio e habilidade técnica."},
        "eventos": {"title": "Eventos & Comunidade",
                    "text": "Meetups, talks e hackathons para ampliar seu networking."}
    }
}

def _load_home():
    data = _load_json(HOME_JSON, {})
    out = DEFAULT_HOME.copy()
    out["cards"] = {**DEFAULT_HOME["cards"], **data.get("cards", {})}
    return out

def _save_home(data):
    _save_json(HOME_JSON, data)


# -----------------------------------------------------------------------------
# Autenticação
# -----------------------------------------------------------------------------
@app.route("/cadastro", methods=["GET", "POST"])
def cadastro():
    if request.method == "POST":
        nome  = (request.form.get("nome")  or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        senha = (request.form.get("senha") or "").strip()

        if not nome or not email or not senha:
            flash("Preencha nome, e-mail e senha.")
            return redirect(url_for("cadastro"))
        if len(senha) < 6:
            flash("A senha deve ter pelo menos 6 caracteres.")
            return redirect(url_for("cadastro"))

        users = _load_users()
        if email in users:
            flash("E-mail já cadastrado. Faça login.")
            return redirect(url_for("login"))

        users[email] = {"nome": nome, "senha_hash": generate_password_hash(senha)}
        _save_users(users)

        # Cria perfil básico
        perfis = _load_profiles()
        perfis.setdefault(email, {
            "nome": nome, "email": email, "area_alvo": "", "objetivo": "",
            "github": "", "linkedin": "", "cv_url": "",
            "skills": {
                "desenvolvimento": 0, "banco_dados": 0, "redes": 0,
                "dados": 0, "seguranca": 0, "devops_cloud": 0
            }
        })
        _save_profiles(perfis)

        session["user_email"] = email
        session["is_admin"] = (email in ADMIN_EMAILS)
        flash("Cadastro concluído! Bem-vindo(a).")
        return redirect(url_for("perfil"))

    return render_template("cadastro.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        senha = (request.form.get("senha") or "").strip()
        if not email or not senha:
            flash("Informe e-mail e senha.")
            return redirect(url_for("login"))

        users = _load_users()
        user = users.get(email)
        if not user or not check_password_hash(user.get("senha_hash", ""), senha):
            flash("E-mail ou senha inválidos.")
            return redirect(url_for("login"))

        session["user_email"] = email
        session["is_admin"]  = (email in ADMIN_EMAILS)
        flash("Login efetuado!")
        return redirect(request.args.get("next") or url_for("perfil"))

    return render_template("login.html")


@app.get("/logout")
def logout():
    session.pop("user_email", None)
    session.pop("is_admin", None)
    flash("Você saiu da sua conta.")
    return redirect(url_for("home"))


# -----------------------------------------------------------------------------
# Rotas principais
# -----------------------------------------------------------------------------
@app.route("/")
def home():
    vagas = _load_vagas()
    home_content = _load_home()
    return render_template(
        "index.html",
        vagas=vagas,
        home=home_content,
        is_admin=session.get("is_admin", False)
    )


@app.route("/o-que-e")
def o_que_e():
    return render_template("o_que_e.html")


# Perfil (edição + skills 0–100)
@app.route("/perfil", methods=["GET", "POST"])
@login_required
def perfil():
    perfis = _load_profiles()
    email = session["user_email"]  # sempre o e-mail logado

    def _to_int(v):
        try:
            n = int(v)
            return 0 if n < 0 else 100 if n > 100 else n
        except Exception:
            return 0

    if request.method == "POST":
        nome     = (request.form.get("nome") or "").strip()
        area     = (request.form.get("area_alvo") or "").strip()
        objetivo = (request.form.get("objetivo") or "").strip()
        github   = (request.form.get("github") or "").strip()
        linkedin = (request.form.get("linkedin") or "").strip()
        cv_url   = (request.form.get("cv_url") or "").strip()

        # valores vêm dos <input type="hidden"> atualizados por static/perfil.js
        skills = {
            "desenvolvimento": _to_int(request.form.get("sk_dev")),
            "banco_dados":     _to_int(request.form.get("sk_db")),
            "redes":           _to_int(request.form.get("sk_net")),
            "dados":           _to_int(request.form.get("sk_data")),
            "seguranca":       _to_int(request.form.get("sk_sec")),
            "devops_cloud":    _to_int(request.form.get("sk_devops")),
        }

        perfis[email] = {
            "nome": nome, "email": email,
            "area_alvo": area, "objetivo": objetivo,
            "github": github, "linkedin": linkedin, "cv_url": cv_url,
            "skills": skills
        }
        _save_profiles(perfis)
        flash("Perfil salvo com sucesso!")
        return redirect(url_for("perfil"))

    # GET: carrega o perfil do logado e garante defaults
    perfil = perfis.get(email) or {}
    perfil.setdefault("email", email)
    perfil.setdefault("skills", {})
    for k in ("desenvolvimento", "banco_dados", "redes", "dados", "seguranca", "devops_cloud"):
        perfil["skills"].setdefault(k, 0)

    return render_template("perfil.html", perfil=perfil, current_email=email)


# -----------------------------------------------------------------------------
# Vagas (público + admin CRUD)
# -----------------------------------------------------------------------------
@app.get("/api/vagas")
def api_vagas_public():
    return jsonify(_load_vagas())


@app.post("/api/admin/vagas")
@admin_required
def api_vagas_create():
    data = request.get_json(silent=True) or {}
    titulo  = (data.get("titulo") or "").strip()
    empresa = (data.get("empresa") or "").strip()
    url     = (data.get("url") or "").strip()
    if not titulo or not empresa:
        return jsonify({"error": "Informe título e empresa."}), 400

    vagas = _load_vagas()
    vaga = {"id": _next_id(vagas), "titulo": titulo, "empresa": empresa, "url": url}
    vagas.append(vaga)
    _save_vagas(vagas)
    return jsonify(vaga), 201


@app.put("/api/admin/vagas/<int:vid>")
@admin_required
def api_vagas_update(vid):
    data = request.get_json(silent=True) or {}
    vagas = _load_vagas()
    for v in vagas:
        if v["id"] == vid:
            v["titulo"]  = (data.get("titulo")  or v["titulo"]).strip()
            v["empresa"] = (data.get("empresa") or v["empresa"]).strip()
            v["url"]     = (data.get("url")     or v.get("url", "")).strip()
            _save_vagas(vagas)
            return jsonify(v)
    return jsonify({"error": "Vaga não encontrada."}), 404


@app.delete("/api/admin/vagas/<int:vid>")
@admin_required
def api_vagas_delete(vid):
    vagas = _load_vagas()
    new = [v for v in vagas if v["id"] != vid]
    if len(new) == len(vagas):
        return jsonify({"error": "Vaga não encontrada."}), 404
    _save_vagas(new)
    return jsonify({"ok": True})


# -----------------------------------------------------------------------------
# Home content (admin) – editar títulos e textos das 3 caixas
# -----------------------------------------------------------------------------
@app.put("/api/admin/home/<section>")
@admin_required
def api_home_update(section):
    section = section.lower()
    if section not in ("vagas", "projetos", "eventos"):
        return jsonify({"error": "Seção inválida."}), 400

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    text  = (data.get("text")  or "").strip()
    if not title or not text:
        return jsonify({"error": "Informe título e texto."}), 400

    home = _load_home()
    home["cards"][section]["title"] = title
    home["cards"][section]["text"]  = text
    _save_home(home)
    return jsonify({"ok": True, "section": section, "data": home["cards"][section]})


# -----------------------------------------------------------------------------
# Chat API
# -----------------------------------------------------------------------------
@app.post("/api/chat")
def api_chat():
    data = (request.get_json(silent=True) or request.form.to_dict() or {})
    user_message = (data.get("message") or "").strip()
    if not user_message:
        return jsonify({"error": "Campo 'message' é obrigatório."}), 400

    if _model:  # Gemini disponível
        site_facts = (
            "ConectaTI é plataforma acadêmica do UniCEUB. Páginas: Home, O que é, Cadastro e Contato. "
            "Na Home há trilhas e vagas em destaque. Cadastro pede nome, e-mail institucional e senha. "
            "Objetivo: ajudar estudantes a evoluírem em habilidades e portfólio."
        )
        prompt = f"""
Você é consultor de carreira em TI e tira dúvidas sobre o site ConectaTI. Responda em PT-BR.
Estilo: 1–2 parágrafos curtos (até ~120 palavras), sem listas e sem numeração. Vá direto ao ponto.
Se a pergunta for ampla, encerre com UMA pergunta de clarificação.

Contexto do site:
{site_facts}

Pergunta do usuário:
\"\"\"{user_message}\"\"\"
"""
        try:
            cfg = {"temperature": 0.25, "top_p": 0.9, "max_output_tokens": 180}
            resp = _model.generate_content(prompt, generation_config=cfg)
            reply = (getattr(resp, "text", "") or "").strip()
            if not reply:
                reply = _fallback_reply(user_message)
            reply = _clean(reply, max_words=120)
            return jsonify({"reply": reply})
        except Exception:
            return jsonify({"reply": _fallback_reply(user_message)})

    # Sem Gemini → fallback local
    return jsonify({"reply": _fallback_reply(user_message)})


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)
