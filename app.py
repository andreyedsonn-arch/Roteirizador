import io
import difflib
import json
import math
import os
import random
import re
import time
import unicodedata
import sqlite3
import threading
import html
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

# ============================================================
# ROTEIRIZADOR DE ENTREGAS - gratuito, com conferência manual
# ============================================================
APP_TITLE = "Roteirizador de Entregas"
ORIGEM_PADRAO = "Rua Perseu Dantas, 191, Campina Grande, Paraíba, Brasil"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
PHOTON_URL = "https://photon.komoot.io/api/"
ORS_GEOCODE_URL = "https://api.openrouteservice.org/geocode/search"
# Chave do OpenRouteService (opcional): variável de ambiente ORS_API_KEY ou st.secrets["ORS_API_KEY"].
# Sem a chave, o motor é ignorado e o app funciona exatamente como antes.
ORS_API_KEY = os.getenv("ORS_API_KEY", "")
if not ORS_API_KEY:
    try:
        ORS_API_KEY = str(st.secrets.get("ORS_API_KEY", "") or "")
    except Exception:
        ORS_API_KEY = ""
ORS_FOCO_LAT, ORS_FOCO_LON = -7.2306, -35.8811  # Campina Grande: prioriza resultados próximos
ORS_CAMADAS_VALIDAS = {"address", "street", "venue"}  # evita aceitar centro de bairro/cidade
OSRM_TABLE_URL = "https://router.project-osrm.org/table/v1/driving/"
OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/driving/"
# Identificação da aplicação para os serviços públicos de geocodificação e roteamento.
# A variável de ambiente pode sobrescrever este padrão quando necessário.
USER_AGENT = os.getenv(
    "ROTEIRIZADOR_USER_AGENT",
    "RoteirizadorEntregas/2.2 (contato: andreyedsonn@gmail.com)",
)
CACHE_DB = Path("data/roteirizador_cache.sqlite3")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": USER_AGENT})
_HTTP_ADAPTER = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
HTTP.mount("https://", _HTTP_ADAPTER)
_CACHE_LOCK = threading.RLock()
_NOMINATIM_LOCK = threading.Lock()
_ULTIMA_CHAMADA_NOMINATIM = 0.0
NOMINATIM_INTERVALO = 1.1
FATOR_RODOVIARIO_HAVERSINE = 1.30
VELOCIDADE_FALLBACK_KMH = 25.0


def inicializar_cache():
    """Cria cache persistente SQLite automaticamente."""
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(CACHE_DB, timeout=15) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS geocodificacao (
            chave TEXT PRIMARY KEY, resultado TEXT NOT NULL,
            criado_em REAL NOT NULL, origem TEXT NOT NULL DEFAULT ''
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS respostas_geocoder (
            chave TEXT PRIMARY KEY, resultado TEXT NOT NULL,
            criado_em REAL NOT NULL, origem TEXT NOT NULL DEFAULT ''
        )""")
        # Remove resultados do OpenRouteService gravados antes da validação de cidade/estado.
        # Os novos resultados usam outro rótulo ("OpenRouteService v3") e não são afetados.
        conn.execute("DELETE FROM geocodificacao WHERE origem IN ('OpenRouteService', 'OpenRouteService v2')")
        conn.execute("DELETE FROM respostas_geocoder WHERE origem IN ('OpenRouteService', 'OpenRouteService v2')")
        conn.execute("""CREATE TABLE IF NOT EXISTS enderecos_confirmados (
            chave TEXT PRIMARY KEY, endereco TEXT NOT NULL, cidade TEXT NOT NULL,
            latitude REAL NOT NULL, longitude REAL NOT NULL,
            criado_em REAL NOT NULL, atualizado_em REAL NOT NULL,
            origem TEXT NOT NULL DEFAULT 'manual'
        )""")


def _cache_ler(tabela, chave, ttl_segundos=15552000):
    if tabela not in {"geocodificacao", "respostas_geocoder"}:
        raise ValueError("Tabela de cache inválida")
    try:
        with _CACHE_LOCK, sqlite3.connect(CACHE_DB, timeout=15) as conn:
            row = conn.execute(f"SELECT resultado, criado_em FROM {tabela} WHERE chave=?", (chave,)).fetchone()
        if not row:
            return None
        payload, criado = row
        if time.time() - float(criado) > ttl_segundos:
            return None
        return json.loads(payload)
    except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
        return None


def _cache_gravar(tabela, chave, resultado, origem=""):
    if tabela not in {"geocodificacao", "respostas_geocoder"}:
        raise ValueError("Tabela de cache inválida")
    try:
        payload = json.dumps(resultado, ensure_ascii=False, allow_nan=False)
        with _CACHE_LOCK, sqlite3.connect(CACHE_DB, timeout=15) as conn:
            conn.execute(
                f"INSERT INTO {tabela}(chave, resultado, criado_em, origem) VALUES(?,?,?,?) "
                "ON CONFLICT(chave) DO UPDATE SET resultado=excluded.resultado, "
                "criado_em=excluded.criado_em, origem=excluded.origem",
                (chave, payload, time.time(), origem),
            )
    except (sqlite3.Error, TypeError, ValueError):
        # Falha no cache não deve derrubar o roteirizador.
        pass


def _chave_geocoder(motor, consulta):
    return normalizar(f"{motor}|{consulta}|br|v2")


def _chave_endereco_confirmado(endereco, cidade):
    """Chave estável para reutilizar coordenadas confirmadas pelo usuário."""
    return normalizar(f"{endereco}|{cidade}|br|coords-confirmadas-v1")


def buscar_endereco_confirmado(endereco, cidade):
    """Retorna coordenadas salvas manualmente para endereço/cidade, se houver."""
    chave = _chave_endereco_confirmado(endereco, cidade)
    try:
        with _CACHE_LOCK, sqlite3.connect(CACHE_DB, timeout=15) as conn:
            row = conn.execute(
                "SELECT latitude, longitude, endereco, atualizado_em FROM enderecos_confirmados WHERE chave=?",
                (chave,),
            ).fetchone()
        if not row:
            return None
        lat, lon, nome, atualizado = row
        return {"lat": float(lat), "lon": float(lon), "nome": nome,
                "motor": "Coordenadas salvas", "atualizado_em": float(atualizado)}
    except (sqlite3.Error, ValueError, TypeError):
        return None


def salvar_endereco_confirmado(endereco, cidade, lat, lon, origem="manual"):
    """Persiste coordenadas verificadas para reutilização em execuções futuras."""
    endereco = " ".join(str(endereco or "").split()).strip(" ,")
    cidade = " ".join(str(cidade or "").split()).strip(" ,")
    lat, lon = float(lat), float(lon)
    if not endereco or not cidade or not (-90 <= lat <= 90) or not (-180 <= lon <= 180) or (lat == 0 and lon == 0):
        raise ValueError("Endereço, cidade ou coordenadas inválidas.")
    chave = _chave_endereco_confirmado(endereco, cidade)
    agora = time.time()
    with _CACHE_LOCK, sqlite3.connect(CACHE_DB, timeout=15) as conn:
        conn.execute(
            "INSERT INTO enderecos_confirmados "
            "(chave, endereco, cidade, latitude, longitude, criado_em, atualizado_em, origem) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET endereco=excluded.endereco, cidade=excluded.cidade, "
            "latitude=excluded.latitude, longitude=excluded.longitude, atualizado_em=excluded.atualizado_em, origem=excluded.origem",
            (chave, endereco, cidade, lat, lon, agora, agora, origem),
        )


def _get_nominatim(url, params, timeout=20, headers=None):
    """Serializa chamadas ao Nominatim neste processo e respeita intervalo mínimo."""
    global _ULTIMA_CHAMADA_NOMINATIM
    with _NOMINATIM_LOCK:
        espera = NOMINATIM_INTERVALO - (time.monotonic() - _ULTIMA_CHAMADA_NOMINATIM)
        if espera > 0:
            time.sleep(espera)
        resposta = HTTP.get(url, params=params, timeout=timeout, headers=headers)
        _ULTIMA_CHAMADA_NOMINATIM = time.monotonic()
    return resposta


inicializar_cache()

st.set_page_config(page_title=APP_TITLE, page_icon="🚚", layout="wide")


st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

/* ============================================================
   FLEET CONTROL PANEL — camada exclusivamente visual
   Glassmorphism escuro, cantos suaves, bordas quase invisíveis
   ============================================================ */
:root {
    --bg-deep: #060814;
    --glass: rgba(18, 18, 24, 0.70);
    --glass-soft: rgba(255, 255, 255, 0.035);
    --hairline: rgba(255, 255, 255, 0.08);
    --hairline-strong: rgba(255, 255, 255, 0.16);
    --text: #F5F7FB;
    --text-soft: #AEB4C4;
    --text-faint: #7C8398;
    --btn: rgba(34, 42, 66, 0.85);
    --btn-hover: rgba(52, 64, 98, 0.95);
    --accent: #FFE600;
    --radius: 16px;
    --radius-sm: 12px;
}

/* ---------- Tipografia ---------- */
.stApp, .stApp p, .stApp li, .stApp label, .stApp input, .stApp textarea,
.stApp button, .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6,
.stApp [data-baseweb], [data-testid="stMarkdownContainer"],
[data-testid="stMetricValue"], [data-testid="stMetricLabel"], [data-testid="stCaptionContainer"] {
    font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif !important;
}
.stApp [data-testid="stIconMaterial"], .stApp span.material-symbols-rounded, .stApp span.material-icons {
    font-family: 'Material Symbols Rounded', 'Material Icons' !important;
}
h1, h2, h3, h4, h5, h6 { color: var(--text) !important; text-shadow: none !important; }
h1 { font-size: 1.95rem !important; font-weight: 700 !important; letter-spacing: -0.025em !important; text-align: center !important; }
[data-testid="stElementContainer"]:has(h1) + [data-testid="stElementContainer"] [data-testid="stCaptionContainer"],
.element-container:has(h1) + .element-container [data-testid="stCaptionContainer"] { text-align: center !important; }
h2, h3 { font-weight: 600 !important; letter-spacing: -0.012em !important; }
h3 { font-size: 1.2rem !important; margin-top: 1.4rem !important; }
p, li, [data-testid="stMarkdownContainer"] { color: var(--text); }
[data-testid="stCaptionContainer"], .stCaption, small {
    color: var(--text-faint) !important; font-weight: 400 !important; font-size: .83rem !important;
}
[data-testid="stWidgetLabel"] p, label p {
    color: var(--text-soft) !important; font-weight: 500 !important; font-size: .84rem !important;
}
::selection { background: rgba(255, 230, 0, .22); }

/* ---------- Fundo do app e lateral ---------- */
html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"],
[data-testid="stMainBlockContainer"] { background: var(--bg-deep) !important; color: var(--text) !important; }
[data-testid="stAppViewContainer"] {
    background-image:
        radial-gradient(1100px 520px at 10% -10%, rgba(58, 76, 160, .20), transparent 60%),
        radial-gradient(900px 480px at 100% 0%, rgba(255, 230, 0, .035), transparent 55%) !important;
}
[data-testid="stHeader"] { background: transparent !important; border: none !important; box-shadow: none !important; }
[data-testid="stDecoration"] { display: none !important; }
footer { visibility: hidden; }
[data-testid="stSidebar"], [data-testid="stSidebar"] > div {
    background: var(--bg-deep) !important;
    border-right: 1px solid rgba(255, 255, 255, .06) !important;
}
[data-testid="stSidebar"] h2 {
    font-size: .8rem !important; font-weight: 600 !important; letter-spacing: .14em !important;
    text-transform: uppercase; color: var(--text-soft) !important;
}
[data-testid="stMainBlockContainer"] { padding-top: 2.4rem; padding-bottom: 3.5rem; }
hr, [data-testid="stDivider"] { display: none !important; }

/* ---------- Cards em vidro ---------- */
[data-testid="stMetric"], [data-testid="stAlert"],
[data-testid="stExpander"] details {
    background: var(--glass) !important;
    -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px);
    border: 1px solid var(--hairline) !important;
    border-radius: var(--radius) !important;
    box-shadow: 0 10px 32px rgba(0, 0, 0, .35) !important;
}
[data-testid="stExpander"] { border: none !important; background: transparent !important; box-shadow: none !important; }
[data-testid="stExpander"] summary { border-radius: var(--radius) !important; font-weight: 500; color: var(--text) !important; }
[data-testid="stExpander"] summary:hover { color: #FFFFFF !important; background: rgba(255, 255, 255, .03) !important; }

/* ---------- Alertas (apenas tom de fundo, sem bordas coloridas) ---------- */
[data-testid="stAlert"] > div { background: transparent !important; color: #E8ECF5 !important; }
[data-testid="stAlert"]:has([data-testid="stAlertContentInfo"]),
[data-testid="stAlert"][kind="info"] {
    background: linear-gradient(rgba(96, 140, 255, .09), rgba(96, 140, 255, .09)), var(--glass) !important; }
[data-testid="stAlert"]:has([data-testid="stAlertContentSuccess"]),
[data-testid="stAlert"][kind="success"] {
    background: linear-gradient(rgba(52, 211, 153, .08), rgba(52, 211, 153, .08)), var(--glass) !important; }
[data-testid="stAlert"]:has([data-testid="stAlertContentWarning"]),
[data-testid="stAlert"][kind="warning"] {
    background: linear-gradient(rgba(255, 190, 60, .08), rgba(255, 190, 60, .08)), var(--glass) !important; }
[data-testid="stAlert"]:has([data-testid="stAlertContentError"]),
[data-testid="stAlert"][kind="error"] {
    background: linear-gradient(rgba(255, 82, 82, .10), rgba(255, 82, 82, .10)), var(--glass) !important; }

/* ---------- Caixa "Importante" (st.info): visual original preservado ---------- */
[data-testid="stAlert"]:has([data-testid="stAlertContentInfo"]),
[data-testid="stAlert"][kind="info"] {
    background: #0B0C10 !important; border: 1px solid #25262C !important; border-radius: 3px !important;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, .018) !important;
    -webkit-backdrop-filter: none !important; backdrop-filter: none !important; color: #FFFFFF !important;
}
[data-testid="stAlert"]:has([data-testid="stAlertContentInfo"]) [data-testid="stMarkdownContainer"] p,
[data-testid="stAlert"][kind="info"] [data-testid="stMarkdownContainer"] p {
    font-family: "Source Sans Pro", "Source Sans", sans-serif !important; color: #FFFFFF !important;
}

/* ---------- Métricas minimalistas ---------- */
[data-testid="stMetric"] { padding: 1.15rem 1.35rem !important; }
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] p {
    color: var(--text-faint) !important; font-size: .72rem !important; font-weight: 500 !important;
    text-transform: uppercase; letter-spacing: .09em;
}
[data-testid="stMetricValue"], [data-testid="stMetricValue"] div {
    color: #FFFFFF !important; font-weight: 600 !important; letter-spacing: -.025em;
    font-size: clamp(1.5rem, 2.3vw, 2.2rem) !important; line-height: 1.15 !important;
}

/* ---------- Tabelas ---------- */
[data-testid="stDataFrame"], [data-testid="stTable"] {
    background: var(--glass) !important;
    border: 1px solid var(--hairline) !important;
    border-radius: var(--radius) !important;
    overflow: hidden;
    box-shadow: 0 10px 32px rgba(0, 0, 0, .30);
}
[data-testid="stTable"] table { background: transparent !important; color: var(--text) !important; }

/* ---------- Inputs ---------- */
[data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div {
    background: var(--glass-soft) !important;
    border: 1px solid var(--hairline) !important;
    border-radius: var(--radius-sm) !important;
    box-shadow: none !important;
    transition: border-color .18s ease, box-shadow .18s ease;
}
[data-baseweb="base-input"] { background: transparent !important; border: none !important; }
[data-baseweb="input"] input, [data-baseweb="textarea"] textarea, [data-baseweb="select"] input {
    background: transparent !important; color: var(--text) !important; border: none !important;
}
[data-baseweb="input"]:focus-within, [data-baseweb="textarea"]:focus-within,
[data-baseweb="select"]:focus-within > div {
    border-color: rgba(255, 255, 255, .28) !important;
    box-shadow: 0 0 0 3px rgba(120, 140, 255, .12) !important;
}
[data-testid="stNumberInput"] button { background: transparent !important; border: none !important; color: var(--text-soft) !important; }
input, textarea { caret-color: var(--accent) !important; }
[data-baseweb="popover"] > div {
    background: rgba(14, 17, 32, .96) !important; border: 1px solid var(--hairline) !important;
    border-radius: var(--radius-sm) !important; overflow: hidden;
    -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px);
}
[data-baseweb="menu"], [role="listbox"] { background: transparent !important; color: var(--text) !important; }
[data-baseweb="option"]:hover, [role="option"]:hover { background: rgba(255, 255, 255, .06) !important; }

/* ---------- Botões premium ---------- */
.stButton > button, [data-testid="stDownloadButton"] > button,
[data-testid="stFormSubmitButton"] > button {
    background: var(--btn) !important; color: #E6EAF5 !important;
    border: 1px solid var(--hairline) !important; border-radius: var(--radius-sm) !important;
    font-weight: 500 !important; letter-spacing: .01em; padding: .55rem 1.15rem !important;
    box-shadow: none !important;
    transition: background .2s ease, box-shadow .2s ease, border-color .2s ease, color .2s ease !important;
}
.stButton > button p, [data-testid="stDownloadButton"] > button p,
[data-testid="stFormSubmitButton"] > button p { color: inherit !important; font-weight: inherit !important; }
.stButton > button:hover, [data-testid="stDownloadButton"] > button:hover,
[data-testid="stFormSubmitButton"] > button:hover {
    background: var(--btn-hover) !important; color: #FFFFFF !important;
    border-color: var(--hairline-strong) !important;
    box-shadow: 0 6px 22px rgba(90, 110, 200, .20) !important;
}
button[kind="primary"], [data-testid="stBaseButton-primary"] {
    background: rgba(255, 230, 0, .10) !important; color: var(--accent) !important;
    border: 1px solid rgba(255, 230, 0, .20) !important; font-weight: 600 !important;
}
button[kind="primary"]:hover, [data-testid="stBaseButton-primary"]:hover {
    background: var(--accent) !important; color: #0A0C14 !important;
    border-color: var(--accent) !important; box-shadow: 0 0 26px rgba(255, 230, 0, .25) !important;
}
.stButton > button:disabled, [data-testid="stDownloadButton"] > button:disabled {
    opacity: .45 !important; background: var(--glass-soft) !important; color: var(--text-faint) !important;
    border-color: var(--hairline) !important; box-shadow: none !important; cursor: not-allowed !important;
}

/* ---------- Upload ---------- */
[data-testid="stFileUploader"] section, [data-testid="stFileUploaderDropzone"] {
    background: var(--glass) !important; border: 1px dashed rgba(255, 255, 255, .14) !important;
    border-radius: var(--radius) !important;
}
[data-testid="stFileUploader"] button { border-radius: var(--radius-sm) !important; }

/* ---------- Progresso, spinner, links, código ---------- */
[data-testid="stProgress"] > div > div { border-radius: 999px !important; background: rgba(255, 255, 255, .06) !important; }
[data-testid="stProgress"] > div > div > div > div {
    background: linear-gradient(90deg, rgba(255, 230, 0, .55), var(--accent)) !important;
    border-radius: 999px !important; box-shadow: 0 0 12px rgba(255, 230, 0, .25);
}
[data-testid="stSpinner"] > div { border-top-color: var(--accent) !important; }
a { color: #C9D2F0 !important; text-decoration-color: rgba(255, 255, 255, .25) !important; }
a:hover { color: #FFFFFF !important; }
code, pre {
    background: var(--glass-soft) !important; color: #E7E9F0 !important;
    border: 1px solid var(--hairline) !important; border-radius: var(--radius-sm) !important;
}
:focus-visible { outline-color: rgba(255, 255, 255, .35) !important; }

/* ---------- Mapa (iframe do Leaflet) ---------- */
[data-testid="stCustomComponentV1"], [data-testid="stIFrame"] { border-radius: var(--radius) !important; overflow: hidden; }
.stApp iframe {
    border-radius: var(--radius) !important; border: 1px solid var(--hairline) !important;
    background: var(--bg-deep); box-shadow: 0 14px 44px rgba(0, 0, 0, .45);
}

/* ---------- Scrollbar discreta ---------- */
* { scrollbar-width: thin; scrollbar-color: rgba(255, 255, 255, .16) transparent; }
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, .14); border-radius: 999px; }
::-webkit-scrollbar-track { background: transparent; }

/* ---------- Etapas do fluxo ---------- */
.etapas { display: flex; align-items: center; justify-content: center; gap: .15rem; margin: 1.2rem 0 1.5rem; padding: .25rem; overflow-x: auto; }
.etapa { display: flex; align-items: center; gap: .55rem; color: var(--text-faint); font-size: .78rem; font-weight: 500; white-space: nowrap; }
.etapa .num {
    width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center; font-size: .7rem; font-weight: 600;
    border: 1px solid var(--hairline-strong); background: var(--glass-soft);
}
.etapa.feita { color: var(--text-soft); }
.etapa.feita .num { background: rgba(52, 211, 153, .14); border-color: rgba(52, 211, 153, .30); color: #5EE0A8; }
.etapa.ativa { color: #FFFFFF; }
.etapa.ativa .num { background: var(--accent); color: #0A0C14; border-color: var(--accent); box-shadow: 0 0 14px rgba(255, 230, 0, .35); }
.etapa-linha { width: 28px; height: 1px; background: var(--hairline-strong); margin: 0 .5rem; flex: none; }

/* ---------- Pílulas de status ---------- */
.pilulas { display: flex; flex-wrap: wrap; gap: .5rem; margin: .7rem 0 .3rem; }
.pilula {
    display: inline-flex; align-items: center; gap: .5rem; padding: .28rem .8rem; border-radius: 999px;
    font-size: .76rem; font-weight: 500; letter-spacing: .01em; color: var(--text-soft);
    border: 1px solid var(--hairline); background: var(--glass-soft);
}
.pilula .ponto { width: 6px; height: 6px; border-radius: 50%; background: currentColor; box-shadow: 0 0 8px currentColor; }
.pilula-ok { color: #5EE0A8; background: rgba(52, 211, 153, .08); }
.pilula-aviso { color: #FFC857; background: rgba(255, 190, 60, .08); }
.pilula-erro { color: #FF7A7A; background: rgba(255, 82, 82, .09); }

/* ---------- Estado vazio ---------- */
.estado-vazio {
    margin: 1.4rem auto; max-width: 720px; padding: 2.2rem 2rem; text-align: center;
    background: var(--glass); -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px);
    border: 1px solid var(--hairline); border-radius: var(--radius); box-shadow: 0 10px 32px rgba(0, 0, 0, .35);
}
.estado-vazio .icone {
    width: 52px; height: 52px; margin: 0 auto 1rem; border-radius: 16px; display: grid; place-items: center;
    font-size: 1.4rem; color: var(--accent); background: rgba(255, 230, 0, .07); border: 1px solid rgba(255, 230, 0, .16);
}
.estado-vazio .titulo { font-size: 1.15rem; font-weight: 600; letter-spacing: -.01em; margin-bottom: .7rem; color: var(--text); }
.estado-vazio p { color: var(--text-soft); font-size: .92rem; line-height: 1.65; margin: .35rem 0; }

/* ---------- Sidebar: seções e serviços ---------- */
.side-label { font-size: .68rem; font-weight: 600; letter-spacing: .14em; text-transform: uppercase; color: var(--text-faint); margin: 1.1rem 0 .5rem; }
.servico { display: flex; flex-wrap: wrap; justify-content: space-between; gap: .1rem .8rem; font-size: .78rem; padding: .38rem 0; color: var(--text-soft); }
.servico b { font-weight: 500; color: var(--text); text-align: right; white-space: nowrap; margin-left: auto; }
.servico b.on { color: #5EE0A8; }

/* ---------- Spinner em pílula ---------- */
[data-testid="stSpinner"] {
    display: inline-flex; width: auto; padding: .35rem .95rem; border-radius: 999px;
    background: var(--glass-soft); border: 1px solid var(--hairline);
}

/* ---------- Entrada suave (somente opacidade) ---------- */
@keyframes entrada-suave { from { opacity: 0; } to { opacity: 1; } }
[data-testid="stMetric"], [data-testid="stAlert"], [data-testid="stExpander"], .estado-vazio {
    animation: entrada-suave .28s ease-out both;
}
@media (prefers-reduced-motion: reduce) {
    [data-testid="stMetric"], [data-testid="stAlert"], [data-testid="stExpander"], .estado-vazio { animation: none !important; }
}

/* ---------- Responsividade ---------- */
@media (max-width: 768px) {
    [data-testid="stMainBlockContainer"] { padding-left: 1rem; padding-right: 1rem; }
    [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; gap: .75rem !important; }
    [data-testid="stColumn"], [data-testid="column"] { min-width: calc(50% - .75rem) !important; flex: 1 1 calc(50% - .75rem) !important; }
}
@media (max-width: 640px) {
    h1 { font-size: 1.5rem !important; }
    [data-testid="stColumn"], [data-testid="column"] { min-width: 100% !important; flex: 1 1 100% !important; }
    .etapa:not(.ativa) .rotulo { display: none; }
    .etapa-linha { width: 14px; margin: 0 .25rem; }
    .estado-vazio { padding: 1.6rem 1.2rem; }
}
</style>
""", unsafe_allow_html=True)

st.title("🚚 Roteirizador de Entregas")
st.caption("Busca gratuita de endereços, confirmação manual e roteirização por múltiplas rotas candidatas, otimização global e distâncias rodoviárias.")

# ------------------------------------------------------------
# Camada visual auxiliar: não participa da lógica de roteirização.
# Todas as funções abaixo são defensivas; se algo falhar, o app segue sem elas.
# ------------------------------------------------------------
ETAPAS_FLUXO = ("Planilha", "Localização", "Confirmação", "Rota", "Resultado")

ESPEC_LOCALIZADOS = {
    "linha_planilha": ("Linha", "num", {"format": "%d", "width": "small"}),
    "endereco": ("Endereço", "texto", {"width": "large"}),
    "latitude": ("Latitude", "num", {"format": "%.6f"}),
    "longitude": ("Longitude", "num", {"format": "%.6f"}),
    "motor": ("Motor", "texto", {}),
    "resultado_encontrado": ("Resultado encontrado", "texto", {"width": "large"}),
}
ESPEC_ROTA = {
    "Parada": ("Parada", "num", {"format": "%d", "width": "small"}),
    "Endereço": ("Endereço", "texto", {"width": "large"}),
    "Linha original": ("Linha original", "num", {"format": "%d", "width": "small"}),
    "Latitude": ("Latitude", "num", {"format": "%.6f"}),
    "Longitude": ("Longitude", "num", {"format": "%.6f"}),
    "Km desde parada anterior": ("Km", "num", {"format": "%.2f km"}),
    "Minutos desde parada anterior": ("Minutos", "num", {"format": "%.1f min"}),
    "Método de distância/tempo": ("Método", "texto", {}),
    "Google Maps": ("Google Maps", "link", {"display_text": "Abrir"}),
    "Status": ("Status", "texto", {"width": "small"}),
}

_slot_etapas = st.empty()


def _etapa_atual(tem_arquivo):
    """Etapa do fluxo derivada apenas do estado que já existe na sessão."""
    if not tem_arquivo:
        return 0
    geo_estado = st.session_state.get("geocodificados")
    if geo_estado is None:
        return 1
    if st.session_state.get("resultados_rota"):
        return 4
    try:
        pendente = bool(geo_estado["latitude"].isna().any() or geo_estado["longitude"].isna().any())
    except Exception:
        pendente = False
    return 2 if pendente else 3


def _atualizar_etapas(tem_arquivo=True):
    try:
        ativa = _etapa_atual(tem_arquivo)
        partes = []
        for i, nome in enumerate(ETAPAS_FLUXO):
            estado = "feita" if i < ativa else ("ativa" if i == ativa else "")
            marca = "✓" if i < ativa else str(i + 1)
            if i:
                partes.append('<span class="etapa-linha"></span>')
            partes.append(f'<div class="etapa {estado}"><span class="num">{marca}</span><span class="rotulo">{nome}</span></div>')
        _slot_etapas.markdown('<div class="etapas">' + "".join(partes) + "</div>", unsafe_allow_html=True)
    except Exception:
        pass


def _pilulas(itens):
    """Pílulas de status. 'tom' aceito: ok, aviso, erro (qualquer outro vira neutro)."""
    try:
        corpo = "".join(
            f'<span class="pilula pilula-{tom if tom in ("ok", "aviso", "erro") else "neutro"}">'
            f'<span class="ponto"></span>{html.escape(str(texto))}</span>'
            for texto, tom in itens
        )
        st.markdown(f'<div class="pilulas">{corpo}</div>', unsafe_allow_html=True)
    except Exception:
        pass


def _painel_servicos():
    try:
        cache_ativo = CACHE_DB.exists()
        itens = [
            ("Cache local", "ativo" if cache_ativo else "inativo", cache_ativo),
            ("Geocodificação", "Nominatim · Photon" + (" · ORS" if ORS_API_KEY else ""), False),
            ("Roteamento", "OSRM público", False),
        ]
        corpo = "".join(
            f'<div class="servico"><span>{nome}</span><b class="{"on" if on else ""}">{valor}</b></div>'
            for nome, valor, on in itens
        )
        st.markdown('<div class="side-label">Serviços</div>' + corpo, unsafe_allow_html=True)
    except Exception:
        pass


def _config_colunas(df, especificacao):
    """Apenas apresentação das tabelas. Qualquer incompatibilidade de versão devolve None."""
    try:
        cfg = {}
        for coluna, (rotulo, tipo, opcoes) in especificacao.items():
            if coluna not in df.columns:
                continue
            if tipo == "num":
                cfg[coluna] = st.column_config.NumberColumn(rotulo, **opcoes)
            elif tipo == "link":
                cfg[coluna] = st.column_config.LinkColumn(rotulo, **opcoes)
            else:
                cfg[coluna] = st.column_config.TextColumn(rotulo, **opcoes)
        return cfg or None
    except Exception:
        return None


def _mostrar_tabela(df, especificacao):
    cfg = _config_colunas(df, especificacao)
    if cfg:
        try:
            st.dataframe(df, use_container_width=True, hide_index=True, column_config=cfg)
            return
        except Exception:
            pass
    st.dataframe(df, use_container_width=True, hide_index=True)


_atualizar_etapas(False)

st.info(
    "Importante: os geocodificadores e o OSRM usados aqui são serviços públicos gratuitos, "
    "sem garantia de disponibilidade ou capacidade. O cache SQLite reduz consultas repetidas. "
    "User-Agent configurado com contato para identificação da aplicação. "
    "O aplicativo não descarta entregas: endereços sem coordenadas ficam pendentes até serem confirmados."
)

def normalizar(texto):
    texto = str(texto or "").strip()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.upper().split())

# ---------- Limpeza e variações de endereço (valem para Nominatim, Photon e ORS) ----------
_ABREVIACOES = {
    "AV": "AVENIDA", "TV": "TRAVESSA", "TRAV": "TRAVESSA", "PROF": "PROFESSOR", "PROFA": "PROFESSORA",
    "DR": "DOUTOR", "DRA": "DOUTORA", "ENG": "ENGENHEIRO", "STA": "SANTA", "STO": "SANTO",
    "CEL": "CORONEL", "PCA": "PRACA", "PRC": "PRACA", "CJ": "CONJUNTO", "JD": "JARDIM", "VL": "VILA",
}
_PARTICULAS = {"DE", "DA", "DO", "DAS", "DOS"}
_GRAFIAS = (("LUIS", "LUIZ"), ("EUSEBIO", "EUZEBIO"), ("ELISEU", "ELIZEU"), ("ISIDORO", "IZIDORO"))
MAX_CONSULTAS_AUTO = 6


def _limpar_endereco(texto):
    """Tira o código IBGE ('2504009|'), o 'N'/'Nº' antes do número e expande abreviações comuns."""
    texto = re.sub(r"\b\d{6,7}\s*\|\s*", "", str(texto or ""))
    texto = re.sub(r"(?i)\b(?:N[º°o]?|NUM|NUMERO|NÚMERO)\.?\s*(?=\d)", "", texto)
    texto = re.sub(r"(?i)^\s*R\.?\s+(?=\S)", "RUA ", texto)
    texto = re.sub(
        r"(?i)\b(" + "|".join(_ABREVIACOES) + r")\b\.?",
        lambda m: _ABREVIACOES[m.group(1).upper()], texto,
    )
    return " ".join(texto.split()).strip(" ,")


def _variantes_endereco(texto):
    """Devolve o endereço limpo e variações: grafia (Luis/Luiz), sem 'de/da/do' e sem o bairro."""
    texto = " ".join(str(texto or "").split())
    if not texto:
        return []
    norm = normalizar(texto)

    def troca_grafia(t):
        for a, b in _GRAFIAS:
            if re.search(rf"\b{a}\b", t):
                return re.sub(rf"\b{a}\b", b, t)
            if re.search(rf"\b{b}\b", t):
                return re.sub(rf"\b{b}\b", a, t)
        return t

    def sem_particulas(t):
        partes = t.split(",")
        partes[0] = " ".join(w for w in partes[0].split() if w not in _PARTICULAS)
        return ",".join(partes)

    variantes = [texto]
    graf = troca_grafia(norm)
    variantes.append(graf)
    variantes.append(sem_particulas(norm))
    variantes.append(sem_particulas(graf))
    partes = [x.strip() for x in texto.split(",") if x.strip()]
    # rua, [número], bairro, município, UF, país -> tenta sem o bairro (nunca remove o número).
    if len(partes) >= 5 and not re.search(r"\d", partes[1]):
        variantes.append(", ".join([partes[0]] + partes[2:]))
    elif len(partes) >= 6 and re.search(r"\d", partes[1]) and not re.search(r"\d", partes[2]):
        variantes.append(", ".join(partes[:2] + partes[3:]))
    saida, vistos = [], set()
    for v in variantes:
        v = " ".join(v.split()).strip(" ,")
        chave = normalizar(v)
        if v and chave not in vistos:
            vistos.add(chave)
            saida.append(v)
    return saida


def montar_consultas(endereco, cidade):
    endereco = str(endereco or "").strip()
    cidade = str(cidade or "").strip()
    if not endereco:
        return []
    cidade_limpa = _limpar_consulta_ors(cidade) or "Campina Grande, PB, Brasil"
    nome_cidade, _ = _cidade_uf_alvo(cidade)
    limpas = []
    for variante in _variantes_endereco(_limpar_endereco(endereco)):
        if nome_cidade and nome_cidade in normalizar(variante):
            limpas.append(variante)
        else:
            limpas.append(f"{variante}, {cidade_limpa}")
    # Consulta original (como era antes) fica como última tentativa, por compatibilidade.
    consultas = limpas[:MAX_CONSULTAS_AUTO - 1] + [endereco]
    saida, vistos = [], set()
    for q in consultas:
        q = " ".join(q.split()).strip(" ,")
        chave = normalizar(q)
        if q and chave not in vistos:
            vistos.add(chave)
            saida.append(q)
    return saida


def geocodificar_nominatim(consulta):
    chave = _chave_geocoder("nominatim", consulta)
    em_cache = _cache_ler("respostas_geocoder", chave)
    if isinstance(em_cache, dict) and em_cache.get("_cache_envelope") is True:
        return em_cache.get("resultado")
    if em_cache is not None:  # Compatibilidade com entradas antigas do cache.
        return em_cache
    resp = _get_nominatim(
        NOMINATIM_URL,
        params={"q": consulta, "format": "jsonv2", "limit": 1, "countrycodes": "br"},
        timeout=(5, 20),
    )
    resp.raise_for_status()
    dados = resp.json()
    resultado = None
    if dados:
        resultado = {"lat": float(dados[0]["lat"]), "lon": float(dados[0]["lon"]),
                     "nome": dados[0].get("display_name", consulta), "motor": "Nominatim"}
    _cache_gravar(
        "respostas_geocoder", chave,
        {"_cache_envelope": True, "resultado": resultado}, "Nominatim",
    )
    return resultado


def geocodificar_photon(consulta):
    chave = _chave_geocoder("photon", consulta)
    em_cache = _cache_ler("respostas_geocoder", chave)
    if isinstance(em_cache, dict) and em_cache.get("_cache_envelope") is True:
        return em_cache.get("resultado")
    if em_cache is not None:  # Compatibilidade com entradas antigas do cache.
        return em_cache
    resp = HTTP.get(PHOTON_URL, params={"q": consulta, "limit": 1}, timeout=(5, 20))
    resp.raise_for_status()
    features = resp.json().get("features", [])
    resultado = None
    if features:
        feature = features[0]
        coords = feature.get("geometry", {}).get("coordinates", [])
        if len(coords) >= 2:
            props = feature.get("properties", {})
            nome = ", ".join(str(props[k]) for k in ("name", "street", "housenumber", "city", "state") if props.get(k))
            resultado = {"lat": float(coords[1]), "lon": float(coords[0]), "nome": nome or consulta, "motor": "Photon"}
    _cache_gravar(
        "respostas_geocoder", chave,
        {"_cache_envelope": True, "resultado": resultado}, "Photon",
    )
    return resultado


def _limpar_consulta_ors(texto):
    """Remove códigos IBGE tipo '2504009|' que confundem o ORS."""
    return " ".join(re.sub(r"\b\d{6,7}\s*\|\s*", "", str(texto or "")).split())


def _cidade_uf_alvo(cidade):
    texto = _limpar_consulta_ors(cidade)
    partes = [p.strip() for p in texto.split(",") if p.strip()]
    nome = normalizar(partes[0]) if partes else ""
    uf = next((normalizar(p) for p in partes[1:] if len(p) == 2), "")
    return nome, uf


_TIPOS_VIA = {"RUA", "AVENIDA", "TRAVESSA", "ALAMEDA", "PRACA", "RODOVIA", "ESTRADA", "BECO",
              "VILA", "JARDIM", "CONJUNTO", "LOTEAMENTO", "LOT", "VIELA", "LADEIRA", "PARQUE"}
_TITULOS = {"PROFESSOR", "PROFESSORA", "DOUTOR", "DOUTORA", "ENGENHEIRO", "CORONEL", "GENERAL", "MAJOR",
            "CAPITAO", "TENENTE", "SARGENTO", "PADRE", "FREI", "PASTOR", "VEREADOR", "VEREADORA",
            "DEPUTADO", "DEPUTADA", "PREFEITO", "GOVERNADOR", "PRESIDENTE", "SENADOR", "DOM", "DONA",
            "MONSENHOR", "BISPO", "COMENDADOR", "DESEMBARGADOR", "MINISTRO", "JUIZ", "MESTRE"}


def _fonetica(token):
    """Aproxima grafias parecidas: Luis/Luiz, Nely/Neli, Souza/Sousa, Phelipe/Felipe."""
    t = normalizar(token)
    for a, b in (("PH", "F"), ("Y", "I"), ("W", "V"), ("Z", "S"), ("K", "C"), ("H", "")):
        t = t.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", t)


def _tokens_nome(texto, filtrar=True):
    saida = []
    for t in re.split(r"[^A-Z0-9]+", normalizar(texto)):
        if not t or any(c.isdigit() for c in t) or len(t) < 2:
            continue
        if filtrar and (t in _PARTICULAS or t in _TIPOS_VIA or t in _TITULOS):
            continue
        saida.append(_fonetica(t))
    return saida


def _rua_confere(rua_ref, nome_resultado, limite=0.8):
    """True se o nome da rua pedido aparece (com tolerância de grafia) no resultado devolvido."""
    ref = _tokens_nome(rua_ref)
    if not ref:
        return True  # ex.: "Rua A", "Rua 10": não há nome para comparar
    candidatos = _tokens_nome(nome_resultado, filtrar=False)
    achados = 0
    for t in ref:
        if any(t == c or difflib.SequenceMatcher(None, t, c).ratio() >= 0.85 for c in candidatos):
            achados += 1
    return achados / len(ref) >= limite


def _ors_resultado_confiavel(props, cidade, rua_ref=""):
    """Aceita só endereço/rua dentro da cidade e do estado pedidos (sem 'palpite' de outra cidade)."""
    if props.get("layer") not in ORS_CAMADAS_VALIDAS:
        return False
    if props.get("match_type") == "fallback":
        return False
    nome, uf = _cidade_uf_alvo(cidade)
    regiao = normalizar(props.get("region_a") or "")
    if uf and regiao and regiao != uf:
        return False
    if nome:
        locais = [normalizar(props.get(k) or "") for k in ("locality", "localadmin", "county")]
        if not any(nome in local for local in locais if local):
            return False
    if rua_ref and not _rua_confere(rua_ref, props.get("label") or props.get("name") or ""):
        return False
    return True


def geocodificar_ors(consulta, cidade="", rua_ref=""):
    """OpenRouteService (Pelias): só é usado se houver chave configurada."""
    if not ORS_API_KEY:
        return None
    chave = _chave_geocoder("ors3", f"{consulta}|{cidade}|{rua_ref}")
    em_cache = _cache_ler("respostas_geocoder", chave)
    if isinstance(em_cache, dict) and em_cache.get("_cache_envelope") is True:
        return em_cache.get("resultado")
    resp = HTTP.get(
        ORS_GEOCODE_URL,
        params={
            "text": _limpar_consulta_ors(consulta), "size": 10, "boundary.country": "BR",
            "focus.point.lat": ORS_FOCO_LAT, "focus.point.lon": ORS_FOCO_LON,
        },
        headers={"Authorization": ORS_API_KEY},
        timeout=(5, 20),
    )
    resp.raise_for_status()  # 429 (cota esgotada) vira erro e NÃO é gravado no cache
    resultado = None
    for feature in resp.json().get("features", []):
        props = feature.get("properties", {})
        coords = feature.get("geometry", {}).get("coordinates", [])
        if len(coords) >= 2 and _ors_resultado_confiavel(props, cidade, rua_ref):
            resultado = {"lat": float(coords[1]), "lon": float(coords[0]),
                         "nome": props.get("label") or consulta, "motor": "OpenRouteService"}
            break
    _cache_gravar(
        "respostas_geocoder", chave,
        {"_cache_envelope": True, "resultado": resultado}, "OpenRouteService v3",
    )
    return resultado


def geocodificar(endereco, cidade):
    # Coordenadas confirmadas pelo usuário têm prioridade sobre os serviços públicos.
    confirmado = buscar_endereco_confirmado(endereco, cidade)
    if confirmado:
        confirmado["consulta"] = f"{endereco}, {cidade}"
        return confirmado, "OK (coordenadas confirmadas anteriormente)"
    chave = _chave_geocoder("endereco2", f"{endereco}|{cidade}")
    cache = _cache_ler("geocodificacao", chave)
    if cache is not None:
        return cache.get("resultado"), cache.get("situacao", "OK (cache SQLite)")
    consultas = montar_consultas(endereco, cidade)
    erros_conexao = []
    # Nome da rua pedido: resultados com rua diferente são descartados (ficam pendentes p/ confirmação).
    rua_ref = _limpar_endereco(endereco).split(",")[0]
    descartados = []
    for consulta in consultas:
        for motor, funcao in (("Nominatim", geocodificar_nominatim), ("Photon", geocodificar_photon)):
            try:
                resultado = funcao(consulta)
                if resultado and not _rua_confere(rua_ref, resultado.get("nome", "")):
                    descartados.append(f"{motor}: {resultado.get('nome', '')}")
                    resultado = None
                if resultado:
                    resultado["consulta"] = consulta
                    _cache_gravar("geocodificacao", chave, {"resultado": resultado, "situacao": "OK"}, motor)
                    return resultado, "OK"
            except requests.RequestException as exc:
                erros_conexao.append(f"{motor}: {exc}")
            except (ValueError, KeyError, TypeError) as exc:
                erros_conexao.append(f"{motor}: {exc}")
    # OpenRouteService só entra depois que Nominatim e Photon falharam (poupa a cota diária).
    if ORS_API_KEY:
        for consulta in consultas[:2]:
            try:
                resultado = geocodificar_ors(consulta, cidade, rua_ref)
                if resultado:
                    resultado["consulta"] = consulta
                    _cache_gravar("geocodificacao", chave, {"resultado": resultado, "situacao": "OK"}, "OpenRouteService v3")
                    return resultado, "OK"
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                erros_conexao.append(f"OpenRouteService: {exc}")
    situacao = "Falha de conexão/serviço" if erros_conexao and len(erros_conexao) >= len(consultas) else "Não encontrado automaticamente"
    if descartados and situacao == "Não encontrado automaticamente":
        situacao += " (resultado descartado: rua diferente)"
    # Cache negativo curto evita tempestade de requisições em repetição imediata.
    return None, situacao

def _consultas_ampliadas(endereco, cidade):
    """Gera consultas mais flexíveis e evita repetir a cidade no texto."""
    import re

    endereco = _limpar_endereco(" ".join(str(endereco or "").strip().split()))
    cidade = _limpar_consulta_ors(" ".join(str(cidade or "").strip().split())) or "Campina Grande, PB, Brasil"
    if not endereco:
        return []

    # Remove a localidade do final caso ela já tenha sido incluída na célula.
    base_endereco = endereco
    base_endereco = re.sub(
        r"(?i)(?:,?\s*)campina\s+grande(?:\s*,\s*(?:pb|para[ií]ba))?(?:\s*,\s*brasil)?\s*$",
        "", base_endereco,
    ).strip(" ,-")
    base_endereco = re.sub(r"(?i)(?:,?\s*)(?:para[ií]ba|pb)(?:\s*,\s*brasil)?\s*$", "", base_endereco).strip(" ,-")
    base_endereco = re.sub(r"(?i)(?:,?\s*)brasil\s*$", "", base_endereco).strip(" ,-")
    if not base_endereco:
        base_endereco = endereco

    base = _variantes_endereco(base_endereco)[:3]
    # Tenta remover o número do imóvel, preservando o nome da rua.
    sem_numero = re.sub(r"[,\s]+\d+[A-Za-z]?\s*$", "", base_endereco).strip(" ,-")
    if sem_numero and normalizar(sem_numero) != normalizar(base_endereco):
        base.append(sem_numero)

    # Tenta também sem o prefixo do tipo de via.
    sem_prefixo = re.sub(
        r"^(RUA|R|AVENIDA|AV|AV\.|TRAVESSA|TV|ALAMEDA|RODOVIA|ESTRADA)\.?\s+",
        "", sem_numero or base_endereco, flags=re.IGNORECASE
    ).strip()
    if sem_prefixo and normalizar(sem_prefixo) not in {normalizar(x) for x in base}:
        base.append(sem_prefixo)

    consultas = []
    vistos = set()
    localidade = cidade
    for item in base:
        # Consulta local principal e uma variante com nome de estado/country.
        variantes = [f"{item}, {localidade}"]
        if "campina grande" not in normalizar(localidade):
            variantes.append(f"{item}, Campina Grande, PB, Brasil")
        variantes.append(f"{item}, Paraíba, Brasil")
        for q in variantes:
            q = " ".join(q.split()).strip(" ,")
            chave = normalizar(q)
            if q and chave not in vistos:
                vistos.add(chave)
                consultas.append(q)
    return consultas[:10]


def buscar_enderecos_semelhantes(endereco, cidade):
    """Busca alternativas por várias grafias; nunca confirma automaticamente."""
    consultas = _consultas_ampliadas(endereco, cidade)
    candidatos = {}
    erros = []
    consultas_com_resultados = 0
    requisicoes = 0

    for consulta in consultas:
        # Nominatim: consulta o cache antes de acessar a API pública.
        try:
            chave_nominatim = _chave_geocoder("nominatim-candidatos10", consulta)
            dados = _cache_ler("respostas_geocoder", chave_nominatim, ttl_segundos=86400)
            if dados is None:
                resp = _get_nominatim(
                    NOMINATIM_URL,
                    params={
                        "q": consulta,
                        "format": "jsonv2",
                        "limit": 10,
                        "countrycodes": "br",
                        "addressdetails": 1,
                        "accept-language": "pt-BR,pt",
                    },
                    headers={"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt;q=0.9"},
                    timeout=(5, 20),
                )
                requisicoes += 1
                resp.raise_for_status()
                dados = resp.json()
                _cache_gravar("respostas_geocoder", chave_nominatim, dados, "Nominatim candidatos")
            if dados:
                consultas_com_resultados += 1
            for item in dados:
                nome = str(item.get("display_name", "")).strip()
                if not nome:
                    continue
                lat, lon = float(item["lat"]), float(item["lon"])
                chave = (round(lat, 6), round(lon, 6))
                if chave not in candidatos:
                    candidatos[chave] = {
                        "nome": nome, "lat": lat, "lon": lon,
                        "motor": "Nominatim", "consulta": consulta,
                    }
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            erros.append(f"Nominatim [{consulta}]: {type(exc).__name__}: {exc}")

        try:
            chave_photon = _chave_geocoder("photon-candidatos10", consulta)
            features = _cache_ler("respostas_geocoder", chave_photon, ttl_segundos=86400)
            if features is None:
                resp = HTTP.get(
                    PHOTON_URL,
                    params={"q": consulta, "limit": 10},
                    headers={"User-Agent": USER_AGENT},
                    timeout=(5, 20),
                )
                requisicoes += 1
                resp.raise_for_status()
                features = resp.json().get("features", [])
                _cache_gravar("respostas_geocoder", chave_photon, features, "Photon candidatos")
            if features:
                consultas_com_resultados += 1
            for item in features:
                coords = item.get("geometry", {}).get("coordinates", [])
                props = item.get("properties", {})
                if len(coords) < 2:
                    continue
                partes = [
                    str(props[k]) for k in
                    ("name", "housenumber", "street", "district", "city", "state")
                    if props.get(k)
                ]
                nome = ", ".join(dict.fromkeys(partes)).strip() or consulta
                lat, lon = float(coords[1]), float(coords[0])
                chave = (round(lat, 6), round(lon, 6))
                if chave not in candidatos:
                    candidatos[chave] = {
                        "nome": nome, "lat": lat, "lon": lon,
                        "motor": "Photon", "consulta": consulta,
                    }
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            erros.append(f"Photon [{consulta}]: {type(exc).__name__}: {exc}")

        # OpenRouteService: opcional, limitado às 4 primeiras consultas para poupar a cota diária.
        if ORS_API_KEY and consulta in consultas[:4]:
            try:
                chave_ors = _chave_geocoder("ors-candidatos10-v2", consulta)
                features = _cache_ler("respostas_geocoder", chave_ors, ttl_segundos=86400)
                if features is None:
                    resp = HTTP.get(
                        ORS_GEOCODE_URL,
                        params={
                            "text": _limpar_consulta_ors(consulta), "size": 10, "boundary.country": "BR",
                            "focus.point.lat": ORS_FOCO_LAT, "focus.point.lon": ORS_FOCO_LON,
                        },
                        headers={"Authorization": ORS_API_KEY},
                        timeout=(5, 20),
                    )
                    requisicoes += 1
                    resp.raise_for_status()
                    features = resp.json().get("features", [])
                    _cache_gravar("respostas_geocoder", chave_ors, features, "OpenRouteService candidatos")
                if features:
                    consultas_com_resultados += 1
                for item in features:
                    coords = item.get("geometry", {}).get("coordinates", [])
                    props = item.get("properties", {})
                    if len(coords) < 2:
                        continue
                    _, uf_alvo = _cidade_uf_alvo(cidade)
                    regiao = normalizar(props.get("region_a") or "")
                    if uf_alvo and regiao and regiao != uf_alvo:
                        continue  # ignora sugestões de outro estado
                    nome = str(props.get("label") or "").strip() or consulta
                    lat, lon = float(coords[1]), float(coords[0])
                    chave = (round(lat, 6), round(lon, 6))
                    if chave not in candidatos:
                        candidatos[chave] = {
                            "nome": nome, "lat": lat, "lon": lon,
                            "motor": "OpenRouteService", "consulta": consulta,
                        }
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                erros.append(f"OpenRouteService [{consulta}]: {type(exc).__name__}: {exc}")
        time.sleep(0.25)

    alvo = normalizar(f"{endereco} {cidade}")
    termos_alvo = set(alvo.split())

    def pontuacao(item):
        nome = normalizar(item["nome"])
        sequencia = difflib.SequenceMatcher(None, alvo, nome).ratio()
        termos_nome = set(nome.split())
        sobreposicao = len(termos_alvo & termos_nome) / max(1, len(termos_alvo))
        # A pontuação lexical ajuda a priorizar rua/nome mesmo com número ausente.
        return max(sequencia, sobreposicao * 0.85)

    ordenados = sorted(candidatos.values(), key=pontuacao, reverse=True)
    diagnostico = {
        "consultas": consultas,
        "requisicoes": requisicoes,
        "consultas_com_resultados": consultas_com_resultados,
        "candidatos_brutos": len(candidatos),
        "erros": erros,
    }
    return ordenados[:15], diagnostico


def abrir_google_maps(endereco, cidade):
    texto = f"{endereco}, {cidade}".strip(", ")
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(texto)

def ler_planilha(arquivo):
    nome = arquivo.name.lower()
    if nome.endswith(".csv"):
        try:
            df = pd.read_csv(arquivo, sep=None, engine="python", dtype=str, encoding="utf-8-sig")
        except UnicodeDecodeError:
            arquivo.seek(0)
            df = pd.read_csv(arquivo, sep=None, engine="python", dtype=str, encoding="latin-1")
    else:
        df = pd.read_excel(arquivo, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    return df.fillna("")

def matriz_haversine(coordenadas, fator=FATOR_RODOVIARIO_HAVERSINE):
    """Matriz aproximada de contingência; não equivale a distância rodoviária real."""
    n = len(coordenadas)
    return [[0.0 if i == j else distancia_haversine(coordenadas[i], coordenadas[j]) * fator
             for j in range(n)] for i in range(n)]


def osrm_matriz(coordenadas):
    if len(coordenadas) < 2:
        return [[0.0 for _ in coordenadas] for _ in coordenadas]
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coordenadas)
    resp = HTTP.get(OSRM_TABLE_URL + coords, params={"annotations": "distance"},
                    timeout=(5, 45), headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("distances"):
        raise RuntimeError(f"OSRM não calculou a matriz: {data.get('message', data.get('code', 'resposta inválida'))}")
    matriz = data["distances"]
    n = len(coordenadas)
    if len(matriz) != n or any(not isinstance(linha, list) or len(linha) != n for linha in matriz):
        raise RuntimeError("OSRM retornou uma matriz incompleta ou com dimensões inválidas.")
    # Completa apenas pares sem caminho com estimativa explícita Haversine.
    # Os índices substituídos são registrados para a interface e as métricas.
    fallbacks = []
    for i in range(n):
        for j in range(n):
            valor = matriz[i][j]
            if i == j:
                matriz[i][j] = 0.0
            elif valor is None or not isinstance(valor, (int, float)) or not math.isfinite(float(valor)):
                matriz[i][j] = distancia_haversine(coordenadas[i], coordenadas[j]) * FATOR_RODOVIARIO_HAVERSINE
                fallbacks.append((i, j))
    return matriz, fallbacks

def distancia_haversine(a, b):
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2-lat1, lon2-lon1
    h = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(h)))

def osrm_distancias_de_um_para_varios(coordenadas, origem_idx, destinos_idx):
    """Distâncias rodoviárias da parada atual para as candidatas restantes."""
    if not destinos_idx:
        return []
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coordenadas)
    resp = HTTP.get(
        OSRM_TABLE_URL + coords,
        params={
            "annotations": "distance",
            "sources": str(origem_idx),
            "destinations": ";".join(str(i) for i in destinos_idx),
        },
        timeout=60,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("distances"):
        raise RuntimeError(data.get("message", data.get("code", "OSRM não retornou distâncias")))
    return data["distances"][0]

def custo_rota(ordem, matriz):
    total = 0.0
    for a, b in zip(ordem, ordem[1:]):
        try:
            d = matriz[a][b]
        except (IndexError, TypeError):
            return math.inf
        if d is None or not isinstance(d, (int, float)) or not math.isfinite(float(d)):
            return math.inf
        total += float(d)
    return total


def _dist(matriz, a, b):
    valor = matriz[a][b]
    if valor is None or not isinstance(valor, (int, float)) or not math.isfinite(float(valor)):
        return math.inf
    return float(valor)


def _delta_2opt(rota, matriz, i, j):
    # Em matriz assimétrica, a inversão muda a direção de todas as arestas internas.
    primeiro = i - 1
    ultimo = min(j, len(rota) - 2)
    custo_antigo = sum(_dist(matriz, rota[k], rota[k + 1]) for k in range(primeiro, ultimo + 1))
    # Reavalia somente a janela afetada, sem somar o custo da rota inteira.
    trecho = list(reversed(rota[i:j + 1]))
    janela = rota[i - 1:i] + trecho + rota[j + 1:j + 2]
    custo_novo = sum(_dist(matriz, janela[k - primeiro], janela[k - primeiro + 1])
                     for k in range(primeiro, ultimo + 1))
    return custo_novo - custo_antigo


def _delta_swap(rota, matriz, i, j):
    afetadas = {k for k in (i - 1, i, j - 1, j) if 0 <= k < len(rota) - 1}
    delta = 0.0
    for k in afetadas:
        a, b = rota[k], rota[k + 1]
        na = rota[j] if a == rota[i] else rota[i] if a == rota[j] else a
        nb = rota[j] if b == rota[i] else rota[i] if b == rota[j] else b
        delta += _dist(matriz, na, nb) - _dist(matriz, a, b)
    return delta


def _delta_relocate(rota, matriz, i, j):
    # j é a posição de inserção na lista após remover o elemento da posição i.
    x = rota[i]
    sem = rota[:i] + rota[i + 1:]
    if j < 1 or j > len(sem):
        return math.inf
    a = rota[i - 1]
    if i < len(rota) - 1:
        b = rota[i + 1]
        delta = -_dist(matriz, a, x) - _dist(matriz, x, b) + _dist(matriz, a, b)
    else:
        delta = -_dist(matriz, a, x)
    c = sem[j - 1]
    if j < len(sem):
        d = sem[j]
        delta += -_dist(matriz, c, d) + _dist(matriz, c, x) + _dist(matriz, x, d)
    else:
        delta += _dist(matriz, c, x)
    return delta


def melhorar_rota_local(ordem, matriz, max_passadas=20, max_avaliacoes=250000):
    """Busca local com deltas incrementais para 2-opt, relocate e swap.

    2-opt considera a assimetria da matriz rodoviária; relocate e swap avaliam
    apenas as arestas afetadas, evitando recalcular o custo completo por candidato.
    """
    if matriz is None or len(ordem) < 3:
        return ordem[:]
    melhor = ordem[:]
    custo_melhor = custo_rota(melhor, matriz)
    if not math.isfinite(custo_melhor):
        return melhor
    avaliacoes = 0
    n = len(melhor)
    for _ in range(max_passadas):
        melhor_delta = -1.0
        melhor_movimento = None
        # 2-opt: O(tamanho do segmento), necessário para matriz assimétrica.
        for i in range(1, n - 1):
            for j in range(i + 1, n):
                avaliacoes += 1
                delta = _delta_2opt(melhor, matriz, i, j)
                if delta < melhor_delta:
                    melhor_delta, melhor_movimento = delta, ("2opt", i, j)
                if avaliacoes >= max_avaliacoes:
                    break
            if avaliacoes >= max_avaliacoes:
                break
        if avaliacoes < max_avaliacoes:
            for i in range(1, n):
                for j in range(1, n):
                    if i == j:
                        continue
                    avaliacoes += 1
                    delta = _delta_relocate(melhor, matriz, i, j)
                    if delta < melhor_delta:
                        melhor_delta, melhor_movimento = delta, ("relocate", i, j)
                    if avaliacoes >= max_avaliacoes:
                        break
                if avaliacoes >= max_avaliacoes:
                    break
        if avaliacoes < max_avaliacoes:
            for i in range(1, n - 1):
                for j in range(i + 1, n):
                    avaliacoes += 1
                    delta = _delta_swap(melhor, matriz, i, j)
                    if delta < melhor_delta:
                        melhor_delta, melhor_movimento = delta, ("swap", i, j)
                    if avaliacoes >= max_avaliacoes:
                        break
                if avaliacoes >= max_avaliacoes:
                    break
        if melhor_movimento is None or melhor_delta >= -1.0:
            break
        tipo, i, j = melhor_movimento
        if tipo == "2opt":
            melhor[i:j + 1] = reversed(melhor[i:j + 1])
        elif tipo == "swap":
            melhor[i], melhor[j] = melhor[j], melhor[i]
        else:
            item = melhor.pop(i)
            melhor.insert(j, item)
        custo_melhor += melhor_delta
        # Proteção contra deriva numérica ou delta incorreto: recalibra por passada.
        custo_real = custo_rota(melhor, matriz)
        if not math.isfinite(custo_real):
            return ordem[:]
        custo_melhor = custo_real
        if avaliacoes >= max_avaliacoes:
            break
    return melhor


def construir_vizinho_mais_proximo(coordenadas, matriz, primeiro=None):
    ordem = [0]
    restantes = set(range(1, len(coordenadas)))
    atual = 0
    if primeiro is not None and primeiro in restantes:
        ordem.append(primeiro)
        restantes.remove(primeiro)
        atual = primeiro
    while restantes:
        opcoes = [(_dist(matriz, atual, i), i) for i in restantes]
        opcoes = [(d, i) for d, i in opcoes if math.isfinite(d)]
        if not opcoes:
            # A matriz de contingência normalmente impede este caso; falha explícita se persistir.
            raise RuntimeError("Não existe distância válida entre a parada atual e as entregas restantes.")
        _, proximo = min(opcoes)
        ordem.append(proximo)
        restantes.remove(proximo)
        atual = proximo
    return ordem


def construir_rota_randomizada(coordenadas, matriz, rng, fator_escolha=3):
    ordem = [0]
    restantes = set(range(1, len(coordenadas)))
    atual = 0
    while restantes:
        validos = sorted((_dist(matriz, atual, i), i) for i in restantes)
        validos = [(d, i) for d, i in validos if math.isfinite(d)]
        if not validos:
            raise RuntimeError("Não existe caminho válido para as entregas restantes.")
        faixa = validos[:min(max(1, fator_escolha), len(validos))]
        pesos = [1.0 / ((pos + 1) ** 1.5) for pos in range(len(faixa))]
        _, proximo = rng.choices(faixa, weights=pesos, k=1)[0]
        ordem.append(proximo)
        restantes.remove(proximo)
        atual = proximo
    return ordem

def agrupar_entregas_geograficamente(coordenadas, tamanho_grupo=4):
    """Agrupa entregas próximas com k-means simples, sem dependências extras."""
    pontos = list(range(1, len(coordenadas)))
    n = len(pontos)
    if n <= tamanho_grupo:
        return [pontos] if pontos else []
    k = min(n, max(2, math.ceil(n / tamanho_grupo)))

    centros = [coordenadas[pontos[0]]]
    while len(centros) < k:
        restantes = [i for i in pontos if coordenadas[i] not in centros]
        if not restantes:
            break
        prox = max(restantes, key=lambda i: min(distancia_haversine(coordenadas[i], c) for c in centros))
        centros.append(coordenadas[prox])

    for _ in range(30):
        grupos = [[] for _ in centros]
        for i in pontos:
            j = min(range(len(centros)), key=lambda c: distancia_haversine(coordenadas[i], centros[c]))
            grupos[j].append(i)
        novos = []
        for j, membros in enumerate(grupos):
            if membros:
                novos.append((sum(coordenadas[i][0] for i in membros)/len(membros),
                              sum(coordenadas[i][1] for i in membros)/len(membros)))
            else:
                novos.append(centros[j])
        if novos == centros:
            break
        centros = novos

    grupos = [[] for _ in centros]
    for i in pontos:
        j = min(range(len(centros)), key=lambda c: distancia_haversine(coordenadas[i], centros[c]))
        grupos[j].append(i)
    return [g for g in grupos if g]


def otimizar_ordem_dentro_grupo(grupo, matriz, coordenadas, inicio):
    """Ordena as paradas de um grupo a partir da parada anterior."""
    restantes = set(grupo)
    ordem = []
    atual = inicio
    while restantes:
        destinos = sorted(restantes)
        if matriz is not None:
            opcoes = [(float(matriz[atual][i]), i) for i in destinos
                      if matriz[atual][i] is not None
                      and isinstance(matriz[atual][i], (int, float))
                      and math.isfinite(matriz[atual][i])]
        else:
            distancias = osrm_distancias_de_um_para_varios(coordenadas, atual, destinos)
            opcoes = [(float(d), i) for i, d in zip(destinos, distancias)
                      if d is not None and isinstance(d, (int, float)) and math.isfinite(d)]
        if not opcoes:
            raise RuntimeError("O serviço rodoviário não encontrou caminho válido para uma entrega do grupo.")
        _, proximo = min(opcoes)
        ordem.append(proximo)
        restantes.remove(proximo)
        atual = proximo

    if matriz is not None and len(ordem) >= 3:
        # Otimiza o bloco localmente, incluindo o custo de entrada a partir da parada anterior.
        indices = [inicio] + ordem
        submatriz = [[matriz[a][b] for b in indices] for a in indices]
        local = melhorar_rota_local(list(range(len(indices))), submatriz, max_passadas=20)
        ordem = [indices[j] for j in local[1:]]
    return ordem


def ordenar_por_proximidade(coordenadas, matriz=None):
    """Compara vizinho mais próximo e rotas randomizadas; otimiza as melhores candidatas."""
    n = len(coordenadas)
    if n <= 2:
        return list(range(n))
    if matriz is None:
        matriz = matriz_haversine(coordenadas)
    if len(matriz) != n or any(len(linha) != n for linha in matriz):
        raise RuntimeError("A matriz de distâncias não corresponde à quantidade de pontos.")
    rng = random.Random()
    candidatos = [construir_vizinho_mais_proximo(coordenadas, matriz)]
    tentativas = min(16, max(4, int(math.sqrt(n)) + 3))
    for _ in range(tentativas):
        candidatos.append(construir_rota_randomizada(coordenadas, matriz, rng, fator_escolha=4))
    melhor_rota, melhor_custo = None, math.inf
    for candidata in candidatos:
        candidata = melhorar_rota_local(candidata, matriz, max_passadas=12, max_avaliacoes=120000)
        custo = custo_rota(candidata, matriz)
        if custo < melhor_custo:
            melhor_rota, melhor_custo = candidata, custo
    if (melhor_rota is None or len(melhor_rota) != n or melhor_rota[0] != 0
            or set(melhor_rota) != set(range(n))):
        raise RuntimeError("A otimização não incluiu todos os pontos exatamente uma vez.")
    return melhor_rota

def obter_detalhes_rota(coordenadas, ordem):
    """Obtém geometria e métricas por perna com uma única chamada OSRM."""
    pontos = [coordenadas[i] for i in ordem]
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in pontos)
    resp = HTTP.get(
        OSRM_ROUTE_URL + coords,
        params={"overview": "full", "geometries": "geojson", "steps": "false"},
        timeout=(5, 45),
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(data.get("message", "OSRM não retornou a rota"))
    rota = data["routes"][0]
    return rota.get("geometry", {}).get("coordinates"), rota.get("legs", [])


def obter_geometria_rota(coordenadas, ordem):
    """Compatibilidade: retorna apenas a geometria da rota."""
    geometria, _ = obter_detalhes_rota(coordenadas, ordem)
    return geometria


def _numero_ou_none(valor):
    """Número finito para o pop-up do mapa; qualquer outra coisa vira None."""
    try:
        v = float(valor)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def exibir_mapa_rota(rota_df, origem, geometria=None):
    'Renderiza Leaflet sem interpolar texto do usuário em HTML executável.'
    pontos = [{"lat": float(origem[0]), "lon": float(origem[1]), "numero": "O", "endereco": "Origem",
               "km": None, "min": None}]
    for _, row in rota_df.iterrows():
        pontos.append({"lat": float(row["Latitude"]), "lon": float(row["Longitude"]),
                       "numero": str(int(row["Parada"])), "endereco": str(row["Endereço"]),
                       "km": _numero_ou_none(row.get("Km desde parada anterior")),
                       "min": _numero_ou_none(row.get("Minutos desde parada anterior"))})
    linha = ([[float(c[1]), float(c[0])] for c in geometria] if geometria
             else [[p["lat"], p["lon"]] for p in pontos])
    # Escapa separadores de script mesmo após JSON.stringify no Python.
    pontos_json = json.dumps(pontos, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    linha_json = json.dumps(linha, ensure_ascii=False, allow_nan=False)
    html_mapa = f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
html,body{{height:100%;width:100%;margin:0;background:transparent;font-family:'Inter','Segoe UI',system-ui,sans-serif}}
#map{{height:100%;width:100%;background:#060814;border-radius:16px;overflow:hidden}}
.leaflet-tile-pane{{filter:invert(1) hue-rotate(180deg) brightness(.92) contrast(.88) saturate(.55)}}
.leaflet-container{{background:#060814;color:#F5F7FB;font-family:inherit}}
.stop-icon,.origin-icon{{position:relative;display:flex;align-items:center;justify-content:center;border-radius:50%;font-weight:700;letter-spacing:-.01em;border:none;box-sizing:border-box;transition:transform .18s ease}}
.stop-icon{{width:26px;height:26px;background:#FFE600;color:#05060D;font-size:11px;box-shadow:0 0 0 4px rgba(255,230,0,.16),0 0 16px 2px rgba(255,230,0,.55)}}
.origin-icon{{width:28px;height:28px;background:#FFFFFF;color:#05060D;font-size:12px;box-shadow:0 0 0 5px rgba(255,255,255,.14),0 0 18px 3px rgba(255,255,255,.55)}}
.leaflet-marker-icon:hover .stop-icon{{transform:scale(1.25)}}
.leaflet-marker-icon:hover .origin-icon{{transform:scale(1.15)}}
.stop-icon::after,.origin-icon::after{{content:'';position:absolute;top:-7px;right:-7px;bottom:-7px;left:-7px;border-radius:50%;opacity:0;animation:gps-pulse 2.6s ease-out infinite}}
.stop-icon::after{{border:1px solid rgba(255,230,0,.55)}}
.origin-icon::after{{border:1px solid rgba(255,255,255,.55)}}
@keyframes gps-pulse{{0%{{transform:scale(.7);opacity:.75}}100%{{transform:scale(1.55);opacity:0}}}}
.rota-fluxo{{animation:rota-fluxo 1.6s linear infinite}}
@keyframes rota-fluxo{{to{{stroke-dashoffset:-36}}}}
@media (prefers-reduced-motion:reduce){{.rota-fluxo,.stop-icon::after,.origin-icon::after{{animation:none}}}}
.leaflet-popup-content-wrapper{{background:rgba(10,12,24,.88);color:#F5F7FB;border:1px solid rgba(255,255,255,.08);border-radius:12px;box-shadow:0 12px 32px rgba(0,0,0,.5);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px)}}
.leaflet-popup-tip{{background:rgba(10,12,24,.88);box-shadow:none}}
.leaflet-popup-content{{margin:12px 16px;font-size:13px;line-height:1.45}}
.leaflet-popup-content strong{{color:#FFFFFF;font-weight:600}}
.leaflet-popup-content .trecho{{margin-top:4px;color:#AEB4C4;font-size:12px}}
a.leaflet-popup-close-button{{color:#7C8398!important}}
.leaflet-bar{{border:none!important;border-radius:12px!important;overflow:hidden;box-shadow:0 8px 24px rgba(0,0,0,.45)!important}}
.leaflet-control-zoom a{{background:rgba(10,12,24,.85)!important;color:#F5F7FB!important;border-bottom:1px solid rgba(255,255,255,.08)!important}}
.leaflet-control-zoom a:hover{{background:rgba(40,50,82,.95)!important}}
.leaflet-control-attribution{{background:rgba(6,8,20,.65)!important;color:#7C8398!important;border-radius:10px 0 0 0;font-size:10px}}
.leaflet-control-attribution a{{color:#AEB4C4!important}}
.legenda{{background:rgba(10,12,24,.82);border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:8px 14px;color:#AEB4C4;font-size:11.5px;line-height:1.9;box-shadow:0 8px 24px rgba(0,0,0,.45);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px)}}
.legenda .dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;vertical-align:middle}}
.legenda .dot-origem{{background:#FFFFFF;box-shadow:0 0 8px rgba(255,255,255,.7)}}
.legenda .dot-parada{{background:#FFE600;box-shadow:0 0 8px rgba(255,230,0,.7)}}
</style></head><body><div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const pontos = {pontos_json}; const linha = {linha_json};
const map = L.map('map', {{zoomControl:true}});
L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom:19, attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>'}}).addTo(map);
const bounds=[];
pontos.forEach(p => {{
 const origem = p.numero === 'O';
 const tam = origem ? 28 : 26;
 const div = document.createElement('div');
 div.className = origem ? 'origin-icon' : 'stop-icon';
 div.textContent = p.numero;
 const icon = L.divIcon({{className:'',html:div.outerHTML,iconSize:[tam,tam],iconAnchor:[tam/2,tam/2]}});
 const popup = document.createElement('div');
 const titulo = document.createElement('strong');
 titulo.textContent = origem ? 'ORIGEM' : 'Parada ' + p.numero;
 const endereco = document.createElement('div');
 endereco.textContent = p.endereco;
 popup.appendChild(titulo); popup.appendChild(endereco);
 if (!origem) {{
  const partes = [];
  if (typeof p.km === 'number') partes.push(p.km.toFixed(1).replace('.', ',') + ' km');
  if (typeof p.min === 'number') partes.push(Math.round(p.min) + ' min');
  if (partes.length) {{
   const trecho = document.createElement('div');
   trecho.className = 'trecho';
   trecho.textContent = partes.join(' · ') + ' desde a parada anterior';
   popup.appendChild(trecho);
  }}
 }}
 L.marker([p.lat,p.lon],{{icon, zIndexOffset: origem ? 1000 : 0, riseOnHover:true}}).addTo(map).bindPopup(popup);
 bounds.push([p.lat,p.lon]);
}});
if (Array.isArray(linha) && linha.length > 1) {{
 L.polyline(linha,{{color:'#FFE600',weight:12,opacity:0.10,lineCap:'round',lineJoin:'round',interactive:false}}).addTo(map);
 L.polyline(linha,{{color:'#FFE600',weight:3.5,opacity:0.60,lineCap:'round',lineJoin:'round',interactive:false}}).addTo(map);
 if (linha.length <= 4000) L.polyline(linha,{{color:'#FFF6A8',weight:3.5,opacity:0.95,dashArray:'3 15',lineCap:'round',lineJoin:'round',className:'rota-fluxo',interactive:false}}).addTo(map);
}}
const legenda = L.control({{position:'bottomleft'}});
legenda.onAdd = function() {{
 const d = L.DomUtil.create('div','legenda');
 d.innerHTML = '<div><span class="dot dot-origem"></span>Origem</div><div><span class="dot dot-parada"></span>Paradas (' + (pontos.length - 1) + ')</div>';
 L.DomEvent.disableClickPropagation(d);
 return d;
}};
legenda.addTo(map);
if(bounds.length) map.fitBounds(bounds,{{padding:[30,30]}});
</script></body></html>'''
    components.html(html_mapa, height=560, scrolling=False)

def calcular_trecho(a, b):
    coords = f"{a[1]:.6f},{a[0]:.6f};{b[1]:.6f},{b[0]:.6f}"
    resp = HTTP.get(OSRM_ROUTE_URL + coords, params={"overview": "false"}, timeout=(5, 25), headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") == "Ok" and data.get("routes"):
        rota = data["routes"][0]
        return rota.get("distance"), rota.get("duration"), "OSRM"
    return None, None, "Distância rodoviária indisponível"

# ============================================================
# IMPORTAÇÃO DE RELATÓRIO PROTHEUS (XML SpreadsheetML)
# Bloco independente: não usa nem altera dados da roteirização.
# Cada linha do XML é um pedido e é sempre preservada individualmente
# (sem agrupar por motorista e sem remover duplicidades).
# ============================================================
PROTHEUS_COLUNAS_FILTRO = ("ITEM", "NOME_MOTORISTA", "DESC_OCORRENCIA", "DT_OCORRENCIA")
PROTHEUS_ITEM_VALIDO = "01"
PROTHEUS_TODOS_MOTORISTAS = "(Todos os motoristas)"
PROTHEUS_TODAS_OCORRENCIAS = "(Todas as ocorrências)"

def _protheus_texto_celula(celula):
    # Texto do <Data> da célula, ignorando o namespace XML.
    for filho in celula:
        if filho.tag.rsplit("}", 1)[-1] == "Data":
            return "".join(filho.itertext()).strip()
    return ""

def _protheus_linha_para_lista(linha):
    # Converte <Row> em lista de textos, respeitando ss:Index (células puladas).
    valores = []
    for celula in linha:
        if celula.tag.rsplit("}", 1)[-1] != "Cell":
            continue
        indice = None
        for chave, valor in celula.attrib.items():
            if chave.rsplit("}", 1)[-1] == "Index":
                try:
                    indice = int(valor)
                except ValueError:
                    indice = None
        if indice is not None and indice - 1 > len(valores):
            valores.extend([""] * (indice - 1 - len(valores)))
        valores.append(_protheus_texto_celula(celula))
    return valores

def ler_xml_protheus(arquivo):
    """Lê o Excel XML (SpreadsheetML) do Protheus em streaming e devolve um DataFrame (tudo texto).

    Mantém uma linha do DataFrame para cada linha de dados do XML.
    """
    import xml.etree.ElementTree as ET

    if hasattr(arquivo, "seek"):
        arquivo.seek(0)
    cabecalho = None
    registros = []
    encontrou_workbook = False
    try:
        for _, elem in ET.iterparse(arquivo, events=("end",)):
            nome_tag = elem.tag.rsplit("}", 1)[-1]
            if nome_tag == "Workbook":
                encontrou_workbook = True
                continue
            if nome_tag != "Row":
                continue
            valores = _protheus_linha_para_lista(elem)
            elem.clear()  # libera memória a cada linha processada
            if cabecalho is None:
                nomes = [v.strip() for v in valores]
                if all(c in nomes for c in PROTHEUS_COLUNAS_FILTRO):
                    cabecalho = []
                    for nome in nomes:
                        base, n = nome or "COLUNA", 2
                        while base in cabecalho:
                            base = f"{nome or 'COLUNA'}_{n}"
                            n += 1
                        cabecalho.append(base)
                continue
            if not any(v for v in valores):
                continue  # linha totalmente vazia (rodapé do relatório)
            if len(valores) < len(cabecalho):
                valores = valores + [""] * (len(cabecalho) - len(valores))
            registros.append(valores[:len(cabecalho)])
    except ET.ParseError as exc:
        raise ValueError(f"XML inválido ou corrompido: {exc}") from exc
    if not encontrou_workbook:
        raise ValueError("O arquivo não é um Excel XML (SpreadsheetML) válido: elemento Workbook não encontrado.")
    if cabecalho is None:
        raise ValueError(
            "Não encontrei a linha de cabeçalhos esperada. Colunas necessárias: "
            + ", ".join(PROTHEUS_COLUNAS_FILTRO) + "."
        )
    if not registros:
        raise ValueError("O relatório não contém registros abaixo do cabeçalho.")
    return pd.DataFrame(registros, columns=cabecalho, dtype=str)

def _protheus_converter_datas(serie):
    # ISO 8601 (inclusive com 'Z') -> datetime sem fuso, mantendo exatamente o horário escrito no XML.
    # Valores vazios ou inválidos viram NaT, sem interromper o aplicativo.
    texto = serie.fillna("").astype(str).str.strip().replace("", pd.NA)
    try:
        convertida = pd.to_datetime(texto, errors="coerce", utc=True, format="mixed")
    except (TypeError, ValueError):
        convertida = pd.to_datetime(texto, errors="coerce", utc=True)
    return convertida.dt.tz_localize(None)

def preparar_base_protheus(df):
    """Aplica o filtro permanente ITEM = 01 e cria as colunas auxiliares (prefixo _) usadas pelos filtros."""
    base = df[df["ITEM"].astype(str).str.strip() == PROTHEUS_ITEM_VALIDO].copy()
    base = base.reset_index(drop=True)  # sem remoção de duplicidades
    # Chave de comparação sem diferença de maiúsculas/minúsculas e espaços (ex.: "Francisco" e "FRANCISCO").
    # Afeta só a lista e o filtro; a coluna original continua intacta na tabela.
    base["_motorista"] = base["NOME_MOTORISTA"].fillna("").astype(str).map(lambda v: " ".join(v.upper().split()))
    base["_ocorrencia"] = base["DESC_OCORRENCIA"].fillna("").astype(str).map(lambda v: " ".join(v.upper().split()))
    base["_data"] = _protheus_converter_datas(base["DT_OCORRENCIA"])
    return base

def opcoes_protheus(serie):
    # Opções de seleção: sem vazios e sem repetidos (somente na lista, nunca na tabela).
    return sorted({v for v in serie if v})

def filtrar_base_protheus(base, motorista=None, ocorrencias=None, data_inicio=None, data_fim=None):
    """Aplica os filtros de forma combinada. Devolve todas as linhas que atendem aos critérios."""
    mascara = pd.Series(True, index=base.index)
    if motorista and motorista != PROTHEUS_TODOS_MOTORISTAS:
        mascara &= base["_motorista"] == motorista
    if ocorrencias and PROTHEUS_TODAS_OCORRENCIAS not in ocorrencias:
        mascara &= base["_ocorrencia"].isin(list(ocorrencias))
    if data_inicio is not None:
        mascara &= base["_data"].notna() & (base["_data"].dt.normalize() >= pd.Timestamp(data_inicio))
    if data_fim is not None:
        mascara &= base["_data"].notna() & (base["_data"].dt.normalize() <= pd.Timestamp(data_fim))
    return base[mascara]

def tabela_protheus_exibicao(filtrado):
    # Colunas principais primeiro; todas as demais do relatório são mantidas.
    exibicao = filtrado.drop(columns=[c for c in filtrado.columns if c.startswith("_")]).copy()
    datas = filtrado["_data"].dt.strftime("%d/%m/%Y %H:%M")
    exibicao["DT_OCORRENCIA"] = datas.where(filtrado["_data"].notna(), exibicao["DT_OCORRENCIA"])
    ordem = list(PROTHEUS_COLUNAS_FILTRO) + [c for c in exibicao.columns if c not in PROTHEUS_COLUNAS_FILTRO]
    return exibicao[ordem]

def secao_importacao_protheus():
    with st.expander("Importação de Relatório Protheus", expanded=False):
        try:
            xml = st.file_uploader("Relatório XML do Protheus", key="protheus_xml")  # sem filtro de extensão: o Protheus pode gerar nomes como "arquivo.xml_2026..."; o conteúdo é validado na leitura
            if xml is None:
                st.caption("Envie o XML exportado pelo Protheus. Somente ITEM = 01 é considerado; cada linha do relatório é um pedido e todas são mantidas.")
                return
            assinatura = (getattr(xml, "file_id", None), xml.name, xml.size)
            if st.session_state.get("protheus_assinatura") != assinatura:
                with st.spinner("Processando relatório Protheus..."):
                    bruto = ler_xml_protheus(xml)
                    st.session_state["protheus_total"] = len(bruto)
                    st.session_state["protheus_base"] = preparar_base_protheus(bruto)
                    st.session_state["protheus_assinatura"] = assinatura
            total = st.session_state["protheus_total"]
            base = st.session_state["protheus_base"]
            st.success("Importação concluída.")
            c1, c2 = st.columns(2)
            c1.metric("Registros importados", f"{total:,}".replace(",", "."))
            c2.metric("Registros com ITEM = 01", f"{len(base):,}".replace(",", "."))
            if base.empty:
                st.warning("Nenhum registro com ITEM = 01 neste relatório.")
                return

            f1, f2 = st.columns(2)
            with f1:
                motorista = st.selectbox("Motorista (NOME_MOTORISTA)", [PROTHEUS_TODOS_MOTORISTAS] + opcoes_protheus(base["_motorista"]), key="protheus_motorista")
            with f2:
                ocorrencias = st.multiselect(
                    "Descrição da ocorrência (DESC_OCORRENCIA)",
                    [PROTHEUS_TODAS_OCORRENCIAS] + opcoes_protheus(base["_ocorrencia"]),
                    default=[PROTHEUS_TODAS_OCORRENCIAS],
                    key="protheus_ocorrencias",
                )

            data_inicio = data_fim = None
            datas_validas = base["_data"].dropna()
            if datas_validas.empty:
                st.caption("Nenhuma data de ocorrência válida no relatório; filtro de período indisponível.")
            else:
                minimo, maximo = datas_validas.min().date(), datas_validas.max().date()
                periodo = st.date_input("Período da ocorrência (DT_OCORRENCIA)", value=(minimo, maximo), min_value=minimo, max_value=maximo, format="DD/MM/YYYY", key="protheus_periodo")
                if isinstance(periodo, (list, tuple)):
                    selecionadas = list(periodo)
                else:
                    selecionadas = [periodo]
                if len(selecionadas) >= 1:
                    inicio = selecionadas[0]
                    fim = selecionadas[1] if len(selecionadas) > 1 else maximo
                    # Só filtra por data se o período foi restringido; assim, no padrão, registros sem data continuam visíveis.
                    if inicio != minimo or fim != maximo:
                        data_inicio, data_fim = inicio, fim
                sem_data = int(base["_data"].isna().sum())
                if sem_data:
                    st.caption(f"{sem_data} registro(s) sem data válida (aparecem enquanto o período não for restringido).")

            filtrado = filtrar_base_protheus(base, motorista, ocorrencias, data_inicio, data_fim)
            exibicao = tabela_protheus_exibicao(filtrado)
            st.markdown(f"**{len(exibicao)} registro(s)** atendem aos filtros (todas as linhas de pedido são exibidas, sem agrupamento).")
            st.dataframe(exibicao, use_container_width=True, hide_index=True)
            st.download_button(
                "Baixar resultado filtrado (CSV)",
                exibicao.to_csv(index=False, sep=";").encode("utf-8-sig"),
                file_name="protheus_filtrado.csv",
                mime="text/csv",
                key="protheus_download",
            )
        except Exception as exc:
            st.error(f"Não foi possível processar o relatório Protheus: {exc}")
            st.session_state.pop("protheus_assinatura", None)

try:
    secao_importacao_protheus()
except Exception as _exc_protheus:
    st.error(f"Falha na seção de importação Protheus (o restante do aplicativo não foi afetado): {_exc_protheus}")

with st.sidebar:
    st.header("Configuração")
    st.markdown('<div class="side-label">Área e origem</div>', unsafe_allow_html=True)
    cidade_padrao = st.text_input("Cidade/área padrão para buscar endereços", "Campina Grande, PB, Brasil")
    origem_texto = st.text_input("Endereço de origem", ORIGEM_PADRAO)
    st.caption("A origem é fixa. O sistema compara várias sequências completas e escolhe a de menor distância rodoviária estimada, tentando evitar retornos entre extremos da cidade.")
    _painel_servicos()

arquivo = st.file_uploader("Carregue a planilha CSV ou Excel", type=["csv", "xlsx", "xls"])
if arquivo is None:
    st.markdown(
        '<div class="estado-vazio">'
        '<div class="icone">↑</div>'
        '<div class="titulo">Formato esperado</div>'
        '<p>Sua planilha precisa ter uma coluna com os endereços. O nome padrão é <code>endereco_entrega</code>, mas você poderá selecionar outra coluna.</p>'
        '<p>As linhas não são limitadas a 10. Endereços repetidos continuam sendo entregas separadas.</p>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.stop()

_atualizar_etapas(True)

try:
    df_original = ler_planilha(arquivo)
except Exception as exc:
    st.error(f"Não consegui ler a planilha: {exc}")
    st.stop()

if df_original.empty:
    st.warning("A planilha não contém linhas.")
    st.stop()

colunas = list(df_original.columns)
# Detecta as quatro colunas usadas na planilha de entregas, ignorando maiúsculas/minúsculas e espaços.
colunas_normalizadas = {normalizar(c).replace(" ", "_"): c for c in colunas}
coluna_rua_auto = colunas_normalizadas.get("RUA_ENTREG")
coluna_bairro_auto = colunas_normalizadas.get("BAIRRO_ENTREG")
coluna_municipio_auto = colunas_normalizadas.get("MUNICIPIO_ENTREG")
coluna_estado_auto = colunas_normalizadas.get("ESTADO_ENTREG")
usar_campos_separados = all((coluna_rua_auto, coluna_bairro_auto, coluna_municipio_auto, coluna_estado_auto))

if usar_campos_separados:
    st.success("Detectei RUA_ENTREG, BAIRRO_ENTREG, MUNICIPIO_ENTREG e ESTADO_ENTREG. A busca usará os quatro campos juntos automaticamente.")
    coluna_endereco = None
    coluna_cidade = None
else:
    st.warning("Não detectei todas as quatro colunas de endereço. Selecione uma coluna com o endereço completo ou confira os cabeçalhos da planilha.")
    sugerida = next((c for c in colunas if normalizar(c).replace(" ", "_") in ("ENDERECO_ENTREGA", "ENDERECO", "ENDERECO_COMPLETO", "DESTINO")), colunas[0])
    col1, col2 = st.columns(2)
    with col1:
        coluna_endereco = st.selectbox("Coluna que contém o endereço", colunas, index=colunas.index(sugerida))
    with col2:
        coluna_cidade = st.selectbox("Coluna de cidade/bairro (opcional)", ["(usar cidade padrão)"] + colunas)
# Detecta automaticamente nomes comuns para número/identificador do pedido.
coluna_pedido_sugerida = next((c for c in colunas if any(
    termo in normalizar(c).replace(" ", "_")
    for termo in ("NUMERO_PEDIDO", "NÚMERO_PEDIDO", "PEDIDO", "ORDEM", "ID_PEDIDO", "CODIGO_PEDIDO")
)), "(não informar)")
coluna_pedido = st.selectbox(
    "Coluna do número do pedido (opcional)",
    ["(não informar)"] + colunas,
    index=(["(não informar)"] + colunas).index(coluna_pedido_sugerida),
    help="Escolha a coluna que identifica cada pedido. O número acompanhará o endereço na rota e nos relatórios CSV/Excel."
)

if "geocodificados" not in st.session_state:
    st.session_state.geocodificados = None
if "resultados_rota" not in st.session_state:
    st.session_state.resultados_rota = None

if st.button("Buscar todos os endereços", type="primary", use_container_width=True):
    trabalho = df_original.copy()
    trabalho["_linha_planilha"] = range(2, len(trabalho) + 2)
    resultados = []
    barra = st.progress(0)
    status = st.empty()
    total = len(trabalho)
    for pos, (_, linha) in enumerate(trabalho.iterrows()):
        if usar_campos_separados:
            # Rua/número + bairro + município + estado: todos os campos entram na consulta.
            partes_endereco = [
                linha.get(coluna_rua_auto, ""),
                linha.get(coluna_bairro_auto, ""),
                linha.get(coluna_municipio_auto, ""),
                linha.get(coluna_estado_auto, ""),
                "Brasil",
            ]
            endereco = ", ".join(str(x).strip() for x in partes_endereco if str(x or "").strip())
            cidade = ", ".join(str(linha.get(c, "")).strip() for c in (coluna_municipio_auto, coluna_estado_auto) if str(linha.get(c, "") or "").strip())
        else:
            endereco = str(linha[coluna_endereco]).strip()
            cidade = cidade_padrao if coluna_cidade == "(usar cidade padrão)" else str(linha[coluna_cidade]).strip()
        if not cidade:
            cidade = cidade_padrao
        status.write(f"**{pos+1}/{total}** · {endereco}")
        resultado, situacao = geocodificar(endereco, cidade)
        resultados.append({
            "linha_planilha": int(linha["_linha_planilha"]),
            "numero_pedido": (linha[coluna_pedido] if coluna_pedido != "(não informar)" else ""),
            "endereco": endereco,
            "cidade": cidade,
            "latitude": resultado["lat"] if resultado else None,
            "longitude": resultado["lon"] if resultado else None,
            "status_geocodificacao": situacao,
            "motor": resultado["motor"] if resultado else "",
            "resultado_encontrado": resultado["nome"] if resultado else "",
            "consulta_usada": resultado["consulta"] if resultado else "",
        })
        barra.progress((pos + 1) / total)
    st.session_state.geocodificados = pd.DataFrame(resultados)
    st.session_state.resultados_rota = None
    status.success("Busca concluída. Confira os endereços pendentes abaixo.")

_atualizar_etapas(True)
geo = st.session_state.geocodificados
if geo is not None:
    ok = geo["latitude"].notna() & geo["longitude"].notna()
    c1, c2, c3 = st.columns(3)
    c1.metric("Entregas na planilha", len(geo))
    c2.metric("Localizadas", int(ok.sum()))
    c3.metric("Pendentes de confirmação", int((~ok).sum()))
    _pilulas([("Todas localizadas" if int((~ok).sum()) == 0 else f"{int((~ok).sum())} pendente(s)",
               "ok" if int((~ok).sum()) == 0 else "aviso")])

    st.subheader("Endereços localizados")
    _mostrar_tabela(geo.loc[ok, ["linha_planilha", "endereco", "latitude", "longitude", "motor", "resultado_encontrado"]], ESPEC_LOCALIZADOS)

    pendentes = geo.index[~ok].tolist()
    if pendentes:
        st.warning(f"Há {len(pendentes)} entrega(s) sem coordenadas. A rota completa só será liberada depois de confirmar todas.")
        st.subheader("Confirmar endereços não encontrados")
        st.caption("Abra o Maps, confirme o ponto exato e copie latitude/longitude do local. Também é possível pesquisar um endereço corrigido abaixo.")
        for idx in pendentes:
            with st.expander(f"Linha {int(geo.at[idx, 'linha_planilha'])} — {geo.at[idx, 'endereco']}"):
                st.markdown(f"[Conferir no Google Maps]({abrir_google_maps(geo.at[idx, 'endereco'], geo.at[idx, 'cidade'])})")
                chave_sugestao = f"sugestoes_endereco_{idx}"
                if st.button("Procurar endereços semelhantes", key=f"buscar_semelhantes_{idx}"):
                    with st.spinner("Buscando alternativas..."):
                        sugestoes, erros_busca = buscar_enderecos_semelhantes(
                            geo.at[idx, "endereco"], geo.at[idx, "cidade"]
                        )
                    st.session_state[chave_sugestao] = sugestoes
                    st.session_state[f"diagnostico_{idx}"] = erros_busca
                    if not sugestoes:
                        st.warning("Nenhum candidato foi retornado pelas consultas realizadas.")
                        st.caption(
                            f"Consultas tentadas: {len(erros_busca.get('consultas', []))} | "
                            f"Requisições concluídas: {erros_busca.get('requisicoes', 0)} | "
                            f"Consultas com algum resultado: {erros_busca.get('consultas_com_resultados', 0)}"
                        )
                        if erros_busca.get("erros"):
                            with st.expander("Ver detalhes técnicos da pesquisa"):
                                for erro_busca in erros_busca["erros"][:8]:
                                    st.code(erro_busca)
                        else:
                            st.info("Os serviços responderam, mas não retornaram resultados para essas variações. Tente incluir o bairro ou corrigir o nome da rua.")

                sugestoes = st.session_state.get(chave_sugestao, [])
                if sugestoes:
                    st.write("**Confira as opções encontradas:**")
                    escolha = st.selectbox(
                        "Endereço sugerido",
                        options=list(range(len(sugestoes))),
                        format_func=lambda i: f"{i + 1}. {sugestoes[i]['nome']} ({sugestoes[i]['motor']})",
                        key=f"escolha_sugestao_{idx}",
                    )
                    candidato = sugestoes[escolha]
                    st.caption(f"Latitude: {candidato['lat']:.7f} | Longitude: {candidato['lon']:.7f}")
                    url_ponto = f"https://www.google.com/maps/search/?api=1&query={candidato['lat']},{candidato['lon']}"
                    st.markdown(f"[Conferir este ponto no Google Maps]({url_ponto})")
                    if st.button("Confirmar este endereço", key=f"confirmar_sugestao_{idx}"):
                        st.session_state.geocodificados.at[idx, "latitude"] = candidato["lat"]
                        st.session_state.geocodificados.at[idx, "longitude"] = candidato["lon"]
                        st.session_state.geocodificados.at[idx, "endereco"] = candidato["nome"]
                        st.session_state.geocodificados.at[idx, "status_geocodificacao"] = "Confirmado por sugestão"
                        st.session_state.geocodificados.at[idx, "motor"] = candidato["motor"]
                        st.session_state.geocodificados.at[idx, "resultado_encontrado"] = candidato["nome"]
                        st.session_state.geocodificados.at[idx, "consulta_usada"] = candidato["consulta"]
                        st.session_state.resultados_rota = None
                        st.rerun()

                novo_endereco = st.text_input("Endereço corrigido (opcional)", value=geo.at[idx, "endereco"], key=f"corr_end_{idx}")
                a, b = st.columns(2)
                with a:
                    lat = st.number_input("Latitude", min_value=-90.0, max_value=90.0, value=0.0, format="%.7f", key=f"lat_{idx}")
                with b:
                    lon = st.number_input("Longitude", min_value=-180.0, max_value=180.0, value=0.0, format="%.7f", key=f"lon_{idx}")
                if st.button("Tentar endereço corrigido", key=f"tentar_{idx}"):
                    resultado, situacao = geocodificar(novo_endereco, geo.at[idx, "cidade"])
                    if resultado:
                        st.session_state.geocodificados.at[idx, "endereco"] = novo_endereco
                        st.session_state.geocodificados.at[idx, "latitude"] = resultado["lat"]
                        st.session_state.geocodificados.at[idx, "longitude"] = resultado["lon"]
                        st.session_state.geocodificados.at[idx, "status_geocodificacao"] = "OK (corrigido)"
                        st.session_state.geocodificados.at[idx, "motor"] = resultado["motor"]
                        st.session_state.geocodificados.at[idx, "resultado_encontrado"] = resultado["nome"]
                        st.rerun()
                    else:
                        st.error(f"Não localizado automaticamente: {situacao}. Confirme no Maps ou informe as coordenadas.")
                if st.button("Salvar coordenadas informadas", key=f"salvar_coord_{idx}"):
                    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0) or (lat == 0.0 and lon == 0.0):
                        st.error("Informe coordenadas válidas: latitude entre -90 e 90 e longitude entre -180 e 180; (0, 0) não é aceito.")
                    else:
                        st.session_state.geocodificados.at[idx, "endereco"] = novo_endereco
                        st.session_state.geocodificados.at[idx, "latitude"] = float(lat)
                        st.session_state.geocodificados.at[idx, "longitude"] = float(lon)
                        st.session_state.geocodificados.at[idx, "status_geocodificacao"] = "Confirmado manualmente"
                        st.session_state.geocodificados.at[idx, "motor"] = "Manual/Google Maps"
                        st.session_state.geocodificados.at[idx, "resultado_encontrado"] = "Coordenadas confirmadas pelo usuário"
                        try:
                            salvar_endereco_confirmado(novo_endereco, geo.at[idx, "cidade"], lat, lon, origem="manual")
                        except (sqlite3.Error, ValueError, TypeError) as exc:
                            st.error(f"As coordenadas foram usadas nesta rota, mas não foi possível salvá-las para próximas execuções: {exc}")
                        st.rerun()

    geo = st.session_state.geocodificados
    tudo_localizado = geo["latitude"].notna().all() and geo["longitude"].notna().all()
    st.subheader("Gerar rota")
    st.write("A rota começa pela origem. O sistema compara vizinho mais próximo com várias sequências randomizadas, melhora cada uma por movimentos locais e escolhe a de menor distância estimada. Assim, uma região extrema próxima das primeiras entregas pode ser atendida antes de avançar para o extremo mais distante, evitando retornos desnecessários. É uma heurística e não garante o ótimo matemático.")
    if not tudo_localizado:
        st.error("Rota completa bloqueada: ainda existem entregas sem coordenadas. Confirme cada uma acima para incluir todas.")
    if st.button("Calcular rota completa", disabled=not tudo_localizado, use_container_width=True):
        with st.spinner("Calculando a rota..."):
            origem_resultado, origem_status = geocodificar(origem_texto, cidade_padrao)
            if not origem_resultado:
                st.error(f"Não consegui localizar a origem automaticamente ({origem_status}). Corrija o endereço de origem ou escolha coordenadas válidas.")
                st.stop()
            coords_entregas = [(float(r.latitude), float(r.longitude)) for r in geo.itertuples()]
            coordenadas = [(origem_resultado["lat"], origem_resultado["lon"])] + coords_entregas
            nomes = [f"ORIGEM: {origem_texto}"] + [str(x) for x in geo["endereco"].tolist()]
            try:
                matriz, pares_fallback = osrm_matriz(coordenadas)
                if pares_fallback:
                    st.warning(f"OSRM não encontrou caminho para {len(pares_fallback)} par(es). Esses pares receberam estimativa Haversine multiplicada por {FATOR_RODOVIARIO_HAVERSINE:.2f}; a rota pode ser aproximada.")
            except Exception as exc:
                st.warning(f"A matriz OSRM falhou ({exc}). Será usada uma matriz Haversine aproximada; distâncias e tempos serão estimativas, não trajetos rodoviários confirmados.")
                matriz = matriz_haversine(coordenadas)
                pares_fallback = [(i, j) for i in range(len(coordenadas)) for j in range(len(coordenadas)) if i != j]
            try:
                ordem = ordenar_por_proximidade(coordenadas, matriz)
            except Exception as exc:
                st.error(f"Não foi possível calcular uma sequência confiável pelas ruas: {exc}")
                st.stop()
            geometria_mapa = None
            erro_mapa = None
            pernas_osrm = []
            try:
                geometria_mapa, pernas_osrm = obter_detalhes_rota(coordenadas, ordem)
                if len(pernas_osrm) != len(ordem) - 1:
                    pernas_osrm = []
                    erro_mapa = "OSRM retornou quantidade inesperada de trechos; o tempo será estimado onde necessário."
            except Exception as exc:
                erro_mapa = str(exc)

            pares_fallback_set = set(pares_fallback)
            linhas_rota = []
            total_m = 0.0
            total_s = 0.0
            falhas_trecho = 0
            for etapa, indice in enumerate(ordem[1:], start=1):
                anterior = ordem[etapa - 1]
                motor_trecho = "OSRM"

                # A distância vem da mesma matriz direcional usada pelo otimizador.
                metros = _dist(matriz, anterior, indice)
                par_usou_fallback = (anterior, indice) in pares_fallback_set
                if (not math.isfinite(metros)):
                    metros = distancia_haversine(
                        coordenadas[anterior], coordenadas[indice]
                    ) * FATOR_RODOVIARIO_HAVERSINE
                    par_usou_fallback = True
                if par_usou_fallback:
                    motor_trecho = "Haversine (fallback de distância)"

                segundos = None
                tempo_estimado = False
                if pernas_osrm and etapa - 1 < len(pernas_osrm):
                    perna = pernas_osrm[etapa - 1]
                    valor_tempo = perna.get("duration") if isinstance(perna, dict) else None
                    if (isinstance(valor_tempo, (int, float))
                            and math.isfinite(float(valor_tempo)) and valor_tempo >= 0):
                        segundos = float(valor_tempo)

                if segundos is None:
                    tempo_estimado = True
                    segundos = (float(metros) / 1000.0) / VELOCIDADE_FALLBACK_KMH * 3600.0
                    motor_trecho = (
                        "Haversine + tempo estimado"
                        if par_usou_fallback
                        else "Distância da matriz + tempo estimado"
                    )
                if par_usou_fallback or tempo_estimado:
                    falhas_trecho += 1

                km = float(metros) / 1000.0
                minutos = float(segundos) / 60.0
                total_m += float(metros)
                total_s += float(segundos)
                idx_geo = indice - 1
                registro = {
                    "Parada": etapa,
                    "Endereço": geo.iloc[idx_geo]["endereco"],
                    "Linha original": int(geo.iloc[idx_geo]["linha_planilha"]),
                    "Latitude": coordenadas[indice][0],
                    "Longitude": coordenadas[indice][1],
                    "Km desde parada anterior": round(km, 2),
                    "Minutos desde parada anterior": round(minutos, 1),
                    "Método de distância/tempo": motor_trecho,
                    "Google Maps": abrir_google_maps(geo.iloc[idx_geo]["endereco"], geo.iloc[idx_geo]["cidade"]),
                    "Status": "Incluída",
                }
                if coluna_pedido != "(não informar)":
                    registro["Número do pedido"] = geo.iloc[idx_geo].get("numero_pedido", "")
                    registro = {
                        "Parada": registro["Parada"],
                        "Número do pedido": registro["Número do pedido"],
                        **{k: v for k, v in registro.items() if k not in ("Parada", "Número do pedido")},
                    }
                linhas_rota.append(registro)
            rota_df = pd.DataFrame(linhas_rota)
            st.session_state.resultados_rota = {
                "origem_coordenadas": coordenadas[0],
                "geometria_mapa": geometria_mapa,
                "erro_mapa": erro_mapa,
                "rota": rota_df,
                "origem": origem_texto,
                "total": len(geo),
                "total_km": total_m / 1000,
                "total_min": total_s / 60,
                "falhas_trecho": falhas_trecho,
                "metodo_ordem": "múltiplas sequências completas + melhoria local + menor distância rodoviária total",
            }

    _atualizar_etapas(True)
    resultado_rota = st.session_state.resultados_rota
    if resultado_rota:
        rota_df = resultado_rota["rota"]
        st.success(f"Rota gerada com {len(rota_df)} de {resultado_rota['total']} entregas.")
        if len(rota_df) == resultado_rota["total"]:
            st.success("Validação concluída: todas as entregas da planilha estão incluídas na rota.")
        else:
            st.error("Atenção: a contagem de entregas da rota não corresponde à planilha.")
        st.write(f"**Critério de proximidade:** {resultado_rota['metodo_ordem']}.")
        m1, m2, m3 = st.columns(3)
        m1.metric("Entregas na rota", len(rota_df))
        m2.metric("Distância rodoviária calculada", f"{resultado_rota['total_km']:.2f} km")
        m3.metric("Tempo rodoviário calculado", f"{resultado_rota['total_min']:.0f} min")
        _pilulas([
            ("Rota completa" if len(rota_df) == resultado_rota["total"] else "Contagem divergente",
             "ok" if len(rota_df) == resultado_rota["total"] else "erro"),
            ("Distâncias via OSRM" if not resultado_rota["falhas_trecho"]
             else f"{resultado_rota['falhas_trecho']} trecho(s) aproximado(s)",
             "ok" if not resultado_rota["falhas_trecho"] else "aviso"),
        ])
        if resultado_rota["falhas_trecho"]:
            st.warning(f"{resultado_rota['falhas_trecho']} trecho(s) usaram fallback Haversine e/ou tempo estimado. Consulte a coluna 'Método de distância/tempo'; os totais continuam preenchidos, mas são aproximados nesses trechos.")
        st.subheader("Mapa da rota sequenciada")
        st.caption("O ponto branco O representa a origem; os pontos amarelos numerados mostram a ordem das entregas. A linha mostra o trajeto pelas ruas quando o OSRM fornece a geometria.")
        if resultado_rota.get("erro_mapa"):
            st.warning("Não foi possível obter a linha pelas ruas neste momento. O mapa exibirá uma ligação aproximada entre as paradas. " + str(resultado_rota["erro_mapa"]))
        exibir_mapa_rota(rota_df, resultado_rota["origem_coordenadas"], resultado_rota.get("geometria_mapa"))
        _mostrar_tabela(rota_df, ESPEC_ROTA)
        csv = rota_df.to_csv(index=False).encode("utf-8-sig")
        st.download_button("Baixar rota em CSV", data=csv, file_name="rota_completa.csv", mime="text/csv")
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            rota_df.to_excel(writer, index=False, sheet_name="Rota")
            geo.to_excel(writer, index=False, sheet_name="Geocodificacao")
        st.download_button("Baixar relatório Excel", data=buffer.getvalue(), file_name="relatorio_roteirizador.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.caption("O botão Google Maps de cada endereço abre a busca desse destino. Trechos sem resposta OSRM usam Haversine × fator rodoviário e tempo estimado de 25 km/h; esses valores são aproximações, não distâncias reais de condução.")
