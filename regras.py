"""
Regras de revisão aprendidas das decisões reais:
  - DEVOLUÇÕES do revisor no ERP -> "Não concluir quando..." (o que a empresa considera pendência);
  - "Não" do agente que o GERENTE REJEITOU no painel -> "PODE CONCLUIR: ..." (o que a empresa aceita), para
    equilibrar: só com devoluções as regras ficam cada vez mais rígidas.

  python regras.py                 lê os motivos e grava regras_sugeridas.txt
  python regras.py --usuario alex  usa a chave de IA desse usuário
  python regras.py --modelo gpt-4.1   (opcional) modelo mais forte só para essa síntese

Fluxo: o gerente marca as sugestões na tela de regras; as "PODE CONCLUIR" vão para a seção QUANDO PODE CONCLUIR.
Só regras_aprovadas.txt entra no prompt do agente (e no testar.py).
Os motivos vão para a IA mascarados (LGPD), como no resto do agente.
"""
import argparse

import pyodbc

import agente
import historico as hist
import llm
import motivos

SAIDA = agente.BASE / "regras_sugeridas.txt"

SYSTEM = """Você é auditor de qualidade de um suporte de ERP. Abaixo estão motivos REAIS escritos pelo revisor
ao DEVOLVER atendimentos que o analista pediu para concluir. Extraia deles as regras de revisão da empresa.

Escreva em português, em texto puro (sem markdown), no formato:
TEMA
- Regra objetiva e verificável lendo o histórico do atendimento. (N casos) Ex.: "trecho curto de um motivo"

Regras:
- Agrupe por tema (ex.: Documentação do registro; RDM/correção no sistema; Atualização de versão;
  Orientação e treinamento; Coerência entre registros; Pendências técnicas; Outros).
- Uma regra por linha, começando por "Não concluir quando..." ou "Exigir que...".
- Junte motivos parecidos numa regra só; ordene pelos mais frequentes; ignore motivos vazios ou sem sentido.
- Não invente regra que não esteja nos motivos. No máximo 25 regras."""

SYSTEM_PODE = """Você é auditor de qualidade de um suporte de ERP. Abaixo estão motivos REAIS escritos pelo gerente quando
DISCORDOU de uma IA que tinha dito que o atendimento NÃO podia ser concluído (para ele, podia). Extraia deles as
situações em que a empresa aceita concluir.

Escreva em português, em texto puro (sem markdown), no formato:
PODE CONCLUIR: TEMA
- Pode concluir quando... (regra objetiva e verificável lendo o histórico) (N casos) Ex.: "trecho curto de um motivo"

Regras:
- Todo tema começa com "PODE CONCLUIR: " (ex.: PODE CONCLUIR: Confirmação do cliente; PODE CONCLUIR: Prints e áudios;
  PODE CONCLUIR: Continuidade em outro atendimento; PODE CONCLUIR: Outros).
- Uma regra por linha, começando por "Pode concluir quando...".
- Junte motivos parecidos numa regra só; ordene pelos mais frequentes; ignore motivos vazios ou sem sentido.
- Não invente regra que não esteja nos motivos. No máximo 12 regras."""


def motivos_pode_concluir(lite, prot, maximo=300):
    """Motivos do gerente ao rejeitar um "Não" do agente (categorias marcadas + texto livre), mascarados."""
    rows = lite.execute(
        "SELECT assunto, motivo, motivo_cat FROM avaliacoes WHERE id IN (SELECT MAX(id) FROM avaliacoes "
        "GROUP BY atendimento_id) AND pode_concluir=0 AND status='rejeitada' "
        "AND LENGTH(TRIM(COALESCE(motivo,'') || COALESCE(motivo_cat,''))) > 15 "
        "ORDER BY COALESCE(decidido_em, avaliado_em) DESC LIMIT ?", (maximo,)).fetchall()
    vistos, linhas = set(), []
    for assunto, motivo, cats in rows:
        m = prot.proteger(agente.limpar(motivos.para_exemplo(cats, motivo)))[:400]
        if m.lower()[:120] in vistos:
            continue
        vistos.add(m.lower()[:120])
        linhas.append(f"- [{prot.proteger(agente.limpar(assunto))[:60]}] {m}")
    return linhas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usuario", default=None)
    ap.add_argument("--modelo", default=None)
    ap.add_argument("--max", type=int, default=400, help="máximo de motivos (os mais recentes)")
    a = ap.parse_args()

    cfg = agente.ler_cfg()
    lite = agente.init_sqlite(agente.BASE / cfg["saida"]["sqlite"])
    hist.criar_tabela(lite)
    rows = lite.execute("SELECT assunto, motivo FROM historico_revisoes WHERE desfecho='devolvido' "
                        "AND LENGTH(TRIM(COALESCE(motivo,''))) > 15 ORDER BY data DESC LIMIT ?", (a.max,)).fetchall()
    if not rows:
        print("Sem devoluções no histórico. Rode antes: python historico.py --importar --meses 12")
        return

    conn = pyodbc.connect(cfg["banco"]["conn_string"], timeout=30)
    try:
        usuarios = agente.consultar(conn, cfg["queries"]["usuarios"]) if cfg.has_option("queries", "usuarios") else []
    finally:
        conn.close()
    prot = agente.Protecao(cfg, usuarios).para_atendimento(None)

    vistos, linhas = set(), []
    for assunto, motivo in rows:
        m = prot.proteger(agente.limpar(motivo))[:400]
        chave = m.lower()[:120]
        if chave in vistos:
            continue
        vistos.add(chave)
        linhas.append(f"- [{prot.proteger(agente.limpar(assunto))[:60]}] {m}")
    print(f"{len(linhas)} motivos de devolução distintos enviados para síntese...")

    client = agente.criar_client(cfg, a.usuario)
    modelo = a.modelo or cfg["agente"]["modelo"]
    texto, tin, tout = llm.chamar(cfg["agente"]["provider"], client, modelo, SYSTEM,
                                  "MOTIVOS DE DEVOLUÇÃO:\n" + "\n".join(linhas), max_tokens=3000, json_mode=False,
                                  temperatura=agente.temperatura(cfg))
    # o outro lado: o que o gerente aceita concluir (motivos dos "Não" do agente que ele rejeitou)
    pode = motivos_pode_concluir(lite, prot)
    texto_pode = ""
    if len(pode) >= 3:
        print(f"{len(pode)} motivos de 'Não' rejeitados pelo gerente enviados para síntese...")
        texto_pode, i, o = llm.chamar(cfg["agente"]["provider"], client, modelo, SYSTEM_PODE,
                                      "MOTIVOS DO GERENTE (a IA disse NÃO, ele achou que podia concluir):\n"
                                      + "\n".join(pode), max_tokens=1500, json_mode=False,
                                      temperatura=agente.temperatura(cfg))
        tin, tout = tin + i, tout + o
    cab = (f"# Regras sugeridas a partir de {len(linhas)} devoluções reais"
           + (f" e {len(pode)} 'Não' rejeitados pelo gerente" if texto_pode else "") + f" ({modelo}).\n"
           "# Revise: marque as que valem na tela de regras (as 'PODE CONCLUIR' vão para a seção QUANDO PODE CONCLUIR).\n\n")
    SAIDA.write_text(cab + texto.strip() + ("\n\n" + texto_pode.strip() if texto_pode else "") + "\n", encoding="utf-8")
    print(f"Gravado: {SAIDA}  ({tin:,} / {tout:,} tokens)".replace(",", "."))


if __name__ == "__main__":
    main()
