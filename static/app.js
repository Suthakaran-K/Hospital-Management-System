document.addEventListener('DOMContentLoaded', function () {
  var toggle = document.getElementById('nav-toggle');
  if (toggle) toggle.addEventListener('click', function () { document.body.classList.toggle('nav-open'); });

  document.querySelectorAll('form[data-confirm]').forEach(function (f) {
    f.addEventListener('submit', function (e) { if (!window.confirm(f.dataset.confirm)) e.preventDefault(); });
  });

  var lines = document.getElementById('bill-lines');
  if (lines) {
    var add = document.getElementById('add-line');
    var recalc = function () {
      var sub = 0;
      lines.querySelectorAll('tr').forEach(function (tr) {
        var q = parseFloat(tr.querySelector('[name=qty]').value) || 0;
        var p = parseFloat(tr.querySelector('[name=price]').value) || 0;
        sub += q * p;
        tr.querySelector('.amt').textContent = (q * p).toFixed(2);
      });
      var disc = parseFloat(document.getElementById('discount').value) || 0;
      var rate = parseFloat(document.getElementById('tax_percent').value) || 0;
      var base = Math.max(sub - disc, 0);
      document.getElementById('preview').textContent =
        'Estimated total: Rs. ' + (base + base * rate / 100).toFixed(2) + ' (final amount is calculated on save)';
    };
    add.addEventListener('click', function () {
      var tr = lines.querySelector('tr').cloneNode(true);
      tr.querySelectorAll('input').forEach(function (i) { i.value = i.name === 'qty' ? '1' : ''; });
      lines.appendChild(tr); recalc();
    });
    lines.addEventListener('click', function (e) {
      if (e.target.classList.contains('rm') && lines.querySelectorAll('tr').length > 1) { e.target.closest('tr').remove(); recalc(); }
    });
    document.addEventListener('input', recalc);
    recalc();
  }

  document.querySelectorAll('select[data-role-select]').forEach(function (sel) {
    var sync = function () {
      sel.closest('form').querySelectorAll('[data-for-role]').forEach(function (el) { el.hidden = el.dataset.forRole !== sel.value; });
    };
    sel.addEventListener('change', sync); sync();
  });
});
