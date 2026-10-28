import io
import difflib
import json
import math
import os
import random
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
st.title("🚚 Roteirizador de Entregas")
st.caption("Busca gratuita de endereços, confirmação manual e roteirização por múltiplas rotas candidatas, otimização global e distâncias rodoviárias.")

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

def montar_consultas(endereco, cidade):
    endereco = str(endereco or "").strip()
    cidade = str(cidade or "").strip()
    if not endereco:
        return []
    consultas = [
        endereco,
        f"{endereco}, {cidade}" if cidade else endereco,
        f"{endereco}, Campina Grande, PB, Brasil",
        f"{endereco}, Paraíba, Brasil",
    ]
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


def geocodificar(endereco, cidade):
    chave = _chave_geocoder("endereco", f"{endereco}|{cidade}")
    cache = _cache_ler("geocodificacao", chave)
    if cache is not None:
        return cache.get("resultado"), cache.get("situacao", "OK (cache SQLite)")
    consultas = montar_consultas(endereco, cidade)
    erros_conexao = []
    for consulta in consultas:
        for motor, funcao in (("Nominatim", geocodificar_nominatim), ("Photon", geocodificar_photon)):
            try:
                resultado = funcao(consulta)
                if resultado:
                    resultado["consulta"] = consulta
                    _cache_gravar("geocodificacao", chave, {"resultado": resultado, "situacao": "OK"}, motor)
                    return resultado, "OK"
            except requests.RequestException as exc:
                erros_conexao.append(f"{motor}: {exc}")
            except (ValueError, KeyError, TypeError) as exc:
                erros_conexao.append(f"{motor}: {exc}")
    situacao = "Falha de conexão/serviço" if erros_conexao and len(erros_conexao) >= len(consultas) else "Não encontrado automaticamente"
    # Cache negativo curto evita tempestade de requisições em repetição imediata.
    return None, situacao

def _consultas_ampliadas(endereco, cidade):
    """Gera consultas mais flexíveis e evita repetir a cidade no texto."""
    import re

    endereco = " ".join(str(endereco or "").strip().split())
    cidade = " ".join(str(cidade or "").strip().split()) or "Campina Grande, PB, Brasil"
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

    base = [base_endereco]
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
    return consultas[:8]


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


def exibir_mapa_rota(rota_df, origem, geometria=None):
    'Renderiza Leaflet sem interpolar texto do usuário em HTML executável.'
    pontos = [{"lat": float(origem[0]), "lon": float(origem[1]), "numero": "O", "endereco": "Origem"}]
    for _, row in rota_df.iterrows():
        pontos.append({"lat": float(row["Latitude"]), "lon": float(row["Longitude"]),
                       "numero": str(int(row["Parada"])), "endereco": str(row["Endereço"])})
    linha = ([[float(c[1]), float(c[0])] for c in geometria] if geometria
             else [[p["lat"], p["lon"]] for p in pontos])
    # Escapa separadores de script mesmo após JSON.stringify no Python.
    pontos_json = json.dumps(pontos, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    linha_json = json.dumps(linha, ensure_ascii=False, allow_nan=False)
    html_mapa = f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>html,body,#map{{height:100%;width:100%;margin:0}} .stop-icon{{background:#1769aa;color:white;border:2px solid white;border-radius:50%;width:30px;height:30px;line-height:26px;text-align:center;font-weight:bold;box-shadow:0 1px 5px #555;font-size:13px}} .origin-icon{{background:#16803c;color:white;border:2px solid white;border-radius:50%;width:32px;height:32px;line-height:28px;text-align:center;font-weight:bold;box-shadow:0 1px 5px #555}}</style></head><body><div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const pontos = {pontos_json}; const linha = {linha_json};
const map = L.map('map');
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom:19, attribution:'&copy; OpenStreetMap contributors'}}).addTo(map);
const bounds=[];
pontos.forEach(p => {{
 const origem = p.numero === 'O';
 const div = document.createElement('div');
 div.className = origem ? 'origin-icon' : 'stop-icon';
 div.textContent = p.numero;
 const icon = L.divIcon({{className:'',html:div.outerHTML,iconSize:[32,32],iconAnchor:[16,16]}});
 const popup = document.createElement('div');
 const titulo = document.createElement('strong');
 titulo.textContent = origem ? 'ORIGEM' : 'Parada ' + p.numero;
 const endereco = document.createElement('div');
 endereco.textContent = p.endereco;
 popup.appendChild(titulo); popup.appendChild(endereco);
 L.marker([p.lat,p.lon],{{icon}}).addTo(map).bindPopup(popup);
 bounds.push([p.lat,p.lon]);
}});
if (Array.isArray(linha) && linha.length > 1) L.polyline(linha,{{color:'#1769aa',weight:5,opacity:0.8}}).addTo(map);
if(bounds.length) map.fitBounds(bounds,{{padding:[25,25]}});
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

with st.sidebar:
    st.header("Configuração")
    cidade_padrao = st.text_input("Cidade/área padrão para buscar endereços", "Campina Grande, PB, Brasil")
    origem_texto = st.text_input("Endereço de origem", ORIGEM_PADRAO)
    st.caption("A origem é fixa. O sistema compara várias sequências completas e escolhe a de menor distância rodoviária estimada, tentando evitar retornos entre extremos da cidade.")

arquivo = st.file_uploader("Carregue a planilha CSV ou Excel", type=["csv", "xlsx", "xls"])
if arquivo is None:
    st.markdown("### Formato esperado")
    st.write("Sua planilha precisa ter uma coluna com os endereços. O nome padrão é `endereco_entrega`, mas você poderá selecionar outra coluna.")
    st.write("As linhas não são limitadas a 10. Endereços repetidos continuam sendo entregas separadas.")
    st.stop()

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

if st.button("🔎 Buscar todos os endereços", type="primary", use_container_width=True):
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
        status.write(f"Buscando endereço {pos+1} de {total}: {endereco}")
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

geo = st.session_state.geocodificados
if geo is not None:
    ok = geo["latitude"].notna() & geo["longitude"].notna()
    c1, c2, c3 = st.columns(3)
    c1.metric("Entregas na planilha", len(geo))
    c2.metric("Localizadas", int(ok.sum()))
    c3.metric("Pendentes de confirmação", int((~ok).sum()))

    st.subheader("Endereços localizados")
    st.dataframe(geo.loc[ok, ["linha_planilha", "endereco", "latitude", "longitude", "motor", "resultado_encontrado"]], use_container_width=True, hide_index=True)

    pendentes = geo.index[~ok].tolist()
    if pendentes:
        st.warning(f"Há {len(pendentes)} entrega(s) sem coordenadas. A rota completa só será liberada depois de confirmar todas.")
        st.subheader("Confirmar endereços não encontrados")
        st.caption("Abra o Maps, confirme o ponto exato e copie latitude/longitude do local. Também é possível pesquisar um endereço corrigido abaixo.")
        for idx in pendentes:
            with st.expander(f"Linha {int(geo.at[idx, 'linha_planilha'])} — {geo.at[idx, 'endereco']}"):
                st.markdown(f"[🔎 Conferir no Google Maps]({abrir_google_maps(geo.at[idx, 'endereco'], geo.at[idx, 'cidade'])})")
                chave_sugestao = f"sugestoes_endereco_{idx}"
                if st.button("🔍 Procurar endereços semelhantes", key=f"buscar_semelhantes_{idx}"):
                    with st.spinner("Pesquisando alternativas nos serviços gratuitos..."):
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
                    st.markdown(f"[📍 Conferir este ponto no Google Maps]({url_ponto})")
                    if st.button("✅ Confirmar este endereço", key=f"confirmar_sugestao_{idx}"):
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
                        st.rerun()

    geo = st.session_state.geocodificados
    tudo_localizado = geo["latitude"].notna().all() and geo["longitude"].notna().all()
    st.subheader("Gerar rota")
    st.write("A rota começa pela origem. O sistema compara vizinho mais próximo com várias sequências randomizadas, melhora cada uma por movimentos locais e escolhe a de menor distância estimada. Assim, uma região extrema próxima das primeiras entregas pode ser atendida antes de avançar para o extremo mais distante, evitando retornos desnecessários. É uma heurística e não garante o ótimo matemático.")
    if not tudo_localizado:
        st.error("Rota completa bloqueada: ainda existem entregas sem coordenadas. Confirme cada uma acima para incluir todas.")
    if st.button("🧭 Calcular rota completa", disabled=not tudo_localizado, use_container_width=True):
        with st.spinner("Localizando a origem e calculando a sequência..."):
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
        if resultado_rota["falhas_trecho"]:
            st.warning(f"{resultado_rota['falhas_trecho']} trecho(s) usaram fallback Haversine e/ou tempo estimado. Consulte a coluna 'Método de distância/tempo'; os totais continuam preenchidos, mas são aproximados nesses trechos.")
        st.subheader("🗺️ Mapa da rota sequenciada")
        st.caption("O marcador verde O representa a origem; os marcadores azuis numerados mostram a ordem das entregas. A linha mostra o trajeto pelas ruas quando o OSRM fornece a geometria.")
        if resultado_rota.get("erro_mapa"):
            st.warning("Não foi possível obter a linha pelas ruas neste momento. O mapa exibirá uma ligação aproximada entre as paradas. " + str(resultado_rota["erro_mapa"]))
        exibir_mapa_rota(rota_df, resultado_rota["origem_coordenadas"], resultado_rota.get("geometria_mapa"))
        st.dataframe(rota_df, use_container_width=True, hide_index=True)
        csv = rota_df.to_csv(index=False).encode("utf-8-sig")
        st.download_button("⬇️ Baixar rota em CSV", data=csv, file_name="rota_completa.csv", mime="text/csv")
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            rota_df.to_excel(writer, index=False, sheet_name="Rota")
            geo.to_excel(writer, index=False, sheet_name="Geocodificacao")
        st.download_button("⬇️ Baixar relatório Excel", data=buffer.getvalue(), file_name="relatorio_roteirizador.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.caption("O botão Google Maps de cada endereço abre a busca desse destino. Trechos sem resposta OSRM usam Haversine × fator rodoviário e tempo estimado de 25 km/h; esses valores são aproximações, não distâncias reais de condução.")
