/* =============================================================
   termo-assinar.js — captura da assinatura manuscrita no tablet.

   Arquivo servido CRU de static/: NÃO passa pelo motor de template, então
   tag de template aqui viraria texto literal e o script quebraria em
   silêncio (nenhum erro no servidor, só no console). Todo dado do servidor
   entra por json_script e é lido uma única vez abaixo — mesmo contrato do
   planta-editor.js (CLAUDE.md, "Telas grandes").

   Sem dependência externa: o canvas 2D do navegador faz tudo. Uma lib
   de assinatura resolveria o mesmo problema em troca de mais um CDN
   num fluxo que precisa funcionar no tablet da empresa, offline do
   mundo e dentro da rede interna.
   ============================================================= */
(function () {
  "use strict";

  var elConfig = document.getElementById("__ta_config");
  if (!elConfig) { return; }

  var CFG = JSON.parse(elConfig.textContent);
  var ENVIAR_URL = CFG.enviarUrl;
  var CONCLUIDO_URL = CFG.concluidoUrl;
  var NOME_SUGERIDO = CFG.nomeSugerido || "";
  var EXPIRA_EM_SEGUNDOS = CFG.expiraEmSegundos || 0;

  // ── Elementos ────────────────────────────────────────────────────────────
  var box = document.getElementById("taSignBox");
  var canvas = document.getElementById("taCanvas");
  var inputNome = document.getElementById("taNome");
  var inputCpf = document.getElementById("taCpf");
  var inputRg = document.getElementById("taRg");
  var chkAceite = document.getElementById("taAceite");
  var btnEnviar = document.getElementById("taEnviar");
  var btnLimpar = document.getElementById("taLimpar");
  var avisoErro = document.getElementById("taErro");
  var contador = document.getElementById("taPrazo");

  if (!canvas || !box) { return; }

  var ctx = canvas.getContext("2d");

  // ── Canvas em resolução de tela ──────────────────────────────────────────
  // O canvas é dimensionado pelo CSS (largura 100%), mas o buffer de pixels
  // precisa acompanhar o devicePixelRatio: sem isso a assinatura sai
  // serrilhada no tablet (que é 2x ou 3x) e a imagem enviada ao servidor é
  // de baixa resolução — e é essa imagem que vai para dentro do PDF, impressa
  // em papel, onde o serrilhado aparece.
  var dpr = Math.min(window.devicePixelRatio || 1, 3);
  var traco = [];        // pontos do traço atual (para medir se há assinatura)
  var temTraco = false;
  var desenhando = false;
  var ultimo = null;

  function configurarCanvas() {
    var caixa = canvas.getBoundingClientRect();
    canvas.width = Math.round(caixa.width * dpr);
    canvas.height = Math.round(caixa.height * dpr);

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.lineWidth = 2.4;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    // Tinta escura literal: o PNG é embutido num PDF em papel branco, não
    // numa tela que possa estar em modo escuro. Usar a cor do tema deixaria
    // a assinatura branca sobre papel branco.
    ctx.strokeStyle = "#101828";
  }

  configurarCanvas();

  // Redimensionar APAGA o canvas (alterar width/height limpa o buffer). Só
  // reconfigura quando ainda não há traço: se o teclado virtual abriu e
  // mudou a largura no meio da assinatura, apagar o que a pessoa já assinou
  // seria pior do que conviver com um leve esticamento.
  var larguraAnterior = canvas.getBoundingClientRect().width;
  window.addEventListener("resize", function () {
    var nova = canvas.getBoundingClientRect().width;
    if (Math.abs(nova - larguraAnterior) < 2) { return; }
    larguraAnterior = nova;
    if (!temTraco) { configurarCanvas(); }
  });

  // ── Desenho ──────────────────────────────────────────────────────────────
  function posicao(evento) {
    var caixa = canvas.getBoundingClientRect();
    return {
      x: evento.clientX - caixa.left,
      y: evento.clientY - caixa.top
    };
  }

  function comecar(evento) {
    // Pointer Events cobre dedo, caneta (stylus) e mouse com um só caminho —
    // o tablet da TI pode ter caneta, e tratar touch e mouse em dois
    // conjuntos de listeners é o que gera traço duplicado no Chrome.
    if (evento.pointerType === "mouse" && evento.button !== 0) { return; }
    desenhando = true;
    ultimo = posicao(evento);
    traco.push(ultimo);
    // Captura o ponteiro: garante que o `pointerup` chegue aqui mesmo se o
    // dedo sair do canvas no meio do traço (acontece sempre ao assinar perto
    // da borda), senão o estado fica "desenhando" para sempre.
    if (canvas.setPointerCapture) {
      try { canvas.setPointerCapture(evento.pointerId); } catch (e) { /* ignora */ }
    }
    evento.preventDefault();
  }

  function mover(evento) {
    if (!desenhando) { return; }

    // `getCoalescedEvents` entrega os pontos que o navegador agrupou entre
    // dois frames: num movimento rápido, usar só o evento final produz uma
    // reta entre os extremos (assinatura "quadrada"). Onde não existe, cai
    // para o evento único.
    var eventos = (evento.getCoalescedEvents && evento.getCoalescedEvents()) || [evento];
    for (var i = 0; i < eventos.length; i++) {
      var ponto = posicao(eventos[i]);
      ctx.beginPath();
      ctx.moveTo(ultimo.x, ultimo.y);
      ctx.lineTo(ponto.x, ponto.y);
      ctx.stroke();
      ultimo = ponto;
      traco.push(ponto);
    }

    if (!temTraco && traco.length > 3) {
      temTraco = true;
      box.classList.add("is-preenchido");
      atualizarBotao();
    }

    evento.preventDefault();
  }

  function terminar(evento) {
    if (!desenhando) { return; }
    desenhando = false;
    ultimo = null;
    if (evento && evento.pointerId && canvas.releasePointerCapture) {
      try { canvas.releasePointerCapture(evento.pointerId); } catch (e) { /* ignora */ }
    }
  }

  canvas.addEventListener("pointerdown", comecar);
  canvas.addEventListener("pointermove", mover);
  canvas.addEventListener("pointerup", terminar);
  canvas.addEventListener("pointercancel", terminar);
  canvas.addEventListener("pointerleave", terminar);

  function limpar() {
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.restore();
    traco = [];
    temTraco = false;
    box.classList.remove("is-preenchido");
    atualizarBotao();
  }

  if (btnLimpar) {
    btnLimpar.addEventListener("click", function (evento) {
      evento.preventDefault();
      limpar();
    });
  }

  // ── Recorte do traço ─────────────────────────────────────────────────────
  // O canvas é um retângulo largo quase todo vazio. Enviar o PNG inteiro faz
  // a assinatura entrar no PDF como um risco minúsculo no meio de uma faixa
  // branca enorme. Recortar na área realmente usada (com uma folga) é o que
  // deixa a assinatura ocupar o espaço dela no documento.
  function pngRecortado() {
    if (!traco.length) { return null; }

    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (var i = 0; i < traco.length; i++) {
      if (traco[i].x < minX) { minX = traco[i].x; }
      if (traco[i].y < minY) { minY = traco[i].y; }
      if (traco[i].x > maxX) { maxX = traco[i].x; }
      if (traco[i].y > maxY) { maxY = traco[i].y; }
    }

    var folga = 14;
    var caixa = canvas.getBoundingClientRect();
    minX = Math.max(0, minX - folga);
    minY = Math.max(0, minY - folga);
    maxX = Math.min(caixa.width, maxX + folga);
    maxY = Math.min(caixa.height, maxY + folga);

    var largura = Math.max(1, Math.round((maxX - minX) * dpr));
    var altura = Math.max(1, Math.round((maxY - minY) * dpr));

    // O servidor recusa PNG abaixo de 120x40 (toque acidental). Garantir o
    // piso aqui evita recusar uma assinatura legítima mas estreita — uma
    // rubrica vertical, por exemplo.
    var larguraFinal = Math.max(largura, 240);
    var alturaFinal = Math.max(altura, 80);

    var destino = document.createElement("canvas");
    destino.width = larguraFinal;
    destino.height = alturaFinal;

    var dctx = destino.getContext("2d");
    dctx.drawImage(
      canvas,
      Math.round(minX * dpr), Math.round(minY * dpr), largura, altura,
      Math.round((larguraFinal - largura) / 2), Math.round((alturaFinal - altura) / 2),
      largura, altura
    );

    return destino.toDataURL("image/png");
  }

  // ── Validação de envio ───────────────────────────────────────────────────
  function nomeValido() {
    return (inputNome.value || "").trim().length >= 3;
  }

  function atualizarBotao() {
    btnEnviar.disabled = !(temTraco && chkAceite.checked && nomeValido());
  }

  if (inputNome) {
    if (!inputNome.value && NOME_SUGERIDO) { inputNome.value = NOME_SUGERIDO; }
    inputNome.addEventListener("input", atualizarBotao);
  }
  if (chkAceite) { chkAceite.addEventListener("change", atualizarBotao); }
  atualizarBotao();

  // Máscara leve de CPF: só formata, nunca valida dígito verificador — o
  // campo é opcional e recusar um CPF digitado com erro travaria a entrega
  // do equipamento por um dado que é complementar.
  if (inputCpf) {
    inputCpf.addEventListener("input", function () {
      var digitos = (inputCpf.value || "").replace(/\D/g, "").slice(0, 11);
      var saida = digitos;
      if (digitos.length > 9) {
        saida = digitos.slice(0, 3) + "." + digitos.slice(3, 6) + "." + digitos.slice(6, 9) + "-" + digitos.slice(9);
      } else if (digitos.length > 6) {
        saida = digitos.slice(0, 3) + "." + digitos.slice(3, 6) + "." + digitos.slice(6);
      } else if (digitos.length > 3) {
        saida = digitos.slice(0, 3) + "." + digitos.slice(3);
      }
      inputCpf.value = saida;
    });
  }

  function mostrarErro(mensagem) {
    avisoErro.textContent = mensagem;
    avisoErro.hidden = false;
    avisoErro.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function csrf() {
    // O cookie CSRF é legível por JS de propósito (CSRF_COOKIE_HTTPONLY=False,
    // regra 28). O campo oculto do formulário é a fonte preferida; o cookie é
    // a reserva para o caso de a tela ser aberta sem o `csrf_token` renderizado.
    var campo = document.querySelector("input[name=csrfmiddlewaretoken]");
    if (campo && campo.value) { return campo.value; }
    var achado = document.cookie.match(/csrftoken=([^;]+)/);
    return achado ? achado[1] : "";
  }

  btnEnviar.addEventListener("click", function (evento) {
    evento.preventDefault();

    var imagem = pngRecortado();
    if (!imagem) {
      mostrarErro("Assine no quadro antes de confirmar.");
      return;
    }

    avisoErro.hidden = true;
    btnEnviar.disabled = true;
    btnEnviar.textContent = "Enviando…";

    fetch(ENVIAR_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrf(),
        "X-Requested-With": "XMLHttpRequest"
      },
      body: JSON.stringify({
        assinatura: imagem,
        nome: (inputNome.value || "").trim(),
        cpf: (inputCpf ? inputCpf.value : "").trim(),
        rg: (inputRg ? inputRg.value : "").trim(),
        aceite: chkAceite.checked
      })
    })
      .then(function (resposta) {
        return resposta.json().then(function (corpo) {
          return { ok: resposta.ok, corpo: corpo };
        });
      })
      .then(function (resultado) {
        if (!resultado.ok || !resultado.corpo.ok) {
          throw new Error(resultado.corpo.erro || "Não foi possível registrar a assinatura.");
        }
        // Substitui a tela: o token já foi consumido, e deixar o formulário
        // visível convidaria a assinar de novo (e receber "já foi assinado").
        window.location.replace(CONCLUIDO_URL);
      })
      .catch(function (erro) {
        mostrarErro(erro.message || "Falha de conexão. Verifique a rede e tente de novo.");
        btnEnviar.textContent = "Confirmar e assinar";
        // Reabilita só se o estado ainda permite — o traço continua no
        // canvas, então a pessoa não perde nada ao tentar outra vez.
        atualizarBotao();
      });
  });

  // ── Contagem do prazo ────────────────────────────────────────────────────
  if (contador && EXPIRA_EM_SEGUNDOS > 0) {
    var restante = EXPIRA_EM_SEGUNDOS;

    var tique = setInterval(function () {
      restante -= 1;
      if (restante <= 0) {
        clearInterval(tique);
        contador.textContent = "expirado";
        mostrarErro("O prazo deste link expirou. Peça um novo ao setor de TI.");
        btnEnviar.disabled = true;
        return;
      }
      var minutos = Math.floor(restante / 60);
      var segundos = restante % 60;
      contador.textContent = minutos + ":" + (segundos < 10 ? "0" : "") + segundos;
    }, 1000);
  }
})();
