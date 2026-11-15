#!/usr/bin/env python3
"""
Comentador para desafios do Instagram ("quem comentar mais ganha").

Inspirado em Fytex/Instagram-Giveaways-Winner (licença MIT), mas reescrito
de raiz com Playwright. Diferenças principais:
  * NÃO faz login por ti nem guarda a tua palavra-passe: abres o navegador,
    fazes login à mão e a sessão fica guardada numa pasta de perfil local.
  * Só comenta numa publicação, com pausas aleatórias entre comentários
    (mínimo de 20 s) e um limite máximo por sessão.
  * Para sozinho se o Instagram mostrar um aviso de bloqueio/restrição ou se
    dois comentários seguidos falharem.
  * Regista cada tentativa em registo.csv.

Uso rápido:
    python comentador.py --post https://www.instagram.com/p/XXXXXXXX/
"""

from __future__ import annotations

import argparse
import csv
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

INTERVALO_MINIMO_ABSOLUTO = 20  # segundos; não é possível configurar abaixo disto

# Textos (PT/EN) que o Instagram mostra quando restringe a conta.
PADRAO_BLOQUEIO = re.compile(
    r"try again later|we restrict certain activity|action blocked|"
    r"tente novamente mais tarde|restringimos determinadas atividades|"
    r"ação bloqueada|acao bloqueada|limitamos a frequência|we limit how often",
    re.IGNORECASE,
)
PADRAO_BOTAO_PUBLICAR = re.compile(r"^\s*(post|publicar)\s*$", re.IGNORECASE)
# A caixa de comentário pode ser um <textarea> (publicações) ou um campo
# editável <div contenteditable role=textbox> (reels e versões novas do site).
# Os aria-label variam com o idioma ("Adicione/Adiciona um comentário…",
# "Add a comment…"), por isso procuramos por "coment"/"comment".
SELETOR_CAIXA = (
    'textarea[aria-label*="coment" i], textarea[placeholder*="coment" i], '
    'textarea[aria-label*="comment" i], textarea[placeholder*="comment" i], '
    '[contenteditable="true"][aria-label*="coment" i], '
    '[contenteditable="true"][aria-label*="comment" i], '
    '[contenteditable="true"][aria-placeholder*="coment" i], '
    '[contenteditable="true"][aria-placeholder*="comment" i], '
    "form textarea, form [contenteditable=\"true\"]"
)
SELETOR_CAIXA_VISIVEL = f"{SELETOR_CAIXA} >> visible=true"
# Ícone/botão que abre o painel de comentários (sobretudo em reels).
SELETOR_ABRIR_COMENTARIOS = (
    'svg[aria-label="Comentar"], svg[aria-label="Comment"], '
    'svg[aria-label="Comentários"], svg[aria-label="Comments"]'
)


# --------------------------------------------------------------------------- #
# Comentários
# --------------------------------------------------------------------------- #
def carregar_comentarios(caminho: Path) -> list[str]:
    if not caminho.exists():
        sys.exit(f"Ficheiro de comentários não encontrado: {caminho}")
    linhas = [l.strip() for l in caminho.read_text(encoding="utf-8").splitlines()]
    comentarios = [l for l in linhas if l and not l.startswith("#")]
    if not comentarios:
        sys.exit(f"O ficheiro {caminho} não tem comentários (uma linha por comentário).")
    return comentarios


def gerador_comentarios(comentarios: list[str]):
    """Percorre a lista baralhada, sem repetir o mesmo comentário duas vezes seguidas."""
    ultimo = None
    while True:
        ordem = comentarios[:]
        random.shuffle(ordem)
        if len(ordem) > 1 and ordem[0] == ultimo:
            ordem[0], ordem[-1] = ordem[-1], ordem[0]
        for c in ordem:
            yield c
            ultimo = c


# --------------------------------------------------------------------------- #
# Interação com a página
# --------------------------------------------------------------------------- #
def caixa_comentario(page: Page):
    return page.locator(SELETOR_CAIXA_VISIVEL).first


def eh_editavel(caixa) -> bool:
    """True se a caixa for um <div contenteditable> em vez de <textarea>."""
    return caixa.evaluate("el => el.tagName !== 'TEXTAREA'")


def texto_da_caixa(caixa) -> str:
    if eh_editavel(caixa):
        return caixa.inner_text(timeout=1000).strip()
    return caixa.input_value(timeout=1000)


def encontrar_caixa(page: Page, timeout_ms: int):
    """Procura a caixa; se não estiver visível, tenta abrir o painel de comentários."""
    caixa = caixa_comentario(page)
    try:
        caixa.wait_for(state="visible", timeout=timeout_ms // 2)
        return caixa
    except PWTimeout:
        pass
    icones = page.locator(f"{SELETOR_ABRIR_COMENTARIOS} >> visible=true")
    if icones.count() > 0:
        try:
            icones.first.click(timeout=3000)
        except PWTimeout:
            pass
    # Às vezes é preciso clicar no texto "Adicione um comentário…" para o
    # campo aparecer.
    marcador = page.get_by_text(re.compile(r"^(adicion\w* um coment|add a comment)", re.I))
    if marcador.count() > 0:
        try:
            marcador.first.click(timeout=3000)
        except PWTimeout:
            pass
    try:
        caixa.wait_for(state="visible", timeout=timeout_ms // 2)
        return caixa
    except PWTimeout:
        return None


def guardar_diagnostico(page: Page, pasta: Path = Path("diagnostico")) -> Path:
    """Guarda captura de ecrã e lista de campos da página para perceber falhas."""
    pasta.mkdir(exist_ok=True)
    carimbo = datetime.now().strftime("%Y%m%d-%H%M%S")
    page.screenshot(path=str(pasta / f"ecra-{carimbo}.png"), full_page=False)
    campos = page.evaluate("""() => [...document.querySelectorAll(
        'textarea, [contenteditable], [role=textbox], input[type=text]')].map(e => ({
            tag: e.tagName, role: e.getAttribute('role'),
            aria: e.getAttribute('aria-label'), placeholder: e.getAttribute('placeholder')
                  || e.getAttribute('aria-placeholder'),
            editavel: e.getAttribute('contenteditable'),
            visivel: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)
        }))""")
    (pasta / f"campos-{carimbo}.txt").write_text(
        f"URL: {page.url}\n" + ("\n".join(map(str, campos)) or "(nenhum campo)"), encoding="utf-8")
    return pasta



def ha_bloqueio(page: Page) -> str | None:
    """Devolve o texto do aviso se houver um diálogo/aviso de restrição visível."""
    # Só olha para janelas/avisos, não para a página toda: um comentário de
    # outra pessoa com "tente novamente mais tarde" não deve parar o bot.
    avisos = page.locator("div[role=dialog], div[role=alertdialog], [role=alert]")
    for i in range(avisos.count()):
        try:
            texto = avisos.nth(i).inner_text(timeout=2000)
        except PWTimeout:
            continue
        m = PADRAO_BLOQUEIO.search(texto)
        if m:
            return m.group(0)
    return None


def publicar(page: Page, texto: str, simular: bool = False, timeout_ms: int = 20000) -> str:
    """
    Escreve e publica um comentário.
    Devolve: "ok", "simulado", "bloqueado", "sem_caixa" ou "falhou".
    """
    caixa = encontrar_caixa(page, timeout_ms)
    if caixa is None:
        return "sem_caixa"

    try:
        caixa.click(timeout=timeout_ms)
        caixa.fill(texto, timeout=timeout_ms)
    except PWTimeout:
        return "falhou"

    if simular:
        caixa.fill("", timeout=timeout_ms)
        return "simulado"

    # O botão "Publicar" pode estar num <form> (publicações) ou não (reels).
    botao = page.get_by_role("button", name=PADRAO_BOTAO_PUBLICAR).locator("visible=true")
    if botao.count() == 0:
        botao = page.locator("div[role=button], button").filter(
            has_text=PADRAO_BOTAO_PUBLICAR).locator("visible=true")
    if botao.count() > 0:
        botao.first.click()
    else:
        caixa.press("Enter")

    # Sucesso = a caixa fica vazia depois de publicar.
    limite = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < limite:
        if ha_bloqueio(page):
            return "bloqueado"
        try:
            if texto_da_caixa(caixa) == "":
                return "ok"
        except PWTimeout:
            pass
        page.wait_for_timeout(500)
    return "bloqueado" if ha_bloqueio(page) else "falhou"


# --------------------------------------------------------------------------- #
# Registo
# --------------------------------------------------------------------------- #
def registar(caminho: Path, comentario: str, estado: str) -> None:
    novo = not caminho.exists()
    with caminho.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if novo:
            w.writerow(["data_hora", "estado", "comentario"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), estado, comentario])


def esperar(segundos: float) -> None:
    fim = time.monotonic() + segundos
    while (resta := fim - time.monotonic()) > 0:
        print(f"\r  próximo comentário em {int(resta):>4d} s  (Ctrl+C para parar)", end="", flush=True)
        time.sleep(min(1, resta))
    print("\r" + " " * 60 + "\r", end="")


# --------------------------------------------------------------------------- #
# Programa principal
# --------------------------------------------------------------------------- #
def argumentos() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Comenta automaticamente numa publicação do Instagram, com pausas.")
    p.add_argument("--post", required=True, help="URL da publicação (https://www.instagram.com/p/...)")
    p.add_argument("--comentarios", default="comentarios.txt", help="ficheiro com um comentário por linha")
    p.add_argument("--intervalo-min", type=int, default=20, help="pausa mínima entre comentários, em segundos (>= 20)")
    p.add_argument("--intervalo-max", type=int, default=20, help="pausa máxima entre comentários, em segundos")
    p.add_argument("--max", type=int, default=5000, help="número máximo de comentários nesta sessão")
    p.add_argument("--perfil", default="perfil-navegador", help="pasta onde a sessão do navegador fica guardada")
    p.add_argument("--registo", default="registo.csv", help="ficheiro CSV com o histórico")
    p.add_argument("--simular", action="store_true", help="escreve o comentário mas não publica (para testar)")
    p.add_argument("--chrome", help="caminho para um Chrome/Chromium já instalado (opcional)")
    p.add_argument("--dispositivo", default="Pixel 7",
                   help='telemóvel a simular (o Instagram só mostra a caixa de comentário na '
                        'versão mobile). Ex.: "Pixel 7", "Galaxy S9+", "iPhone 13"')
    p.add_argument("--desktop", action="store_true", help="abre a versão desktop em vez da mobile")
    a = p.parse_args()

    if a.intervalo_min < INTERVALO_MINIMO_ABSOLUTO:
        p.error(f"--intervalo-min tem de ser pelo menos {INTERVALO_MINIMO_ABSOLUTO} segundos.")
    if a.intervalo_max < a.intervalo_min:
        p.error("--intervalo-max não pode ser menor que --intervalo-min.")
    if a.max < 1:
        p.error("--max tem de ser pelo menos 1.")
    return a


def main() -> None:
    a = argumentos()
    comentarios = carregar_comentarios(Path(a.comentarios))
    proximo = gerador_comentarios(comentarios)
    registo = Path(a.registo)

    with sync_playwright() as pw:
        if a.desktop:
            opcoes = dict(headless=False, viewport={"width": 1200, "height": 900})
        else:
            # Versão mobile: o mesmo que o "modo telemóvel" das ferramentas de
            # programador do Chrome (user agent, ecrã e toque de telemóvel).
            if a.dispositivo not in pw.devices:
                sys.exit(f'Dispositivo desconhecido: "{a.dispositivo}". Experimenta "Pixel 7".')
            opcoes = {k: v for k, v in pw.devices[a.dispositivo].items() if k != "default_browser_type"}
            opcoes["headless"] = False
        if a.chrome:
            opcoes["executable_path"] = a.chrome
        ctx = pw.chromium.launch_persistent_context(a.perfil, **opcoes)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(a.post, wait_until="domcontentloaded")

        print("\n1) No navegador que abriu, faz login no Instagram tu mesmo (se ainda não estiveres).")
        print("2) Confirma que estás na publicação certa e que vês a caixa de comentário.")
        input("3) Depois carrega em Enter aqui para começar... ")

        if a.post.rstrip("/") not in page.url:
            page.goto(a.post, wait_until="domcontentloaded")

        feitos = falhas_seguidas = 0
        try:
            while feitos < a.max:
                aviso = ha_bloqueio(page)
                if aviso:
                    print(f"\n⛔ O Instagram mostrou um aviso de restrição (\"{aviso}\"). A parar.")
                    break

                texto = next(proximo)
                estado = publicar(page, texto, simular=a.simular)
                registar(registo, texto, estado)

                if estado in ("ok", "simulado"):
                    feitos += 1
                    falhas_seguidas = 0
                    print(f"✔ [{feitos}/{a.max}] {estado}: {texto}")
                elif estado == "bloqueado":
                    print(f"\n⛔ Comentário bloqueado pelo Instagram. A parar para proteger a conta.")
                    break
                else:
                    falhas_seguidas += 1
                    print(f"✖ {estado}: {texto}")
                    if estado == "sem_caixa":
                        print("  Não encontrei a caixa de comentário. Estás com login feito e na publicação certa?")
                        pasta = guardar_diagnostico(page)
                        print(f"  Diagnóstico guardado em {pasta.resolve()} (envia-me o .txt e o .png).")
                    if falhas_seguidas >= 2:
                        print("\n⛔ Duas falhas seguidas. A parar.")
                        break
                    page.reload(wait_until="domcontentloaded")

                if feitos < a.max:
                    esperar(random.uniform(a.intervalo_min, a.intervalo_max))
        except KeyboardInterrupt:
            print("\nParado por ti.")
        finally:
            print(f"\nComentários publicados nesta sessão: {feitos}. Histórico em {registo.resolve()}")
            ctx.close()


if __name__ == "__main__":
    main()
