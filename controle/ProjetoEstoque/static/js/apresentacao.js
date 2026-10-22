/* =============================================================
   apresentacao.js — o filme de 30 segundos da vitrine do Zelo.

   Arquivo servido CRU (regra dos estáticos): tag de template aqui
   dentro não é processada — viraria texto literal e o script
   quebraria em silêncio. Dado do servidor entra por json_script;
   esta tela não consome nenhum, então não há json_script para ler.
   (O teste que protege isso varre o arquivo procurando as chaves de
   template, inclusive dentro de comentário — por isso elas não
   aparecem nem aqui.)

   Três decisões que sustentam o resto:

   1. SEM CORTE SECO. Cada cena começa a entrar ~0,6s antes de a
      anterior terminar de sair, e a camada de luz (`[data-luz]`)
      nunca some — ela só muda de posição e tamanho. É a
      continuidade do fundo que faz a troca de plano não parecer
      um corte.

   2. RASTRO DE ECO. Toda palavra revelada ganha duas cópias
      (`.ap-eco`) que chegam depois dela, borradas, e dissolvem.
      As cópias são `aria-hidden`: para o leitor de tela a frase é
      lida uma vez só.

   3. DEGRADA PARA PÁGINA ESTÁTICA. Sem GSAP, com movimento
      reduzido ou se este script falhar, a página inteira aparece
      empilhada e legível (`.ap-sem-filme`). O <head> tem um
      watchdog de 5s para o caso de este arquivo nem chegar.
   ============================================================= */
(function () {
  'use strict';

  var raiz = document.documentElement;
  var corpo = document.body;

  var palco = document.querySelector('[data-palco]');
  var cenas = Array.prototype.slice.call(document.querySelectorAll('[data-cena]'));
  var hud = document.querySelector('[data-hud]');
  var progresso = document.querySelector('[data-progresso]');
  var tempoEl = document.querySelector('[data-tempo]');
  var capitulosEl = document.querySelector('[data-capitulos]');
  var btnTocar = document.querySelector('[data-tocar]');
  var iconeTocar = document.querySelector('[data-icone-tocar]');
  var btnReiniciar = document.querySelector('[data-reiniciar]');
  var botoesPular = Array.prototype.slice.call(document.querySelectorAll('[data-pular]'));
  var rolar = document.querySelector('[data-rolar]');
  var luz = document.querySelector('[data-luz]');
  var tarjas = Array.prototype.slice.call(document.querySelectorAll('[data-tarja]'));
  var topo = document.querySelector('.ap-topo');

  var ICONE_PAUSA = '<path d="M8 5h3v14H8zM13 5h3v14h-3z"></path>';
  var ICONE_TOCAR = '<path d="M8 5l11 7-11 7z"></path>';

  /* Início e fim de cada cena, em segundos. A sobreposição entre o fim
     de uma e o início da seguinte é o crossfade — mexer aqui é mexer na
     montagem do filme. */
  var TEMPOS = [
    [0.0, 4.8],    // 1 · abertura com a marca
    [4.2, 8.4],    // 2 · o problema
    [7.8, 13.0],   // 3 · inventário
    [12.4, 17.4],  // 4 · licenças e custo
    [16.8, 21.6],  // 5 · preventiva em campo
    [21.0, 25.4],  // 6 · assinatura do termo
    [24.8, 28.4],  // 7 · infraestrutura
    [27.8, 30.6]   // 8 · fecho
  ];

  // ───────────────────────────────────────────────────────────────
  // Versão estática — sem GSAP, sem movimento ou se algo falhar
  // ───────────────────────────────────────────────────────────────

  function semFilme() {
    raiz.classList.remove('ap-com-filme');
    raiz.classList.add('ap-sem-filme', 'ap-filme-pronto', 'ap-liberado');
    corpo.classList.remove('ap-travado');
    ligarRevelacoes();
  }

  function ligarRevelacoes() {
    var alvos = Array.prototype.slice.call(document.querySelectorAll('.ap-reveal'));
    if (!alvos.length) return;

    if (!('IntersectionObserver' in window)) {
      alvos.forEach(function (el) { el.classList.add('is-dentro'); });
      return;
    }

    var observador = new IntersectionObserver(function (entradas) {
      entradas.forEach(function (entrada) {
        if (!entrada.isIntersecting) return;
        entrada.target.classList.add('is-dentro');
        observador.unobserve(entrada.target);
      });
    }, { rootMargin: '0px 0px -12% 0px', threshold: 0.12 });

    alvos.forEach(function (el) { observador.observe(el); });
  }

  var gsap = window.gsap;
  var movimentoReduzido = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ───────────────────────────────────────────────────────────────
  // Carrossel das telas (na pagina de venda, abaixo do filme)
  // ───────────────────────────────────────────────────────────────

  /* Fica ANTES da desistencia do filme de proposito: ele nao depende do
     GSAP e precisa funcionar tambem na versao estatica, que e justamente
     onde a pagina inteira vira rolagem. A rolagem com scroll-snap e
     nativa — seta e ponto so chamam scrollTo, entao mesmo que este bloco
     falhasse a faixa continuaria arrastavel. */
  function ligarCarrossel() {
    var caixa = document.querySelector('[data-carrossel]');
    if (!caixa) return;

    var trilho = caixa.querySelector('[data-trilho]');
    var slides = Array.prototype.slice.call(caixa.querySelectorAll('[data-slide]'));
    var pontosEl = caixa.querySelector('[data-pontos]');
    var anuncio = caixa.querySelector('[data-anuncio]');
    var btnAnterior = caixa.querySelector('[data-anterior]');
    var btnProximo = caixa.querySelector('[data-proximo]');
    if (!trilho || slides.length < 2) return;

    var atual = 0;
    var pontos = [];
    var relogio = null;
    var parado = movimentoReduzido;

    function rotuloDe(i) {
      var selo = slides[i].querySelector('.ap-selo');
      return selo ? selo.textContent.replace(/\s+/g, ' ').trim() : 'Tela ' + (i + 1);
    }

    function marcar() {
      pontos.forEach(function (ponto, i) {
        ponto.setAttribute('aria-current', i === atual ? 'true' : 'false');
      });
      if (anuncio) {
        anuncio.textContent = rotuloDe(atual) + ' — ' + (atual + 1) + ' de ' + slides.length;
      }
    }

    function irPara(i, doUsuario) {
      atual = (i + slides.length) % slides.length;
      var destino = slides[atual].offsetLeft - slides[0].offsetLeft;
      if (trilho.scrollTo) {
        trilho.scrollTo({ left: destino, behavior: movimentoReduzido ? 'auto' : 'smooth' });
      } else {
        trilho.scrollLeft = destino;
      }
      marcar();
      if (doUsuario) reiniciarRelogio();
    }

    slides.forEach(function (slide, i) {
      slide.setAttribute('aria-label', rotuloDe(i) + ' — ' + (i + 1) + ' de ' + slides.length);
      var ponto = document.createElement('button');
      ponto.type = 'button';
      ponto.className = 'ap-ponto-slide';
      ponto.setAttribute('aria-label', 'Ver ' + rotuloDe(i));
      ponto.addEventListener('click', function () { irPara(i, true); });
      if (pontosEl) pontosEl.appendChild(ponto);
      pontos.push(ponto);
    });

    if (btnAnterior) btnAnterior.addEventListener('click', function () { irPara(atual - 1, true); });
    if (btnProximo) btnProximo.addEventListener('click', function () { irPara(atual + 1, true); });

    trilho.addEventListener('keydown', function (evento) {
      if (evento.key === 'ArrowRight') { evento.preventDefault(); irPara(atual + 1, true); }
      if (evento.key === 'ArrowLeft') { evento.preventDefault(); irPara(atual - 1, true); }
    });

    /* Quem arrasta com o dedo tambem muda o slide ativo: sem isto os
       pontos passariam a mentir sobre onde a pessoa esta. */
    var aguardando = null;
    trilho.addEventListener('scroll', function () {
      if (aguardando) window.clearTimeout(aguardando);
      aguardando = window.setTimeout(function () {
        var base = slides[0].offsetLeft;
        var maisPerto = 0;
        var menorDistancia = Infinity;
        slides.forEach(function (slide, i) {
          var distancia = Math.abs((slide.offsetLeft - base) - trilho.scrollLeft);
          if (distancia < menorDistancia) { menorDistancia = distancia; maisPerto = i; }
        });
        if (maisPerto !== atual) { atual = maisPerto; marcar(); }
      }, 120);
    }, { passive: true });

    function reiniciarRelogio() {
      if (relogio) window.clearInterval(relogio);
      if (parado) return;
      relogio = window.setInterval(function () { irPara(atual + 1, false); }, 7000);
    }

    function segurar() { if (relogio) { window.clearInterval(relogio); relogio = null; } }

    caixa.addEventListener('mouseenter', segurar);
    caixa.addEventListener('mouseleave', reiniciarRelogio);
    caixa.addEventListener('focusin', segurar);
    caixa.addEventListener('focusout', reiniciarRelogio);
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) segurar(); else reiniciarRelogio();
    });

    marcar();
    reiniciarRelogio();
  }

  ligarCarrossel();

  if (!gsap || !palco || cenas.length !== TEMPOS.length || movimentoReduzido) {
    semFilme();
    return;
  }

  // ───────────────────────────────────────────────────────────────
  // Tipografia: quebra em palavras/letras e cria o rastro de eco
  // ───────────────────────────────────────────────────────────────

  function criarEco(hospedeiro, texto) {
    var eco = document.createElement('span');
    eco.className = 'ap-eco';
    eco.setAttribute('aria-hidden', 'true');
    eco.textContent = texto;
    hospedeiro.appendChild(eco);
    return eco;
  }

  /* Quebra o texto em palavras. Cada palavra vira um `.ap-palavra` com
     dois ecos dentro. Só serve para elemento de texto puro — headline
     com marcação dentro não passa por aqui. */
  function dividirPalavras(el) {
    var bruto = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!bruto) return { palavras: [], ecos: [] };

    el.textContent = '';
    var partes = bruto.split(' ');
    var palavras = [];
    var ecos = [];

    partes.forEach(function (parte, i) {
      var texto = parte + (i < partes.length - 1 ? ' ' : '');
      var span = document.createElement('span');
      span.className = 'ap-palavra';
      span.appendChild(document.createTextNode(texto));
      ecos.push(criarEco(span, texto), criarEco(span, texto));
      el.appendChild(span);
      palavras.push(span);
    });

    return { palavras: palavras, ecos: ecos };
  }

  function dividirLetras(el) {
    var bruto = (el.textContent || '').trim();
    el.textContent = '';
    return bruto.split('').map(function (caractere) {
      var span = document.createElement('span');
      span.className = 'ap-letra';
      span.textContent = caractere;
      el.appendChild(span);
      return span;
    });
  }

  function comprimentoDe(el) {
    try {
      if (el.getTotalLength) {
        var n = el.getTotalLength();
        if (n && isFinite(n)) return n;
      }
    } catch (erro) { /* navegador sem getTotalLength em <rect> */ }
    return 200;
  }

  function prepararTraco(el) {
    var tamanho = comprimentoDe(el);
    gsap.set(el, { strokeDasharray: tamanho, strokeDashoffset: tamanho });
    return tamanho;
  }

  // ───────────────────────────────────────────────────────────────
  // Blocos de animação reaproveitados por várias cenas
  // ───────────────────────────────────────────────────────────────

  /* Palavra entra de baixo, desfocada; o eco chega depois e dissolve.
     Os dois `duration` longos são o pedido de "transição lenta" — não
     são sobra de tempo. */
  function revelarTexto(tl, el, posicao) {
    var corte = dividirPalavras(el);
    if (!corte.palavras.length) return;

    tl.fromTo(corte.palavras,
      { yPercent: 110, opacity: 0, filter: 'blur(14px)' },
      { yPercent: 0, opacity: 1, filter: 'blur(0px)', duration: 1.25, stagger: 0.085, ease: 'expo.out' },
      posicao);

    tl.fromTo(corte.ecos,
      { yPercent: 125, opacity: 0, filter: 'blur(26px)' },
      { yPercent: 0, opacity: 0.5, filter: 'blur(7px)', duration: 1.7, stagger: 0.085, ease: 'expo.out' },
      posicao + 0.07);

    tl.to(corte.ecos,
      { opacity: 0, filter: 'blur(14px)', duration: 1.2, stagger: 0.07, ease: 'power2.out' },
      posicao + 0.85);
  }

  function surgir(tl, alvos, posicao, deslocamento) {
    var lista = Array.prototype.slice.call(alvos);
    if (!lista.length) return;
    tl.fromTo(lista,
      { y: deslocamento === undefined ? 18 : deslocamento, opacity: 0, filter: 'blur(8px)' },
      { y: 0, opacity: 1, filter: 'blur(0px)', duration: 1.2, stagger: 0.12, ease: 'power3.out' },
      posicao);
  }

  function contador(tl, el, posicao) {
    var alvo = parseFloat(el.getAttribute('data-conta'));
    if (!isFinite(alvo)) return;
    var sufixo = el.getAttribute('data-sufixo') || '';
    var moeda = el.getAttribute('data-moeda') === '1';
    var estado = { v: 0 };

    function escrever() {
      var n = Math.round(estado.v);
      el.textContent = (moeda ? n.toLocaleString('pt-BR') : String(n)) + sufixo;
    }

    tl.to(estado, {
      v: alvo, duration: 1.7, ease: 'power2.out',
      onUpdate: escrever, onStart: escrever
    }, posicao);
  }

  /* A luz nunca pisca: ela caminha de uma posição para a outra ao longo
     de toda a cena. É o que costura o corte. */
  function moverLuz(tl, posicao, para) {
    tl.to(luz, {
      xPercent: para.x, yPercent: para.y, scale: para.escala,
      opacity: para.opacidade === undefined ? 0.85 : para.opacidade,
      duration: 2.6, ease: 'sine.inOut'
    }, posicao);
  }

  // ───────────────────────────────────────────────────────────────
  // As oito cenas
  // ───────────────────────────────────────────────────────────────

  function cenaAbertura(tl, cena, inicio) {
    var tracos = cena.querySelectorAll('[data-traco]');
    var cheia = cena.querySelector('[data-marca-cheia]');
    var aneis = cena.querySelectorAll('[data-anel]');
    var letras = dividirLetras(cena.querySelector('[data-letras]'));
    var assinatura = cena.querySelectorAll('[data-surge]');

    Array.prototype.forEach.call(tracos, prepararTraco);

    tl.to(tarjas, { height: '6vh', duration: 1.4, ease: 'power2.out' }, inicio + 0.1);

    tl.to(tracos, {
      strokeDashoffset: 0, duration: 1.9, stagger: 0.25, ease: 'power2.inOut'
    }, inicio + 0.35);

    tl.fromTo(aneis,
      { scale: 0.55, opacity: 0 },
      { scale: 1.95, opacity: 0.5, duration: 2.4, stagger: 0.3, ease: 'power2.out' },
      inicio + 0.9);
    tl.to(aneis, { opacity: 0, duration: 1.6, stagger: 0.3, ease: 'power1.out' }, inicio + 1.9);

    tl.to(cheia, { opacity: 1, duration: 1.1, ease: 'power2.inOut' }, inicio + 2.0);
    tl.to(tracos, { opacity: 0, duration: 1.0, ease: 'power2.inOut' }, inicio + 2.1);

    tl.fromTo(letras,
      { yPercent: 118, opacity: 0, filter: 'blur(18px)' },
      { yPercent: 0, opacity: 1, filter: 'blur(0px)', duration: 1.5, stagger: 0.09, ease: 'expo.out' },
      inicio + 2.15);

    surgir(tl, assinatura, inicio + 3.1, 14);

    // A barra do topo só aparece depois da marca: no primeiro segundo a
    // tela tem uma coisa só para olhar.
    tl.fromTo(topo, { opacity: 0, y: -14 },
      { opacity: 1, y: 0, duration: 1.3, ease: 'power2.out' }, inicio + 2.6);

    moverLuz(tl, inicio, { x: 0, y: 0, escala: 1, opacidade: 0.95 });
  }

  function cenaProblema(tl, cena, inicio) {
    surgir(tl, cena.querySelectorAll('[data-surge]'), inicio + 0.25, 12);
    var titulos = cena.querySelectorAll('[data-palavras]');
    revelarTexto(tl, titulos[0], inicio + 0.5);
    if (titulos[1]) revelarTexto(tl, titulos[1], inicio + 1.6);
    moverLuz(tl, inicio, { x: -14, y: 8, escala: 1.25, opacidade: 0.7 });
  }

  function cenaInventario(tl, cena, inicio) {
    var janela = cena.querySelector('[data-maquete]');
    var linhas = cena.querySelectorAll('[data-linha]');
    var contas = cena.querySelectorAll('[data-conta]');
    var filtros = cena.querySelectorAll('.ap-filtro, .ap-busca');

    surgir(tl, cena.querySelectorAll('[data-surge]'), inicio + 0.3, 14);
    revelarTexto(tl, cena.querySelector('[data-palavras]'), inicio + 0.45);

    tl.fromTo(janela,
      { opacity: 0, y: 46, rotateY: -9, rotateX: 5, filter: 'blur(18px)' },
      { opacity: 1, y: 0, rotateY: 0, rotateX: 0, filter: 'blur(0px)', duration: 2.0, ease: 'power3.out' },
      inicio + 0.6);

    Array.prototype.forEach.call(contas, function (el, i) {
      contador(tl, el, inicio + 1.3 + i * 0.1);
    });

    tl.fromTo(filtros, { opacity: 0, y: 8 },
      { opacity: 1, y: 0, duration: 0.7, stagger: 0.06, ease: 'power2.out' }, inicio + 1.45);

    tl.fromTo(linhas,
      { opacity: 0, x: -26, filter: 'blur(9px)' },
      { opacity: 1, x: 0, filter: 'blur(0px)', duration: 1.1, stagger: 0.14, ease: 'power3.out' },
      inicio + 1.6);

    moverLuz(tl, inicio, { x: 18, y: -6, escala: 1.15, opacidade: 0.6 });
  }

  function cenaLicencas(tl, cena, inicio) {
    var janela = cena.querySelector('[data-maquete]');
    var arco = cena.querySelector('[data-arco]');
    var barras = cena.querySelectorAll('.ap-barra');
    var contas = cena.querySelectorAll('[data-conta]');

    surgir(tl, cena.querySelectorAll('[data-surge]'), inicio + 0.3, 14);
    revelarTexto(tl, cena.querySelector('[data-palavras]'), inicio + 0.45);

    tl.fromTo(janela,
      { opacity: 0, y: 46, rotateY: 9, filter: 'blur(18px)' },
      { opacity: 1, y: 0, rotateY: 0, filter: 'blur(0px)', duration: 2.0, ease: 'power3.out' },
      inicio + 0.55);

    tl.fromTo(arco, { strokeDashoffset: 263.9 },
      { strokeDashoffset: 15.8, duration: 2.1, ease: 'power2.inOut' }, inicio + 1.1);

    tl.fromTo(barras, { scaleY: 0 },
      { scaleY: 1, duration: 1.2, stagger: 0.08, ease: 'power3.out' }, inicio + 1.3);

    Array.prototype.forEach.call(contas, function (el, i) {
      contador(tl, el, inicio + 1.2 + i * 0.18);
    });

    moverLuz(tl, inicio, { x: -20, y: -4, escala: 1.2, opacidade: 0.65 });
  }

  function cenaPreventiva(tl, cena, inicio) {
    var fone = cena.querySelector('[data-maquete]');
    var faixa = cena.querySelectorAll('.ap-pv-mini');
    var itens = cena.querySelectorAll('[data-check]');
    var vistos = cena.querySelectorAll('[data-visto]');
    var conformes = cena.querySelectorAll('.ap-pv-opcao--sim');
    var fotos = cena.querySelectorAll('.ap-foto');

    surgir(tl, cena.querySelectorAll('[data-surge]'), inicio + 0.3, 14);
    revelarTexto(tl, cena.querySelector('[data-palavras]'), inicio + 0.45);

    tl.fromTo(fone,
      { opacity: 0, y: 60, rotateZ: -4, filter: 'blur(20px)' },
      { opacity: 1, y: 0, rotateZ: 0, filter: 'blur(0px)', duration: 2.0, ease: 'power3.out' },
      inicio + 0.55);

    tl.fromTo(faixa, { opacity: 0, y: 10 },
      { opacity: 1, y: 0, duration: 0.7, stagger: 0.06, ease: 'power2.out' }, inicio + 1.1);

    tl.fromTo(itens, { opacity: 0, x: 18 },
      { opacity: 1, x: 0, duration: 0.85, stagger: 0.14, ease: 'power3.out' }, inicio + 1.3);

    /* O tecnico marcando "Conforme" item a item: o visto e desenhado e a
       opcao escolhida acende junto. Na tela real a resposta booleana e
       esse par de botoes, nao um checkbox. */
    Array.prototype.forEach.call(vistos, function (visto) { prepararTraco(visto); });
    tl.to(vistos, { strokeDashoffset: 0, duration: 0.5, stagger: 0.2, ease: 'power2.out' }, inicio + 1.75);
    tl.fromTo(conformes, { opacity: 0.45 },
      { opacity: 1, duration: 0.45, stagger: 0.2, ease: 'power1.out' }, inicio + 1.75);

    tl.fromTo(fotos, { opacity: 0, scale: 0.9 },
      { opacity: 1, scale: 1, duration: 0.9, stagger: 0.12, ease: 'back.out(1.6)' }, inicio + 2.6);

    moverLuz(tl, inicio, { x: 16, y: 10, escala: 1.1, opacidade: 0.6 });
  }

  function cenaAssinatura(tl, cena, inicio) {
    var tablet = cena.querySelector('[data-maquete]');
    var linhasDoc = cena.querySelectorAll('[data-doc]');
    var firma = cena.querySelector('[data-firma]');
    var selo = cena.querySelector('[data-selo]');

    surgir(tl, cena.querySelectorAll('[data-surge]'), inicio + 0.3, 14);
    revelarTexto(tl, cena.querySelector('[data-palavras]'), inicio + 0.45);

    tl.fromTo(tablet,
      { opacity: 0, y: 50, rotateX: 10, filter: 'blur(18px)' },
      { opacity: 1, y: 0, rotateX: 0, filter: 'blur(0px)', duration: 1.9, ease: 'power3.out' },
      inicio + 0.5);

    tl.fromTo(linhasDoc, { scaleX: 0, opacity: 0 },
      { scaleX: 1, opacity: 1, duration: 0.7, stagger: 0.12, ease: 'power2.out' }, inicio + 1.1);

    // O traço é desenhado no ritmo de quem assina, não no de quem anima.
    prepararTraco(firma);
    tl.to(firma, { strokeDashoffset: 0, duration: 1.9, ease: 'power1.inOut' }, inicio + 1.5);

    tl.fromTo(selo, { opacity: 0, y: 14, scale: 0.94 },
      { opacity: 1, y: 0, scale: 1, duration: 1.0, ease: 'back.out(1.5)' }, inicio + 3.1);

    moverLuz(tl, inicio, { x: -16, y: 12, escala: 1.08, opacidade: 0.62 });
  }

  function cenaInfra(tl, cena, inicio) {
    var janela = cena.querySelector('[data-maquete]');
    var terreno = cena.querySelector('.ap-terreno');
    var lado = cena.querySelectorAll('.ap-kq-metrica, .ap-kq-item');
    var rotas = cena.querySelectorAll('[data-rota]');
    var nos = cena.querySelectorAll('[data-no]');
    var balao = cena.querySelector('.ap-no-balao');
    var alerta = cena.querySelector('.ap-no--alerta .ap-no-bolha');

    surgir(tl, cena.querySelectorAll('[data-surge]'), inicio + 0.3, 14);
    revelarTexto(tl, cena.querySelector('[data-palavras]'), inicio + 0.45);

    tl.fromTo(janela,
      { opacity: 0, scale: 1.05, filter: 'blur(20px)' },
      { opacity: 1, scale: 1, filter: 'blur(0px)', duration: 1.9, ease: 'power3.out' },
      inicio + 0.5);

    /* O terreno entra como uma imagem de satelite carregando: aparece
       antes dos marcadores, senao os pinos ficariam boiando no vazio. */
    if (terreno) {
      tl.fromTo(terreno, { opacity: 0 },
        { opacity: 1, duration: 1.2, ease: 'power2.out' }, inicio + 0.7);
    }

    tl.fromTo(lado, { opacity: 0, x: -14 },
      { opacity: 1, x: 0, duration: 0.7, stagger: 0.07, ease: 'power2.out' }, inicio + 0.95);

    Array.prototype.forEach.call(rotas, prepararTraco);
    tl.to(rotas, { strokeDashoffset: 0, duration: 1.7, stagger: 0.18, ease: 'power2.inOut' }, inicio + 1.0);

    tl.fromTo(nos, { opacity: 0, scale: 0.7 },
      { opacity: 1, scale: 1, duration: 0.75, stagger: 0.1, ease: 'back.out(2)' }, inicio + 1.3);

    if (balao) {
      tl.fromTo(balao, { opacity: 0, y: 8 },
        { opacity: 1, y: 0, duration: 0.7, ease: 'back.out(1.8)' }, inicio + 2.1);
    }

    /* Pulso do nó em alerta. `repeat` FINITO: um repeat:-1 dentro de uma
       timeline deixa a duração dela infinita, e aí a barra de progresso
       e o seek por capítulo param de fazer sentido. */
    if (alerta) {
      // A cor sai do CSS (var(--chart-2)) já resolvida pelo navegador: o
      // `color-mix()` da folha não é interpolável, e repetir o HEX aqui
      // faria a cor do pulso envelhecer separada do design system.
      var cor = window.getComputedStyle(alerta).backgroundColor;
      var apagada = cor && cor.indexOf('rgb(') === 0
        ? cor.replace('rgb(', 'rgba(').replace(')', ', 0)')
        : 'rgba(232, 134, 42, 0)';

      tl.fromTo(alerta,
        { boxShadow: '0 0 0 0 ' + (cor || 'rgb(232, 134, 42)') },
        { boxShadow: '0 0 0 14px ' + apagada, duration: 1.2, repeat: 2, ease: 'power2.out' },
        inicio + 1.9);
    }

    moverLuz(tl, inicio, { x: 10, y: -12, escala: 1.3, opacidade: 0.55 });
  }

  function cenaFecho(tl, cena, inicio) {
    var marca = cena.querySelector('[data-marca-fecho]');

    tl.fromTo(marca, { opacity: 0, scale: 0.86, filter: 'blur(14px)' },
      { opacity: 1, scale: 1, filter: 'blur(0px)', duration: 1.4, ease: 'power3.out' }, inicio + 0.2);

    revelarTexto(tl, cena.querySelector('[data-palavras]'), inicio + 0.6);
    surgir(tl, cena.querySelectorAll('[data-surge]'), inicio + 1.3, 16);

    tl.to(tarjas, { height: 0, duration: 1.6, ease: 'power2.inOut' }, inicio + 0.4);
    moverLuz(tl, inicio, { x: 0, y: 0, escala: 1.5, opacidade: 0.9 });
  }

  var MONTAGEM = [
    cenaAbertura, cenaProblema, cenaInventario, cenaLicencas,
    cenaPreventiva, cenaAssinatura, cenaInfra, cenaFecho
  ];

  // ───────────────────────────────────────────────────────────────
  // Montagem da linha do tempo
  // ───────────────────────────────────────────────────────────────

  gsap.set(cenas, { autoAlpha: 0 });
  gsap.set(topo, { opacity: 0 });
  gsap.set(luz, { xPercent: 0, yPercent: 0, scale: 1 });

  var filme = gsap.timeline({
    paused: true,
    onUpdate: aoAtualizar,
    onComplete: aoTerminar
  });

  cenas.forEach(function (cena, i) {
    var inicio = TEMPOS[i][0];
    var fim = TEMPOS[i][1];
    var ultima = i === cenas.length - 1;

    // Entrada: a cena já começa a aparecer enquanto a anterior sai.
    filme.fromTo(cena,
      { autoAlpha: 0, scale: 1.035, filter: 'blur(16px)' },
      { autoAlpha: 1, scale: 1, filter: 'blur(0px)', duration: 1.5, ease: 'power2.out' },
      inicio);

    MONTAGEM[i](filme, cena, inicio);

    // Saída: só a última cena permanece na tela no fim do filme.
    if (!ultima) {
      filme.to(cena,
        { autoAlpha: 0, scale: 0.985, filter: 'blur(18px)', duration: 1.4, ease: 'power2.in' },
        fim - 1.4);
    }
  });

  // Fecha a linha do tempo no instante exato do fim da última cena: sem
  // isto a duração seria a do tween mais longo, e a barra de progresso
  // chegaria a 100% antes de a última cena terminar de se montar.
  var FIM = TEMPOS[TEMPOS.length - 1][1];
  filme.to(luz, { opacity: 0.9, duration: 0.2, ease: 'none' }, FIM - 0.2);

  var duracao = filme.duration();

  // ───────────────────────────────────────────────────────────────
  // HUD — capítulos, progresso, controles
  // ───────────────────────────────────────────────────────────────

  var botoesCapitulo = cenas.map(function (cena, i) {
    var botao = document.createElement('button');
    botao.type = 'button';
    botao.className = 'ap-capitulo';
    botao.innerHTML = '<i aria-hidden="true"></i>';
    botao.appendChild(document.createTextNode(cena.getAttribute('data-rotulo') || ('Cena ' + (i + 1))));
    botao.addEventListener('click', function () {
      filme.pause();
      // `tweenTo` em vez de `seek`: ir ao capítulo também é uma
      // transição, não um salto.
      filme.tweenTo(TEMPOS[i][0] + 0.01, {
        duration: 0.7, ease: 'power2.inOut',
        onComplete: function () { filme.play(); trocarIcone(true); }
      });
    });
    if (capitulosEl) capitulosEl.appendChild(botao);
    return botao;
  });

  function formatarTempo(segundos) {
    var s = Math.max(0, Math.round(segundos));
    var m = Math.floor(s / 60);
    var r = s % 60;
    return (m < 10 ? '0' : '') + m + ':' + (r < 10 ? '0' : '') + r;
  }

  var capituloAtual = -1;

  function aoAtualizar() {
    // A montagem renderiza a timeline antes de o HUD existir; sem esta
    // guarda o primeiro render estoura em `botoesCapitulo.forEach`.
    if (!botoesCapitulo || !duracao) return;

    var t = filme.time();

    if (progresso) progresso.style.width = ((t / duracao) * 100).toFixed(2) + '%';
    if (tempoEl) tempoEl.textContent = formatarTempo(t) + ' / ' + formatarTempo(duracao);

    var indice = 0;
    for (var i = 0; i < TEMPOS.length; i++) {
      if (t >= TEMPOS[i][0]) indice = i;
    }
    if (indice !== capituloAtual) {
      capituloAtual = indice;
      botoesCapitulo.forEach(function (botao, i) {
        botao.setAttribute('aria-current', i === indice ? 'true' : 'false');
      });
    }
  }

  function trocarIcone(tocando) {
    if (iconeTocar) iconeTocar.innerHTML = tocando ? ICONE_PAUSA : ICONE_TOCAR;
    if (btnTocar) btnTocar.setAttribute('aria-label', tocando ? 'Pausar apresentação' : 'Retomar apresentação');
  }

  var liberado = false;

  /* Fim do filme: a página destrava, o HUD sai e o convite para rolar
     aparece. Idempotente — "pular" e o fim natural chamam o mesmo. */
  function liberar() {
    if (liberado) return;
    liberado = true;

    raiz.classList.add('ap-liberado');
    corpo.classList.remove('ap-travado');

    gsap.to(hud, { autoAlpha: 0, duration: 0.9, ease: 'power2.out' });
    gsap.to(tarjas, { height: 0, duration: 1.0, ease: 'power2.inOut' });

    if (rolar) rolar.classList.add('is-visivel');
    botoesPular.forEach(function (botao) {
      if (botao.classList.contains('ap-btn')) botao.textContent = 'Rever apresentação';
      botao.setAttribute('data-rever', '1');
      botao.setAttribute('aria-label', 'Rever a apresentação desde o começo');
    });
  }

  function aoTerminar() {
    trocarIcone(false);
    liberar();
  }

  function rever() {
    liberado = false;
    raiz.classList.remove('ap-liberado');
    corpo.classList.add('ap-travado');
    window.scrollTo({ top: 0, behavior: 'smooth' });

    gsap.to(hud, { autoAlpha: 1, duration: 0.6 });
    if (rolar) rolar.classList.remove('is-visivel');
    botoesPular.forEach(function (botao) {
      if (botao.classList.contains('ap-btn')) botao.textContent = 'Pular apresentação';
      botao.removeAttribute('data-rever');
      botao.setAttribute('aria-label', 'Pular para o fim da apresentação');
    });

    filme.restart();
    trocarIcone(true);
  }

  /* Pular não corta: acelera o filme até o fim em pouco mais de um
     segundo. Um `seek(duracao)` seco entregaria a última cena sem
     nenhuma das transições, que é justamente o que esta peça evita. */
  function pular() {
    filme.pause();
    filme.tweenTo(duracao, {
      duration: 1.1, ease: 'power2.inOut',
      onComplete: function () { aoTerminar(); }
    });
  }

  if (btnTocar) {
    btnTocar.addEventListener('click', function () {
      if (filme.paused()) { filme.play(); trocarIcone(true); }
      else { filme.pause(); trocarIcone(false); }
    });
  }

  if (btnReiniciar) btnReiniciar.addEventListener('click', rever);

  botoesPular.forEach(function (botao) {
    botao.addEventListener('click', function () {
      if (botao.getAttribute('data-rever') === '1') rever();
      else pular();
    });
  });

  // Links internos para o conteúdo precisam primeiro soltar a página —
  // ela está com `overflow: hidden` enquanto o filme roda.
  Array.prototype.forEach.call(document.querySelectorAll('a[href="#conteudo"]'), function (link) {
    link.addEventListener('click', function (evento) {
      evento.preventDefault();
      if (!liberado) pular();
      window.setTimeout(function () {
        var destino = document.getElementById('conteudo');
        if (destino) destino.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }, liberado ? 0 : 1200);
    });
  });

  document.addEventListener('keydown', function (evento) {
    var alvo = evento.target;
    var digitando = alvo && (alvo.tagName === 'BUTTON' || alvo.tagName === 'A' ||
                             alvo.tagName === 'INPUT' || alvo.tagName === 'TEXTAREA');

    if (evento.key === ' ' && !digitando) {
      evento.preventDefault();
      if (filme.paused()) { filme.play(); trocarIcone(true); }
      else { filme.pause(); trocarIcone(false); }
    }

    if (evento.key === 'Escape' && !liberado) pular();
  });

  // Aba escondida não deve "gastar" o filme — e a retomada só acontece
  // se ele estava rodando quando a aba saiu de vista.
  var tocavaAntes = false;
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      tocavaAntes = !filme.paused();
      filme.pause();
      trocarIcone(false);
    } else if (tocavaAntes && !liberado) {
      filme.play();
      trocarIcone(true);
    }
  });

  if (rolar) {
    window.addEventListener('scroll', function () {
      if (window.scrollY > 40) rolar.classList.remove('is-visivel');
    }, { passive: true });
  }

  ligarRevelacoes();

  raiz.classList.add('ap-filme-pronto');
  trocarIcone(true);
  filme.play();
})();
