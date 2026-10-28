# Agente de revisão de conclusão de atendimentos (CEOSoftware)

Contexto para o Claude Code. Responder em português, tom informal, direto ao ponto.

## O que é
Agente que lê o banco `BDCEOSoftware` (SQL Server) e **sugere** se cada atendimento enviado para
revisão pode ser concluído, com resumo, justificativa e CQS. **Nunca altera o ERP**: só leitura.
Painel Streamlit para os gerentes revisarem as sugestões (a IA aprende com essas revisões).

## Ambiente
- Windows, Python 3.14: `C:\Users\alex\AppData\Local\Python\pythoncore-3.14-64\python.exe`
- Pasta: `C:\Users\alex\Documents\Python\agente_conclusao` (PowerShell: `& "caminho" ...`, `$env:VAR=...`)
- Painel: `python -m streamlit run dashboard.py --server.port 8502`
- Agente contínuo (agenda do ini): `python agente.py` · um ciclo: `--uma-vez [--max N] [--usuario X] [--manual]`
- Serviço (NSSM) só depois do período de testes; rodar com usuário do domínio e trocar `M:\` por UNC no ini.

## Regras de negócio
- **Fila**: `Atendimento.Situacao = 0` e a **última** iteração (maior `NumIteracao` por
  `CodEmpresa + NumAtendimento + Desdobramento`) tem `CodTipoIteracao = 26`. Sempre respeitar o desdobramento.
- **Pendência em qualquer fonte impede concluir**: texto, imagens (OCR), áudios (transcrição) e fichas de visita.
  Salvaguarda no código: "Sim" com pendências listadas vira "Não".
- **Contato da interação** (`AtendimentoIteracao.NomeContato`): cada linha do histórico vai para a IA com
  "(contato: cliente | o próprio analista | usuário interno | não informado)", nunca com o nome (LGPD).
  Analista = autor da interação ou analista do atendimento, pelo login ("Vinicius.Souza", "Vinicius S"); só o 1º nome
  ("Vinicius" com login "Vinicius.Souza") = CLIENTE (regra do gerente, caso 550 de 27/10; antes contava como analista;
  simulação nas 437 decisões desde 01/10: libera 13 travas, 6 "Sim" que o gerente concluiu e 7 sem efeito).
  Exceção: nota interna de OUTRO usuário com o 1º nome dele como contato continua interna (`primeiro_nome=True` em
  `_indice_conclusao`). Interno = login exato de outro usuário, só se tiver ponto ou nome completo (30 logins de uma
  palavra, ex. "ramon", coincidem com nomes de clientes). Com `[agente] exigir_contato_cliente = sim`, a interação de
  conclusão com contato que não é o cliente = "Não" forçado no código + pendência "Contato: ...". Conclusão com o
  cliente = a própria 26 quando traz o resumo da conclusão (texto além do fecho "Atendimento enviado para
  revisão/finalizado", `resumo_no_pedido`) com contato do cliente, OU a última antes da 26 que não seja vazia, nem só
  "enviado para revisão", nem nota interna de OUTRO usuário (ex.: devolução do revisor). A 26 entrou em 20/10: os
  analistas escrevem a conclusão nela; simulação nas 281 decisões do painel liberou 31 travas, 28 certas.
  Obs.: o revisor humano concluía 95% desses casos (regra nova), então o "acerto real" cai nesses.
  Testado e NÃO aplicado (28/10): o contato da 26 com resumo decidir sozinho (o gerente barrou 506, 545, 558, mas
  travaria 23 "Sim" desde 01/10 e ele tinha concluído 20; desde 23/10, 3 a 3); 26 só com o fecho e contato do cliente
  liberar a trava (490; 5 de 5 certos) e pós-venda encerrado "conforme alinhamento com o supervisor" concluir com 54
  (517, 522; no ERP os 3 de 23/10 foram concluídos com 54): o gerente não quis.
- **Cliente dispensou o suporte** (regra do gerente, 22/10): registro de que o cliente não precisa ou não quer mais o
  suporte ("cliente informou que não precisa mais de suporte", "desistiu") = pode concluir com **CQS 54**, mesmo que o
  analista tenha analisado/orientado antes (`[cqs] regras`) e mesmo sem o cliente como contato da conclusão:
  `cliente_dispensou()` (regex `_DISPENSA` na interação de conclusão ou no pedido de revisão) libera a trava de
  contato, e no print a fala do cliente "não precisamos mais"/"desisti"/"pode encerrar" conta em `confirma_resultado`.
  Teste com os 8 casos do painel que citam isso: todos com "não precisa mais de suporte" saíram Sim 54 (antes 344, 412
  e 466 davam Não); 464 e 470 têm o mesmo registro e tinham sido devolvidos (rótulo antigo, antes da regra).
- **Pedido de revisão (26)**: o prompt manda ler o texto dela como RESUMO DA CONCLUSÃO (o que foi feito e o resultado);
  antes mandava ignorá-la ("avalie o que veio antes"), o que causou 5 dos 6 erros de 19/10.
- **Confirmação** (`_CONF_VALE`): vale (a) o OK do cliente em texto/áudio/print; (b) o analista registrar que o CLIENTE
  obteve o resultado, fez/testou/conseguiu ("o cliente realizou um teste e o sistema iniciou normalmente", "seguindo
  essa sequência a nota fechou"), ou resultado conferido no sistema dele ("nota transmitida com sucesso"); (c) o cliente
  dizer que resolveu sozinho/não precisa mais; (d) DÚVIDA pura respondida de forma clara ("a dúvida do cliente foi
  esclarecida"); (e) monitoramento encerrado porque o erro não voltou ("não foi mais apresentado erro após a
  atualização", 537); (f) ficha de visita/OS assinada pelo cliente (554; ficha da rede com "Assinada" no nome do
  arquivo vai com "(arquivo marcado como assinada)" no cabeçalho, sem o nome). Ressalva leve do cliente também vale
  ("aparentemente resolveu sim", "a princípio deu tudo certo": 485, 28/10). Exceção: PEDIDO NEGADO (`_CONF_PEDIDO_NEGADO`, item próprio no prompt + 1º critério do
  `[agente] criterio_conclusao`): pedido do cliente cuja resposta foi "não é possível"/"terá de ser manual"/RDM de
  orçamento só conclui com o aceite dele depois da resposta (fala dele, ou o analista registrar que aceitou/confirmou,
  inclusive em ligação ou reunião). Motivo do gerente no caso 437 (22/10); escondida dentro do (d) a IA ignorava,
  precisou de item próprio com exemplo. Não vale: só a ação do analista, mesmo "com êxito" ("cadastro realizado com êxito"), nem resultado sem
  dizer quem verificou ("passou a ser exibido corretamente") em PROBLEMA do cliente (critério tirado das revisões de
  19/10, confirmadas no ERP). Em QUALQUER interação, inclusive no resumo da 26; sem exigir frase "explícita" do cliente
  (idem em `[agente] criterio_conclusao`). OCR lê as ÚLTIMAS `max_imagens_por_atendimento` imagens (a confirmação
  costuma estar nos últimos prints).
- **Print de conversa do WhatsApp** (`privacidade.LeitorOCR.ler_print`, 21/10): o analista anexa o print com o aceite
  do cliente. Detectado pelas cores (fundo `#F5F1EB`/`#EFEAE2`..., balão enviado `#D9FDD3`..., modo escuro também);
  `image_to_data` dá a caixa de cada linha e a cor do balão decide quem falou: verde = ANALISTA (direita); branco com o
  fundo da conversa ao lado e começando à ESQUERDA da área da conversa = CLIENTE. Sai na ordem da conversa:
  `[Imagem N (conversa de WhatsApp lida por OCR): CLIENTE: "..." | ANALISTA: "..."]` (hora, marca de leitura, "Hoje",
  "Digite uma mensagem" removidos; passou de `max_chars` fica o FIM; texto que não é balão, ex. tela do sistema no
  mesmo print, vai em "outros textos"; print só com falas do analista sai com "(nenhuma fala do CLIENTE foi lida neste
  print)", senão a IA "via" aceite que não existe). Outras imagens: OCR comum. Vale como confirmação (mesmo com o analista como
  contato) a fala do CLIENTE que RESPONDE à solução: resultado ("deu certo", "consegui") ou "ok/obrigado/entendi"
  logo depois da solução COMPLETA; não vale "ok" a aviso, a informação parcial ou a pedido de teste sem resultado,
  nem "aguardando" (critério do gerente, 22/10). Print na interação de conclusão (ou no pedido de revisão) com fala do
  cliente que confirma o resultado ou agradece (`privacidade.confirma_resultado`; "ok" solto não basta) libera a trava
  de contato (`print_do_cliente_na_conclusao`, depois do `montar_historico`). Teste de 22/10 (71 casos do painel com
  print, OCR comum × falas separadas): concordância 77% → 83% nas 3 rodadas com o leitor novo, "Não" indevido
  11 → 5-6, "Sim" indevido 5 → 6-7 (1-5910473 e 1-1566122 erram sempre). RUÍDO: mesmo com temperatura 0, 12 dos 71
  casos mudaram de decisão entre rodadas com prompts quase iguais (casos limítrofes do gpt-4.1-mini): diferença de
  1-2 casos entre rodadas não prova nada; olhar o que se repete em várias rodadas. Liga/desliga:
  `[privacidade] separar_falas_whatsapp` (entra na `versao_prompt`); teste: `testar.py --com-print [--ocr-simples]`
  (casos do painel com print de WhatsApp, escolhidos só pelas cores; `--ocr-simples` = como era). Antes o OCR lia por
  colunas e misturava as falas (casos 287, 291, 293, 353: aceite no print e IA disse Não; 437: print só do analista
  e IA disse Sim).
- **Continuidade em outro atendimento** (`[agente] continuidade_outro_atendimento = sim`): se o assunto que falta
  segue em OUTRO atendimento com o número informado, este pode ser concluído e o CQS sai do que foi feito até a
  transferência. A IA devolve `atendimento_continuidade`; o código confere (outro número, escrito no histórico, existe
  no ERP) senão "Não". Dispensa confirmação/contato do cliente; RDM pendente vinculada a ESTE continua travando.
  Fica em `avaliacoes.continuidade` ("" = conferido sem continuidade; NULL = avaliado antes da regra → "Não" pendente
  que cita outro atendimento é reavaliado uma vez). O revisor humano já concluía esses casos (8 de 9 desde 04/2026).
- **RDM vinculada** (`RDM.CodEmpresa + NumAtendimento + DesdobramentoAtendimento`, query `[queries] rdms`): só
  conclui se TODAS estiverem em `[rdm] finalizadas` (3 = concluída, 4 = cancelada). Outra situação = "Não" forçado
  no código + pendência "RDM: ...". RDM citada no texto e vinculada a outro atendimento não conta.
  A situação vai para a IA (seção "RDMs VINCULADAS") e fica em `avaliacoes.rdms`; mudou → reavalia.
- **Registro de atualização** (definição da empresa): texto com os 4 dados obrigatórios, em qualquer ordem/formato:
  atualização (completa ou parcial), data, analista que fez e tipo (remota ou presencial). Ex.: "Ticket N -
  Atualização: Completa - Data: dd/mm/aaaa - Analista: X [/ Y] - Remoto" ou "Atualização Completa - Remota - Data ..
  - Analista Angela". Faltou um dado = não é registro (`registros_atualizacao()`).
- **Só registro de atualização** (`[agente] concluir_so_atualizacao = sim`, `classificar_so_atualizacao()`): todas as
  interações antes da 26 têm registro. 'puro' (nada além dos dados + "Ticket N", "Controle de atualização", "do
  sistema", "Atendimento finalizado") → "Sim" SEM chamar a IA: CQS por `[cqs] so_atualizacao` (remoto 65; presencial
  60 dentro de JF / 59 fora, pela cidade; sem cidade = revisor escolhe) e resumo do próprio registro. 'extra' (OBS,
  motivo, imagem) → IA, "Sim" salvo pendência real no texto a mais (falta de confirmação nunca conta). Contato,
  confirmação e 2ª revisão não se aplicam; RDM pendente continua travando. CQS da atualização vira o principal (sem
  secundário repetido) e precisa bater com a modalidade (`cqs_combina_modalidade`). `avaliacoes.so_atualizacao`
  1 = puro, 2 = extra, 0 = não (NULL = antes da regra → "Não" pendente é refeito uma vez). Histórico: 44 puro + 5 extra.
- **CQS**: principal só quando "Sim"; secundário por registro de atualização (analista do registro; repetido no texto
  do pedido de revisão conta uma vez). Listas em `[cqs] principal/secundario` (63 excluído do catálogo em 16/10;
  59 = Visita fora de JF, 60 = Visita dentro de JF, 65 = Atualização remota completa ou parcial). `[cqs] regras` só
  com o que é da empresa (54 só sem auxílio ou cliente que já resolveu sozinho, ex. "ao retornar a ligação, informou
  que já resolveu" (526), e conseguir DEPOIS da orientação não é resolver sozinho (546); analisou/orientou/mostrou/
  testou = 55+; atualização de módulo = 55+ (560); 65 como principal só se o atendimento foi apenas a atualização, com
  suporte + atualização o principal é o do suporte e o 65 fica no secundário (486); 60/59 só com visita presencial,
  remoto nunca é visita) — sem repetir o catálogo.
  Cidade do cliente (`Clientes.Cidade/Estado` na query candidatos) vai para a IA só como "Local do cliente: dentro/fora
  de Juiz de Fora" (`[cqs] cidade_local`). Catálogo mudou → só o CQS dos "Sim" pendentes é refeito com prompt curto
  (`reclassificar_cqs`/`refazer_so_cqs`, análise mantida). Exemplos de CQS valem enquanto os códigos existirem
  (`codigos_vigentes`), não pela assinatura do catálogo.
- **2ª revisão desligada** (`[agente] segunda_revisao = nao`, 16/10): em produção mudou 1 em ~256 "Sim", nos testes
  errou 12 de 17 mudanças e custava ~40% dos tokens (gpt-4.1). Comparar: `testar.py --com-revisao`.
- **Temperatura** (`[agente] temperatura = 0`, 16/10): respostas estáveis (com o padrão 1,0, 21 de 90 casos mudavam
  de decisão entre rodadas iguais). Vazio = padrão do provedor. Modelo que recusa o parâmetro (os de raciocínio) é
  chamado sem ele (`llm._SEM_TEMPERATURA`). Entra na `versao_prompt`.
- **Decisão única** `agente.decidir()` (IA + travas + CQS), usada pelo ciclo e pelo `testar.py`: o teste mede
  exatamente o que o agente faz.
- **Medição**: `execucoes.tokens_cache` (tokens lidos do cache do provedor, ~60% da entrada), `avaliacoes.versao_prompt`
  (hash do prompt fixo + modelo); acerto real no log e em `historico.py --acerto` por total, 7/30 dias e versão; painel
  mostra o acerto da versão atual (total na ajuda).
- **Revisão separada**: o gerente avalia a **análise** (concluir sim/não → `status`) e o **CQS**
  (`status_cqs`, `cqs_correto`, `motivo_cqs`) separadamente; grava `decidido_por`.
  Exemplos de aprendizado também separados (análise × CQS) em `montar_exemplos`.
- **Aprendizado**: `historico.py` importa do ERP cada iteração 26 já decidida (próxima iteração
  "Conclusão revisada." = concluído + CQS de `CQSRegistros`/`CQSRegistrosAtendimento`; senão devolvido, com o texto
  do revisor como motivo) para a tabela `historico_revisoes`. `similares.py` (TF-IDF local) escolhe, por atendimento,
  os exemplos mais parecidos (revisões do painel + histórico). `python historico.py --acerto` = concordância com o ERP (o revisor às vezes erra; IA está conservadora de propósito).
  `python testar.py` = roda a decisão do agente num conjunto FIXO de casos reais para comparar antes x depois:
  `--painel` (casos revisados pelo gerente, verdade = decisão dele; 90 = 30 Não rejeitados + 30 Não aceitos + 30 Sim),
  `--repetir RODADA` (mesmos casos), `--regras ARQUIVO` (proposta sem gravar), `--listar` (sem IA),
  `--comparar A B` (antes x depois, sem IA), `--modelo X`, `--sem-regras`, `--com-revisao`, `--com-print`
  (casos com print de WhatsApp), `--ocr-simples` (prints sem separar as falas), `--ids 437,386` (só essas avaliações).
  `python regras.py` = IA sintetiza as devoluções reais ("Não concluir quando...") e os "Não" do agente que o gerente
  rejeitou ("PODE CONCLUIR: ...") em regras_sugeridas.txt; na tela de regras as "PODE CONCLUIR" vão para a seção
  QUANDO PODE CONCLUIR (o agente apresenta com instrução própria) e "≈ parecida com" avisa regra já aprovada.
  Só regras_aprovadas.txt entra no prompt. Em 13/10 entraram 28 regras quase repetidas (o "Sim" caiu de ~90% para ~30%);
  em 16/10 foram mescladas (41 → 13 regras, 9 KB → 2,7 KB; a versão de 41 está em regras_bkp/). No teste pelo painel
  (90 casos) a concordância foi de 48% para 57%, o CQS de 4/10 para 9/11 e os tokens caíram 21%.
  Histórico ignora não-decisões (reenvio p/ revisão, ticket novo, auditoria do gerente: `[aprendizado] nao_decisao`).
  Regras de CQS da empresa em `[cqs] regras` (ex.: 54 = sem auxílio do analista ao cliente).
- **Motivo estruturado**: na revisão da análise o gerente marca categorias com um clique (`motivos.py`, listas
  diferentes para IA disse Sim/Não, trocáveis em `[painel] motivos_sim/motivos_nao`); grava `avaliacoes.motivo_cat`
  e entra nos exemplos junto com o texto livre. Painel mostra a contagem por motivo.
- **Regras no painel**: `tela_regras.py` edita `regras_aprovadas.txt` (backup em `regras_bkp/` + log de quem alterou),
  marca sugestões de `regras_sugeridas.txt` para adicionar e roda o `regras.py` com a chave do usuário.
- **Indicador "concluídos sem confirmação"** (PDF e e-mail): IA barrou só por falta de confirmação
  (`historico.so_confirmacao`) e o revisor concluiu no ERP mesmo assim. Novos desde o ciclo anterior + total
  dos últimos `[relatorio] dias_sem_confirmacao` dias.
- Migrações do SQLite em `migracoes.py` (PRAGMA user_version). v2: rejeitadas antigas cujo motivo cita
  "CQS" viraram análise correta + CQS errado.

## LGPD
- Só texto mascarado vai para a IA (CPF, CNPJ, chave NF-e, e-mail, telefone, cartão, IP, senha).
- Nomes de usuários/cliente pseudonimizados (`[USUARIO_cod]`, `[CLIENTE]`) e reidentificados localmente.
- Pessoa do cliente registrada como contato (NomeContato classificado como cliente) vira `[CONTATO_CLIENTE_n]` no
  texto, OCR, áudio e fichas (`Pseudonimizador.adicionar_contato`, chamado no `montar_historico`; 28/10). Antes o
  1º nome ia em claro (81 de 193 decisões de 19 a 27/10) ou virava `[USUARIO_n]` quando coincidia com um login (12;
  no 497 a IA leu "[USUARIO_119] informou que a nota foi importada" como usuário interno e negou). Prioridade: nome
  composto de usuário e nome do cliente > 1º nome do contato > 1º nome/login de uma palavra dos usuários. Fica de
  fora: palavra genérica ("Financeiro", `_NAO_E_NOME`), até 2 letras, minúsculo (não troca "clara") e contato com o
  mesmo 1º nome do autor/analista (ambíguo).
- Imagens (PNG do RichText), áudios (.ogg) e fichas (PDF/foto) são lidos **localmente** (Tesseract,
  faster-whisper, pypdfium2); nunca saem do servidor.

## Arquivos
- `agente.py` ciclo, prompt, avaliação, CQS, relatório e e-mail no fim do ciclo; `agendamento.py` dias/horários.
- `dashboard.py` painel (login ERP com `csspValidaSenha` + `PDVGerenteSN=1`, chave de IA por usuário,
  revisão análise/CQS, métricas de acerto separadas, buscar próximos, PDF, e-mail, formulário de agenda/e-mail
  que grava no ini).
- `llm.py` OpenAI/Anthropic/Google · `cripto.py` Fernet (chaves de IA e senha SMTP cifradas no ini;
  `chave_mestra.key` ou env `AGENTE_CHAVE_MESTRA`) · `privacidade.py` máscara/pseudônimo/OCR ·
  `fichas.py` · `audios.py` · `historico.py` · `similares.py` · `relatorio.py` PDF Modelo B · `email_relatorio.py` SMTP · `migracoes.py`
- `tela_regras.py` tela de regras no painel · `motivos.py` categorias de motivo da revisão ·
  `rdm.py` situação das RDMs vinculadas (assinatura, pendências, texto para IA e painel)
- `tema_login.py` tema da tela de login (Modelo C claro) + `assets/login_fundo.(svg|png|jpg|webp)`
  (o primeiro que existir, nesta ordem).
- `config.ini` (fora do git) · `config.ini.example` modelo.

## Config (ini)
`[banco]` `[agente]` (max_por_ciclo, exemplos_por_tipo, usuario_servico...) `[queries]` `[cqs]`
`[privacidade]` `[fichas]` `[audios]` `[agendamento]` (modo horario/intervalo, dias seg-sab, horarios 19:00)
`[email]` (Locaweb, suporte@ceosoftware.com.br, porta 587, destinatarios, não envia ciclo vazio)
`[relatorio]` (dias_sem_confirmacao) `[rdm]` (verificar, finalizadas) `[painel]` (motivos) `[pastas_clientes]` `[saida]` `[chaves_api]`.

## Nunca
- Commitar `config.ini`, `chave_mestra.key`, `avaliacoes.db`, `relatorios/`.
- Colar senhas/chaves no chat.
- Usar imagens de banco de imagens sem licença (ex.: Depositphotos) no painel.

## Pendências / ideias
- Trava para não rodar dois ciclos ao mesmo tempo (painel + agenda).
- Tirar o preenchimento do último usuário no login (arquivo comum a todos) quando for multiusuário.
- Futuro: automação em etapas (sugerir → devolver ao analista → concluir com filtro), via procedure do ERP,
  usuário "Agente IA", chave `modo` no ini; medir acerto comparando com o que aconteceu no ERP.
