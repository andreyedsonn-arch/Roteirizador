# 📍 Roteirizador de Entregas

Um sistema em Python desenvolvido para automatizar, otimizar e roteirizar entregas por múltiplas rotas candidatas, contando com verificação e conferência manual de endereços.

---

## 🚀 Sobre o Projeto

O **Roteirizador de Entregas** é uma ferramenta focada em logística urbana que transforma uma lista de endereços em rotas otimizadas. O sistema realiza a busca gratuita de coordenadas, valida os endereços e calcula as distâncias rodoviárias para organizar a sequência de entregas de forma eficiente.

## 🛠️ Tecnologias Utilizadas

O projeto foi construído utilizando as seguintes tecnologias e bibliotecas:

*   **Python** — Linguagem base do projeto.
*   **Streamlit** — Framework utilizado para a construção da interface web interativa.
*   **Pandas** — Manipulação e estruturação dos dados de entrada e saída.
*   **OpenStreetMap (OSRM & Photon)** — APIs utilizadas para geocodificação (busca de endereços) e cálculo de rotas rodoviárias reais.

---

## 📦 Como Instalar e Rodar o Projeto

Para executar este projeto localmente na sua máquina, siga os passos abaixo:

### Pré-requisitos
Certifique-se de ter o **Python 3.x** instalado no seu sistema.

### 1. Clonar o repositório
```bash
git clone https://github.com/andreyedsonn-arch/Roteirizador.git
cd Roteirizador
```

### 2. Instalar as dependências
Instale todas as bibliotecas necessárias listadas no arquivo `requirements.txt`:
```bash
pip install -r requirements.txt
```

### 3. Executar a aplicação

Para abrir a interface do sistema no seu navegador, você tem duas opções:

*   **Opção 1 (Mais fácil):** Basta ir até a pasta do projeto e dar dois cliques no arquivo **`Iniciar_Roteirizador.bat`**.
*   **Opção 2 (Via terminal):** Se preferir rodar manualmente pelo terminal, execute o comando:
    ```bash
    streamlit run app.py
    ```


---

## 🗺️ Funcionalidades Principais

*   **Busca de Endereços:** Integração com serviços de mapas para localização exata de coordenadas.
*   **Otimização Global:** Algoritmo para redução de distância e tempo total de rodagem.
*   **Conferência Manual:** Interface amigável para o operador validar e ajustar os pontos de entrega.
*   **Suporte a Arquivos:** Integração com planilhas para carga rápida de dados de rotas.
