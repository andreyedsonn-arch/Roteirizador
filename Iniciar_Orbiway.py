from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
import tkinter as tk
from tkinter import messagebox

BASE_DIR = Path(__file__).resolve().parent
APP_FILE = BASE_DIR / "app.py"
SPLASH_FILE = BASE_DIR / "assets" / "orbiway_splash.png"
ICON_FILE = BASE_DIR / "assets" / "orbiway.ico"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "inicializacao.log"
TEMPO_MINIMO_SPLASH = 5.0
TEMPO_MAXIMO_INICIALIZACAO = 180


def escolher_porta_livre():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def mostrar_splash():
    janela = tk.Tk()
    janela.overrideredirect(True)
    janela.attributes("-topmost", True)
    try:
        janela.iconbitmap(str(ICON_FILE))
    except Exception:
        pass
    imagem = tk.PhotoImage(file=str(SPLASH_FILE))
    largura, altura = imagem.width(), imagem.height()
    tela_largura, tela_altura = janela.winfo_screenwidth(), janela.winfo_screenheight()
    escala = min(1.0, (tela_largura * 0.90) / largura, (tela_altura * 0.82) / altura)
    if escala < 1.0:
        fator = max(1, int(round(1 / escala)))
        imagem = imagem.subsample(fator, fator)
        largura, altura = imagem.width(), imagem.height()
    x, y = (tela_largura-largura)//2, (tela_altura-altura)//2
    janela.geometry(f"{largura}x{altura}+{x}+{y}")
    painel = tk.Label(janela, image=imagem, borderwidth=0, highlightthickness=0)
    painel.image = imagem
    painel.pack()
    janela.update_idletasks()
    janela.update()
    return janela


def servidor_pronto(processo, janela, url, inicio):
    health = f"{url}/_stcore/health"
    while time.monotonic() - inicio < TEMPO_MAXIMO_INICIALIZACAO:
        try:
            with urllib.request.urlopen(health, timeout=1.0) as resposta:
                pronto = resposta.status == 200
        except Exception:
            pronto = False
        if processo.poll() is not None:
            return False
        if pronto and time.monotonic() - inicio >= TEMPO_MINIMO_SPLASH:
            return True
        try:
            janela.update()
        except tk.TclError:
            return False
        time.sleep(0.2)
    return False


def main():
    if not APP_FILE.is_file():
        messagebox.showerror("Orbiway", f"Não encontrei o aplicativo atualizado:\n{APP_FILE}")
        return
    if not SPLASH_FILE.is_file() or not ICON_FILE.is_file():
        messagebox.showerror("Orbiway", "Arquivos visuais ausentes. Extraia novamente a pasta completa.")
        return
    LOG_DIR.mkdir(exist_ok=True)
    janela = processo = log = None
    try:
        porta = escolher_porta_livre()
        url = f"http://127.0.0.1:{porta}"
        janela = mostrar_splash()
        log = LOG_FILE.open("a", encoding="utf-8")
        log.write(f"\n--- Orbiway | app.py | porta {porta} ---\n")
        log.flush()
        comando = [
            sys.executable, "-m", "streamlit", "run", str(APP_FILE),
            "--server.headless=true", f"--server.port={porta}",
            "--server.address=127.0.0.1", "--browser.gatherUsageStats=false",
        ]
        processo = subprocess.Popen(
            comando, cwd=str(BASE_DIR), stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if servidor_pronto(processo, janela, url, time.monotonic()):
            janela.destroy()
            janela = None
            webbrowser.open(url)
            processo.wait()
            return
        if processo.poll() is None:
            processo.terminate()
            try:
                processo.wait(timeout=5)
            except subprocess.TimeoutExpired:
                processo.kill()
        if log:
            log.close()
            log = None
        if janela:
            janela.destroy()
            janela = None
        try:
            diagnostico = LOG_FILE.read_text(encoding="utf-8", errors="replace")[-2500:]
        except OSError:
            diagnostico = "Não foi possível ler o arquivo de diagnóstico."
        messagebox.showerror(
            "Orbiway", "O app.py não iniciou no tempo esperado.\n\n"
            f"Consulte o log: {LOG_FILE}\n\n{diagnostico[-1600:]}"
        )
    except Exception as erro:
        if processo is not None and processo.poll() is None:
            processo.terminate()
        if janela is not None:
            try: janela.destroy()
            except tk.TclError: pass
        messagebox.showerror("Orbiway", f"Não foi possível iniciar o app.py.\n\n{erro}")
    finally:
        if log and not log.closed:
            log.close()


if __name__ == "__main__":
    main()
