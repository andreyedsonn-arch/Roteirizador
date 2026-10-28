import io
import difflib
import json
import math
import random
import time
import unicodedata
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
USER_AGENT = "RoteirizadorEntregas/2.0 (uso pessoal; contato: usuario-local)"

st.set_page_config(page_title=APP_TITLE, page_icon="🚚", layout="wide")
st.title("🚚 Roteirizador de Entregas")
st.caption("Busca gratuita de endereços, confirmação manual e roteirização por múltiplas rotas candidatas, otimização global e distâncias rodoviárias.")

st.info(
    "Importante: os geocodificadores e o OSRM usados aqui são serviços públicos gratuitos, "
    "sem garantia de disponibilidade. O aplicativo não descarta entregas: endereços sem "
    "coordenadas ficam pendentes até serem confirmados."
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
    resp = requests.get(
        NOMINATIM_URL,
        params={"q": consulta, "format": "jsonv2", "limit": 1, "countrycodes": "br"},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    resp.raise_for_status()
    dados = resp.json()
    if not dados:
        return None
    return {
        "lat": float(dados[0]["lat"]),
        "lon": float(dados[0]["lon"]),
        "nome": dados[0].get("display_name", consulta),
        "motor": "Nominatim",
    }

def geocodificar_photon(consulta):
    resp = requests.get(
        PHOTON_URL,
        params={"q": consulta, "limit": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    resp.raise_for_status()
    features = resp.json().get("features", [])
    if not features:
        return None
    feature = features[0]
    coords = feature.get("geometry", {}).get("coordinates", [])
    if len(coords) < 2:
        return None
    props = feature.get("properties", {})
    nome = ", ".join(str(props[k]) for k in ("name", "street", "housenumber", "city", "state") if props.get(k))
    return {"lat": float(coords[1]), "lon": float(coords[0]), "nome": nome or consulta, "motor": "Photon"}

def geocodificar(endereco, cidade):
    consultas = montar_consultas(endereco, cidade)
    erros_conexao = []
    for consulta in consultas:
        for motor, funcao in (("Nominatim", geocodificar_nominatim), ("Photon", geocodificar_photon)):
            try:
                resultado = funcao(consulta)
                if resultado:
                    resultado["consulta"] = consulta
                    return resultado, "OK"
            except requests.RequestException as exc:
                erros_conexao.append(f"{motor}: {exc}")
            except (ValueError, KeyError, TypeError) as exc:
                erros_conexao.append(f"{motor}: {exc}")
            # Pausa conservadora entre consultas aos serviços públicos
            time.sleep(1.1 if motor == "Nominatim" else 0.25)
    if erros_conexao and len(erros_conexao) >= len(consultas):
        return None, "Falha de conexão/serviço"
    return None, "Não encontrado automaticamente"

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
        # Nominatim: devolve até 10 candidatos, se existirem correspondências suficientes.
        try:
            resp = requests.get(
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
                timeout=20,
            )
            requisicoes += 1
            resp.raise_for_status()
            dados = resp.json()
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

        # Respeita intervalo conservador entre requisições ao Nominatim público.
        time.sleep(1.1)

        try:
            resp = requests.get(
                PHOTON_URL,
                params={"q": consulta, "limit": 10},
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
            requisicoes += 1
            resp.raise_for_status()
            features = resp.json().get("features", [])
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

def osrm_matriz(coordenadas):
    if len(coordenadas) < 2:
        return [[0.0 for _ in coordenadas] for _ in coordenadas]
    # OSRM público tem limites de tamanho/uso. Falhar explicitamente é melhor
    # do que inventar distâncias ou omitir entregas.
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coordenadas)
    resp = requests.get(
        OSRM_TABLE_URL + coords,
        params={"annotations": "distance"},
        timeout=90,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("distances"):
        raise RuntimeError(f"OSRM não calculou a matriz: {data.get('message', data.get('code', 'resposta inválida'))}")
    return data["distances"]

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
    resp = requests.get(
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
    """Soma as distâncias rodoviárias direcionais da sequência completa."""
    total = 0.0
    for a, b in zip(ordem, ordem[1:]):
        try:
            d = matriz[a][b]
        except (IndexError, TypeError):
            return math.inf
        if d is None or not isinstance(d, (int, float)) or not math.isfinite(d):
            return math.inf
        total += float(d)
    return total

def melhorar_rota_local(ordem, matriz, max_passadas=30):
    """Busca local para reduzir a distância rodoviária total sem mover a origem.

    Testa três operações: inverter segmentos (2-opt), mover uma entrega para
    outra posição (relocate) e trocar duas entregas (swap). Usa distâncias
    direcionais da matriz OSRM, importante para ruas de mão única.
    """
    if matriz is None or len(ordem) < 4:
        return ordem[:]
    melhor = ordem[:]
    custo_melhor = custo_rota(melhor, matriz)
    if not math.isfinite(custo_melhor):
        return melhor

    for _ in range(max_passadas):
        candidata_melhor = melhor
        custo_candidato = custo_melhor
        n = len(melhor)

        # 2-opt: inverter qualquer segmento, mantendo a origem na posição 0.
        for i in range(1, n - 1):
            for j in range(i + 1, n):
                cand = melhor[:i] + list(reversed(melhor[i:j + 1])) + melhor[j + 1:]
                c = custo_rota(cand, matriz)
                if c + 1.0 < custo_candidato:
                    candidata_melhor, custo_candidato = cand, c

        # Relocate: retirar uma entrega e inseri-la em outra posição.
        for i in range(1, n):
            item = melhor[i]
            sem = melhor[:i] + melhor[i + 1:]
            for j in range(1, len(sem) + 1):
                if j == i:
                    continue
                cand = sem[:j] + [item] + sem[j:]
                c = custo_rota(cand, matriz)
                if c + 1.0 < custo_candidato:
                    candidata_melhor, custo_candidato = cand, c

        # Swap: trocar a posição de duas entregas.
        for i in range(1, n - 1):
            for j in range(i + 1, n):
                cand = melhor[:]
                cand[i], cand[j] = cand[j], cand[i]
                c = custo_rota(cand, matriz)
                if c + 1.0 < custo_candidato:
                    candidata_melhor, custo_candidato = cand, c

        if custo_candidato + 1.0 < custo_melhor:
            melhor, custo_melhor = candidata_melhor, custo_candidato
        else:
            break
    return melhor

def construir_vizinho_mais_proximo(coordenadas, matriz, primeiro=None):
    """Constrói uma sequência inicial usando somente distâncias rodoviárias."""
    ordem = [0]
    restantes = set(range(1, len(coordenadas)))
    atual = 0
    if primeiro is not None and primeiro in restantes:
        ordem.append(primeiro)
        restantes.remove(primeiro)
        atual = primeiro
    while restantes:
        destinos = sorted(restantes)
        if matriz is not None:
            distancias = [matriz[atual][i] for i in destinos]
        else:
            distancias = osrm_distancias_de_um_para_varios(coordenadas, atual, destinos)
        opcoes = [(float(d), i) for i, d in zip(destinos, distancias)
                  if d is not None and isinstance(d, (int, float)) and math.isfinite(d)]
        if not opcoes:
            raise RuntimeError(
                "O serviço rodoviário não conseguiu calcular caminhos para as entregas restantes. "
                "Nenhuma entrega foi ordenada por linha reta. Tente novamente mais tarde."
            )
        _, proximo = min(opcoes)
        ordem.append(proximo)
        restantes.remove(proximo)
        atual = proximo
    return ordem

def construir_rota_randomizada(coordenadas, matriz, rng, fator_escolha=3):
    """Constrói uma rota variando entre candidatas próximas, para escapar de uma única sequência gulosa."""
    ordem = [0]
    restantes = set(range(1, len(coordenadas)))
    atual = 0

    while restantes:
        candidatos = sorted(restantes)
        distancias = [matriz[atual][i] for i in candidatos]
        validos = [
            (float(d), i) for i, d in zip(candidatos, distancias)
            if d is not None and isinstance(d, (int, float)) and math.isfinite(d)
        ]
        if not validos:
            raise RuntimeError(
                "O serviço rodoviário não encontrou caminhos válidos para todas as entregas. "
                "Nenhuma rota baseada em linha reta foi criada."
            )
        validos.sort()
        # Escolhe aleatoriamente entre as poucas opções mais próximas.
        # Isso cria rotas iniciais diferentes sem perder o foco na proximidade rodoviária.
        k = min(max(1, fator_escolha), len(validos))
        faixa = validos[:k]
        pesos = [1.0 / (pos + 1) for pos in range(len(faixa))]
        _, proximo = rng.choices(faixa, weights=pesos, k=1)[0]
        ordem.append(proximo)
        restantes.remove(proximo)
        atual = proximo
    return ordem


def melhorar_rota_local(ordem, matriz, max_passadas=40):
    """Busca local com 2-opt, relocate, swap e inserção por melhor posição."""
    if matriz is None or len(ordem) < 4:
        return ordem[:]

    melhor = ordem[:]
    custo_melhor = custo_rota(melhor, matriz)
    if not math.isfinite(custo_melhor):
        return melhor

    for _ in range(max_passadas):
        candidata_melhor = melhor
        custo_candidato = custo_melhor
        n = len(melhor)

        # 2-opt: inverte trechos. A rota é sempre reavaliada com distâncias direcionais.
        for i in range(1, n - 1):
            for j in range(i + 1, n):
                cand = melhor[:i] + list(reversed(melhor[i:j + 1])) + melhor[j + 1:]
                c = custo_rota(cand, matriz)
                if c + 1.0 < custo_candidato:
                    candidata_melhor, custo_candidato = cand, c

        # Relocate: remove uma parada e insere em outro lugar.
        for i in range(1, n):
            item = melhor[i]
            sem = melhor[:i] + melhor[i + 1:]
            for j in range(1, len(sem) + 1):
                cand = sem[:j] + [item] + sem[j:]
                c = custo_rota(cand, matriz)
                if c + 1.0 < custo_candidato:
                    candidata_melhor, custo_candidato = cand, c

        # Swap: troca duas paradas.
        for i in range(1, n - 1):
            for j in range(i + 1, n):
                cand = melhor[:]
                cand[i], cand[j] = cand[j], cand[i]
                c = custo_rota(cand, matriz)
                if c + 1.0 < custo_candidato:
                    candidata_melhor, custo_candidato = cand, c

        if custo_candidato + 1.0 < custo_melhor:
            melhor, custo_melhor = candidata_melhor, custo_candidato
        else:
            break
    return melhor


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
    """Busca uma rota global para evitar avançar a uma região e depois retornar.

    Com matriz rodoviária disponível, cria várias rotas candidatas a partir da
    origem, variando entre as 3-5 próximas paradas por distância de rua. Depois
    aplica busca local (2-opt, relocate e swap) e escolhe a rota aberta de menor
    distância total. A rota não retorna à origem: a última entrega pode ficar
    no extremo mais distante, se isso reduzir o percurso total.
    """
    n = len(coordenadas)
    if n <= 2:
        return list(range(n))

    if matriz is None:
        # Mantém o modo de contingência rodoviária por consultas menores.
        # Nesse modo, a ordem é montada por proximidade de rua, sem fingir que
        # distâncias em linha reta são distâncias de condução.
        grupos = agrupar_entregas_geograficamente(coordenadas, tamanho_grupo=4)
        pendentes = [g[:] for g in grupos]
        ordem = [0]
        atual = 0
        while pendentes:
            def custo_entrada(grupo):
                try:
                    distancias = osrm_distancias_de_um_para_varios(
                        coordenadas, atual, sorted(grupo)
                    )
                    validas = [d for d in distancias if d is not None and math.isfinite(float(d))]
                    if validas:
                        return min(float(d) for d in validas)
                except Exception:
                    pass
                return min(distancia_haversine(coordenadas[atual], coordenadas[i]) for i in grupo)

            grupo = min(pendentes, key=custo_entrada)
            pendentes.remove(grupo)
            bloco = otimizar_ordem_dentro_grupo(grupo, None, coordenadas, atual)
            ordem.extend(bloco)
            atual = ordem[-1]
        if len(ordem) != n or set(ordem[1:]) != set(range(1, n)):
            raise RuntimeError("A rota não preservou todas as entregas; geração cancelada.")
        return ordem

    # Validar a matriz antes de iniciar a busca para evitar rotas com trechos
    # sem distância rodoviária conhecida.
    if len(matriz) != n or any(len(linha) != n for linha in matriz):
        raise RuntimeError("A matriz rodoviária não corresponde à quantidade de pontos.")

    rng = random.Random(20261023)
    melhor_rota = None
    melhor_custo = math.inf
    # Mais de uma construção inicial ajuda a escapar da escolha gulosa que
    # costuma mandar o motorista a um extremo e depois fazê-lo voltar.
    tentativas = min(30, max(12, n))
    for tentativa in range(tentativas):
        ordem = [0]
        restantes = set(range(1, n))
        atual = 0
        while restantes:
            validos = []
            for i in restantes:
                d = matriz[atual][i]
                if d is not None and isinstance(d, (int, float)) and math.isfinite(d):
                    validos.append((float(d), i))
            if not validos:
                raise RuntimeError(
                    "O serviço rodoviário não encontrou caminhos válidos para todas as entregas. "
                    "Nenhuma entrega foi descartada. Tente novamente mais tarde."
                )
            validos.sort()
            # Nas primeiras tentativas, pega o mais próximo; nas demais, faz
            # uma escolha ponderada entre os mais próximos para explorar ordens.
            if tentativa == 0 or len(validos) == 1:
                proximo = validos[0][1]
            else:
                k = min(5, len(validos))
                faixa = validos[:k]
                pesos = [1.0 / ((pos + 1) ** 1.5) for pos in range(k)]
                proximo = rng.choices(faixa, weights=pesos, k=1)[0][1]
            ordem.append(proximo)
            restantes.remove(proximo)
            atual = proximo

        ordem = melhorar_rota_local(ordem, matriz, max_passadas=20)
        custo = custo_rota(ordem, matriz)
        if custo < melhor_custo:
            melhor_rota, melhor_custo = ordem, custo

    if melhor_rota is None or len(melhor_rota) != n or set(melhor_rota[1:]) != set(range(1, n)):
        raise RuntimeError("A otimização não conseguiu incluir todas as entregas; geração cancelada.")
    return melhor_rota

def obter_geometria_rota(coordenadas, ordem):
    """Obtém a geometria rodoviária completa na ordem já calculada."""
    pontos = [coordenadas[i] for i in ordem]
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in pontos)
    resp = requests.get(
        OSRM_ROUTE_URL + coords,
        params={"overview": "full", "geometries": "geojson", "steps": "false"},
        timeout=90,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(data.get("message", "OSRM não retornou a geometria da rota"))
    return data["routes"][0]["geometry"]["coordinates"]


def exibir_mapa_rota(rota_df, origem, geometria=None):
    """Exibe mapa Leaflet com paradas numeradas e linha da rota."""
    pontos = [{"lat": float(origem[0]), "lon": float(origem[1]), "numero": "O", "endereco": "Origem"}]
    for _, row in rota_df.iterrows():
        pontos.append({
            "lat": float(row["Latitude"]),
            "lon": float(row["Longitude"]),
            "numero": str(int(row["Parada"])),
            "endereco": str(row["Endereço"]),
        })
    # GeoJSON do OSRM vem em [longitude, latitude]; Leaflet usa [latitude, longitude].
    if geometria:
        linha = [[float(coord[1]), float(coord[0])] for coord in geometria]
    else:
        linha = [[p["lat"], p["lon"]] for p in pontos]
    pontos_json = json.dumps(pontos, ensure_ascii=False)
    linha_json = json.dumps(linha)
    html = f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<style>html,body,#map{{height:100%;width:100%;margin:0}} .stop-icon{{background:#1769aa;color:white;border:2px solid white;border-radius:50%;width:30px;height:30px;line-height:26px;text-align:center;font-weight:bold;box-shadow:0 1px 5px #555;font-size:13px}} .origin-icon{{background:#16803c;color:white;border:2px solid white;border-radius:50%;width:32px;height:32px;line-height:28px;text-align:center;font-weight:bold;box-shadow:0 1px 5px #555}}</style></head><body><div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const pontos = {pontos_json}; const linha = {linha_json};
const map = L.map('map');
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom:19, attribution:'&copy; OpenStreetMap contributors'}}).addTo(map);
const bounds=[];
pontos.forEach(p => {{ const origem = p.numero === 'O'; const icon = L.divIcon({{className:'',html:`<div class="${{origem?'origin-icon':'stop-icon'}}">${{p.numero}}</div>`,iconSize:[32,32],iconAnchor:[16,16]}}); L.marker([p.lat,p.lon],{{icon}}).addTo(map).bindPopup(`<b>${{origem?'ORIGEM':'Parada '+p.numero}}</b><br>${{p.endereco}}`); bounds.push([p.lat,p.lon]); }});
L.polyline(linha,{{color:'#1769aa',weight:5,opacity:0.8}}).addTo(map);
if(bounds.length) map.fitBounds(bounds,{{padding:[25,25]}});
</script></body></html>'''
    components.html(html, height=560, scrolling=False)


def calcular_trecho(a, b):
    coords = f"{a[1]:.6f},{a[0]:.6f};{b[1]:.6f},{b[0]:.6f}"
    resp = requests.get(OSRM_ROUTE_URL + coords, params={"overview": "false"}, timeout=35, headers={"User-Agent": USER_AGENT})
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
                    if lat == 0.0 and lon == 0.0:
                        st.error("Latitude e longitude não podem ficar ambas em zero.")
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
    st.write("A rota começa pela origem. O sistema testa várias sequências completas, melhora cada uma por trocas e inversões de paradas e escolhe a de menor distância rodoviária total. Assim, uma região extrema próxima das primeiras entregas pode ser atendida antes de avançar para o extremo mais distante, evitando retornos desnecessários. É uma heurística e não garante o ótimo matemático.")
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
                matriz = osrm_matriz(coordenadas)
            except Exception as exc:
                st.warning(f"A matriz rodoviária completa não ficou disponível ({exc}). O aplicativo tentará calcular a proximidade pelas ruas em consultas menores.")
                matriz = None
            try:
                ordem = ordenar_por_proximidade(coordenadas, matriz)
            except Exception as exc:
                st.error(f"Não foi possível calcular uma sequência confiável pelas ruas: {exc}")
                st.stop()
            linhas_rota = []
            total_m = 0.0
            total_s = 0.0
            falhas_trecho = 0
            for etapa, indice in enumerate(ordem[1:], start=1):
                anterior = ordem[etapa - 1]
                km, minutos, motor_trecho = None, None, "Indisponível"
                try:
                    metros, segundos, motor_trecho = calcular_trecho(coordenadas[anterior], coordenadas[indice])
                    if metros is not None:
                        km = metros / 1000
                        minutos = segundos / 60 if segundos is not None else None
                        total_m += metros
                        total_s += segundos or 0
                except Exception:
                    falhas_trecho += 1
                idx_geo = indice - 1
                registro = {
                    "Parada": etapa,
                    "Endereço": geo.iloc[idx_geo]["endereco"],
                    "Linha original": int(geo.iloc[idx_geo]["linha_planilha"]),
                    "Latitude": coordenadas[indice][0],
                    "Longitude": coordenadas[indice][1],
                    "Km desde parada anterior": round(km, 2) if km is not None else None,
                    "Minutos desde parada anterior": round(minutos, 1) if minutos is not None else None,
                    "Google Maps": abrir_google_maps(geo.iloc[idx_geo]["endereco"], geo.iloc[idx_geo]["cidade"]),
                    "Status": "Incluída",
                }
                if coluna_pedido != "(não informar)":
                    registro["Número do pedido"] = geo.iloc[idx_geo].get("numero_pedido", "")
                    # Mantém o número do pedido ao lado da parada/endereço no relatório.
                    registro = {
                        "Parada": registro["Parada"],
                        "Número do pedido": registro["Número do pedido"],
                        **{k: v for k, v in registro.items() if k not in ("Parada", "Número do pedido")},
                    }
                linhas_rota.append(registro)
            rota_df = pd.DataFrame(linhas_rota)
            geometria_mapa = None
            erro_mapa = None
            try:
                geometria_mapa = obter_geometria_rota(coordenadas, ordem)
            except Exception as exc:
                erro_mapa = str(exc)
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
            st.warning(f"{resultado_rota['falhas_trecho']} trecho(s) não tiveram distância/tempo rodoviário retornados pelo serviço.")
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
        st.caption("O botão Google Maps de cada endereço abre a busca desse destino. As distâncias e tempos dependem da disponibilidade do OSRM público.")
