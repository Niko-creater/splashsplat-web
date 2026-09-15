/* SplashSplat project page
 * - lazy video loading: <video data-src="..."> gets its src (and, for clips inside hidden
 *   tabs, its poster) only when it approaches the viewport
 * - playback follows visibility, prefers-reduced-motion and a global "Pause videos" toggle
 * - accessible tab switchers (arrow keys, Home/End) and a BibTeX copy button
 */
(function () {
  'use strict';

  var reduce = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var motionOff = reduce;
  var videos = Array.prototype.slice.call(document.querySelectorAll('video[data-src]'));

  function inViewport(el) {
    var r = el.getBoundingClientRect();
    var h = window.innerHeight || document.documentElement.clientHeight;
    return r.bottom > 0 && r.top < h;
  }
  function load(v) {
    if (v.dataset.loaded) return;
    if (!v.getAttribute('poster') && v.dataset.poster) v.poster = v.dataset.poster;
    v.preload = 'auto';                       // preload="none" in the markup only guards the unloaded state
    if (v.dataset.start) {                    // start where the poster was taken (40% in), not on an empty bowl
      v.addEventListener('loadedmetadata', function () {
        var t = parseFloat(v.dataset.start) * v.duration;
        if (isFinite(t)) v.currentTime = t;
      }, { once: true });
    }
    v.src = v.dataset.src;
    v.dataset.loaded = '1';
    v.load();
  }
  function tryPlay(v) {
    if (motionOff) { v.controls = true; return; }
    if (v.closest('.panel[hidden]')) return;
    var p = v.play();
    if (p && typeof p.catch === 'function') p.catch(blocked(v));
  }
  // pause() on a still-pending play() rejects with AbortError -- that is not an autoplay block
  function blocked(v) { return function (err) { if (!err || err.name !== 'AbortError') v.controls = true; }; }
  function pauseAll() { videos.forEach(function (v) { if (!v.paused) v.pause(); }); }

  videos.forEach(function (v) {
    // click on the picture toggles playback; skipped once native controls are shown
    v.addEventListener('click', function () {
      if (v.controls) return;
      load(v);
      if (v.paused) { var p = v.play(); if (p && p.catch) p.catch(blocked(v)); }
      else v.pause();
    });
  });

  if ('IntersectionObserver' in window) {
    var near = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { load(e.target); near.unobserve(e.target); } });
    }, { rootMargin: '600px 0px' });
    var vis = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        var v = e.target;
        if (e.intersectionRatio >= 0.15) { load(v); tryPlay(v); }
        else if (!v.paused) { v.pause(); }
      });
    }, { threshold: [0, 0.15] });
    videos.forEach(function (v) { near.observe(v); vis.observe(v); });
  } else {
    videos.forEach(function (v) { load(v); tryPlay(v); });
  }

  // ---------- global pause / play ----------
  var toggle = document.getElementById('motion-toggle');
  function syncToggle() {
    if (!toggle) return;
    // play/pause pattern: the label carries the state (no aria-pressed, which would contradict it)
    toggle.classList.toggle('is-paused', motionOff);
    toggle.textContent = motionOff ? 'Play videos' : 'Pause videos';
  }
  if (toggle) {
    toggle.addEventListener('click', function () {
      motionOff = !motionOff;
      syncToggle();
      if (motionOff) pauseAll();
      else videos.forEach(function (v) { v.controls = false; if (inViewport(v)) { load(v); tryPlay(v); } });
    });
    syncToggle();
  }

  // ---------- tabs ----------
  // <div class="tabs" role="tablist" data-tabs> <button class="tab" role="tab" aria-controls="id">…</button> … </div>
  // <div class="panel" id="id" role="tabpanel">…</div>
  document.querySelectorAll('[data-tabs]').forEach(function (bar) {
    var tabs = Array.prototype.slice.call(bar.querySelectorAll('.tab'));
    function show(tab, initial) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
        var panel = document.getElementById(t.getAttribute('aria-controls') || t.dataset.target);
        if (!panel) return;
        panel.hidden = !on;
        if (initial) return;                    // at load, visibility observers decide what plays
        panel.querySelectorAll('video').forEach(function (v) {
          if (!on) { v.pause(); return; }
          if (v.closest('.panel[hidden]')) return;   // inside a nested, still-hidden gallery panel
          if (inViewport(v)) { load(v); tryPlay(v); }
        });
      });
    }
    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { show(t, false); });
      t.addEventListener('keydown', function (e) {
        var j;
        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') j = (i + 1) % tabs.length;
        else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') j = (i - 1 + tabs.length) % tabs.length;
        else if (e.key === 'Home') j = 0;
        else if (e.key === 'End') j = tabs.length - 1;
        else return;
        e.preventDefault(); show(tabs[j], false); tabs[j].focus();
      });
    });
    var initial = tabs.filter(function (t) { return t.getAttribute('aria-selected') === 'true'; })[0] || tabs[0];
    show(initial, true);
  });

  // ---------- BibTeX copy ----------
  var copy = document.getElementById('copy-bib');
  if (copy && navigator.clipboard) {
    copy.addEventListener('click', function () {
      var txt = document.getElementById('bibtex').textContent;
      navigator.clipboard.writeText(txt).then(function () {
        copy.textContent = 'Copied';
        setTimeout(function () { copy.textContent = 'Copy'; }, 1600);
      }, function () { copy.textContent = 'Copy failed'; });
    });
  } else if (copy) {
    copy.hidden = true;
  }
})();
