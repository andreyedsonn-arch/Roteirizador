# Orbiway
### Sistema Inteligente de Roteirização de Entregas

O **Orbiway** é um sistema de roteirização logística desenvolvido em Python e Streamlit, voltado à organização e otimização de entregas urbanas.

A plataforma transforma listas de endereços em rotas otimizadas, utilizando serviços de geolocalização, cálculo de trajetos rodoviários e conferência manual de endereços.

Seu objetivo é tornar o planejamento das entregas mais eficiente, reduzir atividades manuais e oferecer maior controle sobre a operação logística.

---

## 🚀 Sobre o Projeto

O Orbiway foi desenvolvido para apoiar o processo de planejamento e execução de entregas.

O sistema realiza a busca de coordenadas geográficas, valida os endereços informados e calcula distâncias rodoviárias para organizar a sequência de entregas de maneira eficiente.

A ferramenta combina automação e conferência manual, permitindo que o operador acompanhe e valide as informações utilizadas na roteirização.

## 🛠️ Tecnologias Utilizadas

O projeto utiliza as seguintes tecnologias, bibliotecas e serviços:

- **Python** — Linguagem principal do projeto.
- **Streamlit** — Framework utilizado na construção da interface web interativa.
- **Pandas** — Manipulação e estruturação dos dados de entrada e saída.
- **SQLite** — Armazenamento local e cache de informações.
- **OpenStreetMap** — Base de dados geográficos.
- **Nominatim** — Serviço de geocodificação para busca de endereços e obtenção de coordenadas.
- **Photon** — Serviço complementar de busca e geocodificação de endereços.
- **OSRM** — Serviço utilizado no cálculo de rotas e distâncias rodoviárias.
---

## 🗺️ Funcionalidades Principais

- **Busca de Endereços:** localização de endereços e obtenção de coordenadas geográficas.
- **Otimização de Rotas:** organização das entregas buscando reduzir a distância e o tempo total de deslocamento.
- **Conferência Manual:** possibilidade de validar e ajustar os pontos de entrega.
- **Integração com Planilhas:** importação de arquivos para facilitar a carga dos dados de entrega.
- **Cálculo de Distâncias:** utilização de trajetos rodoviários para apoiar o planejamento das rotas.

---

## 📦 Como Instalar e Executar

Para executar o projeto localmente, siga os passos abaixo.

### Pré-requisitos

- Python 3.x instalado na máquina.
- Git instalado para clonar o repositório.

### 1. Clonar o repositório

```bash
git clone https://github.com/andreyedsonn-arch/Roteirizador.git
cd Roteirizador
```

### 2. Instalar as dependências

Instale as bibliotecas necessárias utilizando o arquivo `requirements.txt`:

```bash
pip install -r requirements.txt
```

### 3. Executar a aplicação

Existem duas maneiras de iniciar o sistema:

**Opção 1 — Inicialização pelo Windows**

Execute o arquivo `Iniciar_Roteirizador.bat`, localizado na pasta do projeto.

**Opção 2 — Pelo terminal**

Execute o comando:

```bash
streamlit run app.py
```

Após a inicialização, a aplicação poderá ser acessada pelo navegador.

---

## 📈 Evolução do Projeto

O Orbiway está em evolução contínua, com melhorias voltadas à eficiência operacional, experiência de utilização e confiabilidade do processo de roteirização.

As atualizações são orientadas pelas necessidades da operação logística e pela busca de soluções práticas para os desafios do planejamento de entregas.

---

**Orbiway**  
*Tecnologia aplicada à eficiência logística.*