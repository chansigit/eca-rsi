
// One delegated listener for every sparkline on the page: the point's own <title> is the text,
// so the chart stays the single source of the reading and works without script for a screen
// reader, while a pointer gets it at once instead of after the browser's tooltip delay.
(function(){
  var tip = document.createElement("div"); tip.className = "sp-tip"; document.body.appendChild(tip);
  function place(ev){ var pad = 12, w = tip.offsetWidth, h = tip.offsetHeight;
    var x = ev.clientX + pad, y = ev.clientY - h - pad;
    if (x + w > innerWidth - 4) x = ev.clientX - w - pad;
    if (y < 4) y = ev.clientY + pad;
    tip.style.left = x + "px"; tip.style.top = y + "px"; }
  document.addEventListener("mouseover", function(ev){
    var hit = ev.target.closest && ev.target.closest("circle.sp-hit"); if (!hit) return;
    var t = hit.querySelector("title"); if (!t) return;
    var text = t.textContent, cut = text.indexOf(" so far");
    tip.innerHTML = cut < 0 ? esc(text)
      : esc(text.slice(0, cut)) + '<span class="q">' + esc(text.slice(cut)) + "</span>";
    tip.classList.add("on"); place(ev);
  });
  document.addEventListener("mousemove", function(ev){ if (tip.classList.contains("on")) place(ev); });
  document.addEventListener("mouseout", function(ev){
    if (ev.target.closest && ev.target.closest("circle.sp-hit")) tip.classList.remove("on"); });
  function esc(s){ var d = document.createElement("span"); d.textContent = s; return d.innerHTML; }
})();
