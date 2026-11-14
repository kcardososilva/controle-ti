/* Conciliação de chips (estoque de TI × conectividade) — comportamento mínimo.

   Servido CRU: nenhuma tag de template aqui dentro. Sem este arquivo a tela continua utilizável (os formulários enviam
   normalmente); ele só acrescenta confirmação e o "marcar todos".

   1 · `[data-ct-confirma]` num formulário: pergunta antes de enviar (a ligação em lote grava dezenas de registros).
   2 · `[data-ct-marcar-todos]`: marca/desmarca os chips da tela (só os habilitados).
   3 · `#ct-form-cadastro`: antes de cadastrar, diz quantos chips vão ser criados (os marcados, ou todos, se a caixa "todos"
       estiver ligada) — cadastrar no estoque de TI é a única ação desta tela que CRIA itens. */
(function () {
  'use strict';

  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    var pergunta = form.getAttribute && form.getAttribute('data-ct-confirma');
    if (pergunta && !window.confirm(pergunta)) ev.preventDefault();
  });

  document.addEventListener('change', function (ev) {
    if (!ev.target.matches('[data-ct-marcar-todos]')) return;
    document.querySelectorAll('#ct-form-cadastro input[name="ids"]:not(:disabled)').forEach(function (c) { c.checked = ev.target.checked; });
  });

  var cadastro = document.getElementById('ct-form-cadastro');
  if (cadastro) {
    cadastro.addEventListener('submit', function (ev) {
      var todos = cadastro.querySelector('input[name="todos"]');
      var marcados = cadastro.querySelectorAll('input[name="ids"]:checked').length;
      if (todos && todos.checked) {
        if (!window.confirm('Cadastrar no estoque de TI TODOS os chips sem item' + (cadastro.querySelector('input[name="q"]').value ? ' desta busca' : '') + '? Cada um vira um item novo, ligado ao chip.')) ev.preventDefault();
      } else if (marcados === 0) {
        ev.preventDefault();
        window.alert('Marque ao menos um chip.');
      } else if (!window.confirm('Cadastrar ' + marcados + ' chip' + (marcados === 1 ? '' : 's') + ' no estoque de TI? Cada um vira um item novo, ligado ao chip.')) {
        ev.preventDefault();
      }
    });
  }
})();
